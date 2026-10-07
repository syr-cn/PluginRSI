import asyncio
import json

import pytest

from pluginrsi.evaluation.runner import Evaluator as RealEvaluator
from pluginrsi.search.mutation import paired
from pluginrsi.search.compact_evidence import trace_views
from pluginrsi.search.loop import Search
from pluginrsi.search.loop import initialize
from pluginrsi.search.selection import apply_quality_policy, is_valid
from pluginrsi.search.store import read_json, write_json
from test_full_search import config_at, Evaluator, Proposer


@pytest.mark.parametrize('eager', [True, False])
@pytest.mark.parametrize('blocked', [1, 2])
def test_provider_rejection_does_not_cancel_peers_or_retry_blocked_task(tmp_path, monkeypatch, eager, blocked):
    config = config_at(tmp_path)
    config.search.max_task_rollouts = 500
    config.evaluation.concurrency = 20
    config.evaluation.accept_partial_results = True
    config.evaluation.fail_fast_candidate_errors = True
    config.evaluation.eager_infra_retries = eager
    config.evaluation.infra_retry_attempts = 1
    config.evaluation.min_valid_ratio = .95
    store = initialize(config)
    calls = []
    async def command(command, cwd, log_path, timeout, env=None):
        tid = int(read_json(cwd / 'request.json')['task']['id'][1:])
        calls.append(tid)
        if tid < blocked:
            row = dict(score=0, status='infra_error', detail='ModelRequestRejected', retryable=False)
        else:
            await asyncio.sleep(.02)
            row = dict(score=1, status='completed')
        write_json(cwd / 'result.json', row)
        return 0
    monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', command)
    evaluator = RealEvaluator(config, store, retry_infra=True)
    tasks = [dict(id=f't{i}') for i in range(20)]
    result = asyncio.run(evaluator.evaluate('c000000', tasks, 'screening', 'provider-rejection'))
    assert result['valid_task_count'] == 20 - blocked
    assert is_valid(result) == (blocked == 1)
    assert result.get('finish_reason') != 'candidate_error'
    assert sorted(calls) == list(range(20))
    asyncio.run(evaluator.evaluate('c000000', tasks, 'screening', 'provider-rejection'))
    assert sorted(calls) == list(range(20))


@pytest.mark.parametrize('total,valid,hard_error', [(20, 19, False), (100, 95, False), (20, 18, False), (20, 19, True)])
def test_fast_completion_deadline_and_hard_errors(tmp_path, monkeypatch, total, valid, hard_error):
    config = config_at(tmp_path)
    config.search.max_task_rollouts = 500
    config.evaluation.concurrency = total
    config.evaluation.accept_partial_results = True
    config.evaluation.min_valid_ratio = .95
    config.evaluation.local_timeout_seconds = .2
    config.evaluation.infra_retry_attempts = 1
    store = initialize(config)
    active = 0
    calls = []

    async def command(command, cwd, log_path, timeout, env=None):
        nonlocal active
        tid = int(read_json(cwd / 'request.json')['task']['id'][1:])
        calls.append(tid)
        active += 1
        try:
            if tid >= valid:
                if hard_error:
                    write_json(cwd / 'result.json', dict(score=0, status='candidate_error'))
                    return 0
                await asyncio.Event().wait()
            await asyncio.sleep(.02)
            write_json(cwd / 'result.json', dict(score=1, status='completed'))
            return 0
        finally:
            active -= 1

    monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', command)
    tasks = [dict(id=f't{i}') for i in range(total)]
    evaluator = RealEvaluator(config, store, retry_infra=True)
    result = asyncio.run(evaluator.evaluate('c000000', tasks, 'screening', 'fast'))
    assert is_valid(result) == (valid / total >= .95 and not hard_error)
    assert active == 0 and evaluator._reserved_rollouts == 0
    assert result['valid_task_count'] == valid
    if is_valid(result):
        assert result['finish_reason'] == 'valid_ratio_reached'
        assert result['mean_score'] == 1
        assert all(row['excluded'] for row in result['scores'][valid:])
        before = len(calls)
        assert asyncio.run(evaluator.evaluate('c000000', tasks, 'screening', 'fast')) == result
        assert len(calls) == before
    elif not hard_error:
        assert result['finish_reason'] == 'evaluation_deadline'
        assert result['mean_score'] is None


def adaptive_config(tmp_path):
    config = config_at(tmp_path)
    config.search.recomposition_offspring = 1
    config.search.local_branch_timeout_seconds = 10
    return config


@pytest.fixture
def loader_without_subprocess(monkeypatch):
    async def validate(*args):
        pass
    monkeypatch.setattr(Search, 'validate_command', validate)


