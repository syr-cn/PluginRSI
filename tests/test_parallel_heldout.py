import asyncio
import json
from collections import Counter

import pytest

from pluginrsi.evaluation.runner import Evaluator as RealEvaluator, heldout_reserve
from pluginrsi.search.loop import Search
from pluginrsi.search.loop import initialize
from pluginrsi.search.store import read_json, write_json
from test_full_search import config_at, Evaluator, Proposer


def test_seed_runs_400_heldin_and_100_heldout_together(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    config.evaluation.feedback_tasks.write_text(''.join(json.dumps(dict(id=f'in{i}')) + '\n' for i in range(400)))
    config.evaluation.heldout_tasks.write_text(''.join(json.dumps(dict(id=f'out{i}')) + '\n' for i in range(100)))
    config.evaluation.parallel_heldout = True
    config.evaluation.concurrency = 500
    config.search.iterations = 0
    config.search.max_task_rollouts = 500
    store = initialize(config)

    async def exercise():
        active = peak = 0
        counts = Counter()
        saturated, release = asyncio.Event(), asyncio.Event()

        async def command(command, cwd, log_path, timeout, env=None):
            nonlocal active, peak
            request = read_json(cwd / 'request.json')
            heldin = request['task']['id'].startswith('in')
            counts['in' if heldin else 'out'] += 1
            active += 1
            peak = max(peak, active)
            if active == 500:
                saturated.set()
            try:
                await release.wait()
                write_json(cwd / 'result.json', dict(score=int(heldin), status='completed'))
                return 0
            finally:
                active -= 1

        monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', command)
        search = Search(config, store, evaluator=RealEvaluator(config, store))
        job = asyncio.create_task(search.run())
        try:
            await asyncio.wait_for(saturated.wait(), 10)
            assert counts == {'in': 400, 'out': 100}
            release.set()
            assert await job == 'c000000'
        finally:
            release.set()
            await asyncio.gather(job, return_exceptions=True)
        assert peak == 500 and active == 0
        assert store.state['archive']['c000000']['mean_score'] == 1
        out = read_json(store.root / 'evaluations' / store.state['evaluation_keys']['final:heldout:c000000'] / 'evaluation.json')
        assert out['mean_score'] == 0 and out['valid_task_count'] == 100
        assert store.state['rollouts_started'] == 500

    asyncio.run(exercise())


@pytest.mark.parametrize('heldout_valid', [True, False])
def test_each_iteration_reports_heldout_without_selecting_on_it(tmp_path, monkeypatch, heldout_valid):
    config = config_at(tmp_path)
    config.evaluation.parallel_heldout = True
    config.search.iterations = 2
    config.search.recomposition_offspring = 1
    config.search.max_task_rollouts = 100
    store = initialize(config)

    class OppositeHeldout(Evaluator):
        async def evaluate(self, cid, tasks, purpose, key):
            result = await super().evaluate(cid, tasks, purpose, key)
            if purpose == 'heldout':
                for row in result['scores']:
                    row.update(score=int(cid == 'c000000'), status='completed' if heldout_valid else 'infra_error')
                result.update(mean_score=float(cid == 'c000000') if heldout_valid else None,
                              status='completed' if heldout_valid else 'infra_error')
                write_json(self.store.root / 'evaluations' / result['id'] / 'evaluation.json', result)
            return result

    async def validated(*args):
        pass

    monkeypatch.setattr(Search, 'validate_command', validated)
    evaluator, proposer = OppositeHeldout(store), Proposer()
    search = Search(config, store, evaluator, proposer)
    assert heldout_reserve(config, store) == 3
    assert asyncio.run(search.run()) == 'c000003'
    assert store.state['stop_reason'] == 'iterations_completed'
    assert len([c for c in evaluator.calls if c[1] == 'heldout']) == 3
    assert heldout_reserve(config, store) == 0
    archived = json.dumps(store.state['archive'], sort_keys=True)
    report = asyncio.run(search.report_heldout())
    assert [p['iteration'] for p in report['curve']] == [0, 1, 2]
    assert len(report['results']) == 3
    assert len([c for c in evaluator.calls if c[1] == 'heldout']) == 3
    assert json.dumps(store.state['archive'], sort_keys=True) == archived
    heldout_ids = {eid for key, eid in store.state['evaluation_keys'].items() if key.startswith('final:heldout:')}
    for request in proposer.requests:
        assert request['feedback']['id'] not in heldout_ids
        assert all(entry['evaluation_id'] not in heldout_ids for entry in request['history'].values())
