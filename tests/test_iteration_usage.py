import asyncio
import json
from pathlib import Path

from pluginrsi.runtime import EventLog
from pluginrsi.search.loop import Search
from pluginrsi.search.loop import initialize
from pluginrsi.search.store import Store, read_json, write_json
from pluginrsi.search.usage import write_iteration_usage
from pluginrsi.evaluation.runner import Evaluator as RealEvaluator
from pluginrsi.evaluation import worker
from pluginrsi.tracing import ORIGIN
from test_full_search import config_at, Evaluator, Proposer


def test_finished_experiment_automatically_writes_iteration_json(tmp_path):
    config = config_at(tmp_path)
    config.solver.api_metrics_path = tmp_path/'solver.jsonl'
    config.proposer.agent = config.solver.model_copy(update={'api_metrics_path': tmp_path/'evolver.jsonl'})
    store = initialize(config)

    class MeteredEvaluator(Evaluator):
        async def evaluate(self, cid, tasks, purpose, key):
            cached = key in self.store.state['evaluation_keys']
            result = await super().evaluate(cid, tasks, purpose, key)
            if not cached:
                for task in tasks:
                    EventLog(config.solver.api_metrics_path)('api/request_end',
                        request_id=f"{result['id']}:{task['id']}", model_role='solver',
                        evaluation_id=result['id'], iteration=store.state['iteration'],
                        phase='heldout' if purpose == 'heldout' else store.state['phase'],
                        success=True, usage={'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12})
            return result

    class MeteredProposer(Proposer):
        async def propose(self, request):
            EventLog(config.proposer.agent.api_metrics_path)('api/request_end',
                request_id=request['candidate_id'], model_role='evolver',
                candidate_id=request['candidate_id'], iteration=request['iteration'], phase=request['phase'],
                success=True, usage={'input_tokens': 30, 'output_tokens': 5, 'total_tokens': 35})
            return await super().propose(request)

    search = Search(config, store, MeteredEvaluator(store), MeteredProposer())
    asyncio.run(search.run())
    first = read_json(store.root/'iteration_usage.json')
    iteration = first['iterations'][0]
    assert iteration['completed'] and iteration['iteration'] == 1
    assert iteration['solver']['rollouts'] == 2 * 2 + 2 * 4
    assert iteration['solver']['input_tokens'] == 120 and iteration['solver']['output_tokens'] == 24
    assert iteration['evolver']['rollouts'] == 4
    assert iteration['evolver']['input_tokens'] == 120 and iteration['evolver']['output_tokens'] == 20
    assert first['initialization']['solver']['rollouts'] == 4
    asyncio.run(search.run())
    assert read_json(store.root/'iteration_usage.json')['totals'] == first['totals']
    asyncio.run(search.report_heldout())
    final = read_json(store.root/'iteration_usage.json')
    assert final['heldout']['solver']['rollouts'] == 2
    assert final['heldout']['solver']['input_tokens'] == 20
    assert final['iterations'] == first['iterations']
    assert final['totals']['solver']['rollouts'] == 18


def test_retries_duplicate_events_and_parent_reuse_do_not_double_count(tmp_path):
    config = config_at(tmp_path)
    config.solver.api_metrics_path = tmp_path/'api.jsonl'
    config.proposer.agent = config.solver.model_copy()
    store = Store(tmp_path/'run')
    store.state['rollouts_started'] = 3
    for iteration, attempts in ((1, 2), (2, 1)):
        write_json(store.root/f'evaluations/e{iteration}/evaluation.json',
            {'id': f'e{iteration}', 'candidate_id': 'same_parent', 'iteration': iteration,
             'phase': 'plugin_mutation', 'rollouts_started': attempts})
        write_json(store.root/f'iterations/iter_{iteration:04d}/plugin_mutation.json',
            {'iteration': iteration, 'phase': 'plugin_mutation', 'completed': False,
             'slots': [{'id': f'c{iteration}', 'proposal_attempts': 2 if iteration == 1 else 1}]})
    events = [
        {'event': 'api/request_end', 'request_id': 'failed', 'model_role': 'solver',
         'evaluation_id': 'e1', 'success': False, 'usage': None},
        {'event': 'api/request_end', 'request_id': 'good', 'model_role': 'solver',
         'evaluation_id': 'e1', 'success': True, 'usage': {'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10}},
        {'event': 'api/request_end', 'request_id': 'later', 'model_role': 'solver',
         'evaluation_id': 'e2', 'success': True, 'usage': {'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15}},
        {'event': 'api/request_start', 'request_id': 'pending', 'model_role': 'evolver', 'candidate_id': 'c2'},
    ]
    config.solver.api_metrics_path.write_text(''.join(json.dumps(e)+'\n' for e in events+[events[1]])+'{"partial":')
    report = write_iteration_usage(config, store)
    first, second = report['iterations']
    assert first['solver']['rollouts'] == 2 and first['evolver']['rollouts'] == 2
    assert first['solver']['input_tokens'] == 7 and first['solver']['output_tokens'] == 3
    assert first['solver']['api_requests'] == 2 and first['solver']['api_failures'] == 1
    assert first['solver']['requests_without_usage'] == 1
    assert second['solver']['input_tokens'] == 11
    assert second['evolver']['pending_requests'] == 1
    assert report['totals']['solver']['rollouts'] == 3
    assert write_iteration_usage(config, store)['totals'] == report['totals']


def test_budget_stop_can_report_heldout_and_updates_usage_json(tmp_path):
    config = config_at(tmp_path)
    config.search.max_task_rollouts = 5
    store = initialize(config)
    search = Search(config, store, Evaluator(store), Proposer())
    asyncio.run(search.run())
    assert store.state['phase_index'] == 0
    assert 'budget' in store.state['stop_reason']
    assert (store.root/'iteration_usage.json').exists()
    asyncio.run(search.report_heldout())
    assert (store.root/'heldout_report.json').exists()
    assert read_json(store.root/'iteration_usage.json')['heldout']['solver']['rollouts'] == 1


def test_worker_requests_keep_evaluation_iteration_not_parent_iteration(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    store = initialize(config)
    store.state.update(iteration=7, phase='plugin_mutation')
    requests = []

    async def command(command, cwd, log_path, timeout, env=None):
        request = read_json(cwd/'request.json')
        requests.append(request)
        write_json(cwd/'result.json', {'score': 1, 'status': 'completed'})
        return 0

    monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', command)
    result = asyncio.run(RealEvaluator(config, store).evaluate('c000000', [{'id': 'task'}], 'feedback', 'local:7:parent'))
    assert result['iteration'] == 7 and result['phase'] == 'plugin_mutation'
    accounting = requests[0]['accounting']
    assert accounting == {'iteration': 7, 'phase': 'plugin_mutation', 'evaluation_id': result['id'],
                          'model_role': 'solver', 'task_attempt': 1}

    async def evaluate(request):
        assert ORIGIN.get()['iteration'] == 7
        assert ORIGIN.get()['model_role'] == 'solver'
        return {'score': 1, 'status': 'completed'}

    request = requests[0]
    request['evaluation']['benchmark'] = 'swe_harbor'
    path = Path(request['work_dir'])/'request.json'
    write_json(path, request)
    monkeypatch.setattr(worker, 'evaluate_harbor', evaluate)
    asyncio.run(worker.main(path))
    assert ORIGIN.get() == {}