def test_branch_timeout_cancels_proposer(tmp_path):
    config = adaptive_config(tmp_path)
    config.search.local_branch_timeout_seconds = .03
    store = initialize(config)
    cancelled = []

    class HangingProposer:
        async def propose(self, request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(request['candidate_id'])

    asyncio.run(Search(config, store, Evaluator(store), HangingProposer()).run())
    local = read_json(store.root / 'iterations/iter_0001/plugin_mutation.json')
    assert len(cancelled) == 2
    assert all(b['stopped_reason'] == 'branch_timeout' for b in local['branches'])
    assert not local['published_plugins']


def test_compact_trace_drops_replayed_context_and_preserves_evidence(tmp_path):
    trace = tmp_path / 'trajectory.jsonl'
    rows = [dict(event='model/request', request=dict(input=[dict(role='user', content='task'), dict(role='assistant', content='replayed' * 100000)])),
            dict(event='tool/request', command='pytest'), dict(event='tool/response', content='failure detail ' * 10000)]
    trace.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    score = dict(task_id='t1', score=0, status='completed')
    summary, events, data = trace_views(trace, score)
    assert len(summary.read_text()) < 3000
    assert events.stat().st_size < 2000
    assert 'replayed' not in events.read_text()
    assert 'failure detail' in events.read_text()
    assert data['command_count'] == 1 and data['model_calls'] == 1
    before = summary.stat().st_mtime_ns
    trace_views(trace, score)
    assert summary.stat().st_mtime_ns == before


def test_paired_gate_does_not_accept_changed_denominator():
    parent = apply_quality_policy(dict(scores=[dict(task_id=f't{i}', score=int(i < 10), status='completed') for i in range(20)]), .95)
    child = dict(parent, scores=[dict(row) for row in parent['scores']])
    child['scores'][-1]['status'] = 'infra_error'
    child = apply_quality_policy(child, .95)
    assert child['mean_score'] > parent['mean_score']
    assert not paired(parent, child, [f't{i}' for i in range(20)], .95)['retained']


def test_even_iteration_synthesizes_once_and_resume_repairs_interrupted_proposal(tmp_path, monkeypatch):
    config = adaptive_config(tmp_path)
    config.search.iterations = 2
    config.search.max_task_rollouts = 100
    store = initialize(config)
    evaluator, proposer = Evaluator(store), Proposer()
    evaluator.retry_infra = True
    search = Search(config, store, evaluator, proposer)

    async def validate(*args):
        pass

    monkeypatch.setattr(Search, 'validate_command', validate)
    asyncio.run(search.run())
    assert len([r for r in proposer.requests if r['phase'] == 'harness_recomposition']) == 2
    assert len([c for c in evaluator.calls if c[1] == 'selection']) == 3
    local = read_json(store.root / 'iterations/iter_0001/plugin_mutation.json')
    slot = local['slots'][0]
    directory = store.candidate(slot['id'])
    (directory / 'proposal_result.json').unlink()
    slot['status'] = 'proposing'
    request = read_json(directory / 'proposal_request.json')
    count = len(proposer.requests)
    asyncio.run(search.propose_slot(slot, request, local, store.root / 'recovery-test.json'))
    assert slot['status'] == 'ready' and len(proposer.requests) == count + 1


def test_compact_read_batch_retains_negative_evidence_and_respects_budget(tmp_path, monkeypatch):
    from pluginrsi.search.text_proposer import TextActionEvolver
    from pluginrsi.search.compact_evidence import prepare_compact
    output = tmp_path / 'candidate'
    (output / 'harness').mkdir(parents=True)
    evidence = tmp_path / 'evidence.txt'
    evidence.write_text('detail ' * 10000)
    request = dict(output_dir=str(output), parent_dir=str(output / 'harness'), library_dirs=[],
                   allowed_plugin_refs=[], feedback_dir=str(tmp_path), feedback={'scores': []},
                   phase='harness_recomposition', candidate_id='c1', instructions='Write files',
                   proposer_agent={'model': 'fixture'}, proposer_context_max_bytes=131072,
                   proposer_read_max_chars=12000, proposer_read_batch_max_chars=32000,
                   compact_evidence=True, extra_readable_files={'evidence.txt': str(evidence)},
                   local_evidence=[dict(id='bad', retained=False, matched_gain=-.1, losses=2)])
    agent = TextActionEvolver(request)
    public = prepare_compact(agent, {}, [])
    assert public['local_evidence'][0]['losses'] == 2
    assert not public['focus_candidates']
    monkeypatch.setattr(agent, 'prepare_context', lambda: {})

    class Model:
        def __init__(self, *args):
            self.calls = self.tokens = 0
        async def complete(self, messages, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                actions = [dict(name='read_file', arguments={'path': 'evidence.txt'})] * 8
            else:
                results = json.loads(messages[-1]['content'])['action_results']
                assert sum(len(r['result']) for r in results if not r['result'].startswith('error:')) <= 32000
                assert any(r['result'].startswith('error: read batch budget') for r in results)
                actions = [dict(name='submit_proposal', arguments=dict(hypothesis='fixture', changes=[], new_plugin_refs=[]))]
            return dict(output_text=json.dumps(dict(actions=actions, final=self.calls > 1)))
        async def close(self):
            pass
    monkeypatch.setattr('pluginrsi.search.text_proposer.SolverAgent', Model)
    asyncio.run(agent._run())
    assert read_json(output / 'proposal_result.json')['hypothesis'] == 'fixture'


def test_cancelled_worker_terminates_its_process_group(tmp_path):
    import sys
    from pathlib import Path
    from pluginrsi.search.proposer import run_command

    pid_file = tmp_path / 'child.pid'
    code = ('import subprocess,sys,time; from pathlib import Path; '
            'p=subprocess.Popen([sys.executable,"-S","-c","import time; time.sleep(60)"]); '
            'Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)')

    async def exercise():
        job = asyncio.create_task(run_command([sys.executable, '-S', '-c', code, str(pid_file)], tmp_path, tmp_path / 'worker.log', 10))
        try:
            async with asyncio.timeout(5):
                while not pid_file.exists():
                    await asyncio.sleep(.01)
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        assert job.cancelled()
        status = Path('/proc') / pid_file.read_text() / 'status'
        async with asyncio.timeout(1):
            while True:
                try:
                    state = status.read_text()
                except FileNotFoundError:
                    break
                if any(f'State:\t{code}' in state for code in ('Z', 'X')):
                    break
                await asyncio.sleep(.01)
    asyncio.run(exercise())
