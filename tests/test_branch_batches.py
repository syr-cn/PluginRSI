import asyncio
import json
from pathlib import Path

import pytest

from pluginrsi.search.mutation import balanced_batch, create_branches
from pluginrsi.search.feedback import published_plugin_evidence
from pluginrsi.search.loop import Search, initialize
from pluginrsi.search.selection import apply_quality_policy, valid_scores
from pluginrsi.search.store import read_json, write_json
from test_full_search import Evaluator, Proposer, config_at


def test_independent_fixed_batches_reuse_scores_traces_and_retained_results(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    config.evaluation.feedback_tasks.write_text(''.join(json.dumps({'id': f't{i}'}) + '\n' for i in range(40)))
    config.search.feedback_batch_size = 8
    config.search.local_failure_tasks = 4
    config.search.offspring_per_phase = 3
    config.search.max_task_rollouts = 300
    config.evaluation.min_valid_ratio = 0.95

    async def validate_command(*args):
        return 0

    monkeypatch.setattr('pluginrsi.search.loop.run_command', validate_command)
    store = initialize(config)

    class TraceEvaluator(Evaluator):
        async def evaluate(self, *args):
            result = await super().evaluate(*args)
            if result['candidate_id'] == 'c000000':
                result['scores'][-1]['status'] = 'infra_error'
                result = apply_quality_policy(result, 0.95)
                write_json(store.root / 'evaluations' / result['id'] / 'evaluation.json', result)
            for row in result['scores']:
                (store.root / 'evaluations' / result['id'] / row['trajectory']).write_text('{}\n')
            return result

    evaluator, proposer = TraceEvaluator(store), Proposer()
    search = Search(config, store, evaluator, proposer)
    asyncio.run(search.run())
    local = read_json(store.root / 'iterations/iter_0001/plugin_mutation.json')
    full = search.result(local['parent_evaluation'])
    scores = valid_scores(full)
    assert len({tuple(b['batch_task_ids']) for b in local['branches']}) == 3
    assert not any(call[1] == 'feedback' for call in evaluator.calls)
    assert store.state['rollouts_started'] == 40 + 3 * 8 + 2 * 40
    for i, branch in enumerate(local['branches']):
        ids = branch['batch_task_ids']
        expected = balanced_batch([t for t in search.feedback if t['id'] in scores], scores, 8, 4, f'42:1:{i}')
        assert ids == [t['id'] for t in expected]
        assert len(ids) == len(set(ids)) == 8
        assert 't39' not in ids
        assert sum(scores[tid] == 0 for tid in ids) == 4
        slots = [s for s in local['slots'] if s['branch'] == i]
        assert all(s['batch_task_ids'] == ids for s in slots)
        assert [s['retained'] for s in slots] == [True]
        assert slots[0]['parent_evaluation_id'] == full['id']
        for slot in slots:
            request = read_json(store.candidate(slot['id']) / 'proposal_request.json')
            assert request['feedback']['task_ids'] == ids
            assert request['feedback']['mean_score'] == slot['parent_mean']
            assert [r['task_id'] for r in request['feedback']['scores']] == ids
            assert Path(request['feedback_dir']).name == slot['parent_evaluation_id']
            assert all((Path(request['feedback_dir']) / r['trajectory']).exists() for r in request['feedback']['scores'])
    full_request = next(r for r in proposer.requests if r['phase'] == 'harness_recomposition')
    assert all('batch_task_ids' in s and 'parent_evaluation_id' in s for s in full_request['local_evidence'])
    assert any(k.startswith('evidence/c000000/') for k in full_request['extra_readable_files'])
    evidence = published_plugin_evidence(full_request)
    assert len(evidence) == 3
    for item in evidence:
        assert item['batch_task_ids'] == local['branches'][item['branch']]['batch_task_ids']
        assert len(item['local_results']) == 1
        assert all(s['branch'] == item['branch'] for s in item['local_results'])
    assert read_json(store.root / 'iteration_usage.json')['totals']['solver']['rollouts'] == 144


def test_branch_projection_excludes_unselected_tasks_and_preserves_source(tmp_path):
    search = Search.__new__(Search)
    search.config = config_at(tmp_path)
    source = dict(id='e0', status='completed', scores=[
        dict(task_id='a', score=0, status='completed', trajectory='a.jsonl'),
        dict(task_id='b', score=1, status='completed', trajectory='b.jsonl')], mean_score=0.5)
    before = json.dumps(source)
    search.result = lambda eid: source
    result = search.branch_feedback(dict(evaluation_id='e0', batch_task_ids=['b']))
    assert result['mean_score'] == 1
    assert result['id'] == 'e0' and result['rollouts_started'] == 0
    assert [r['trajectory'] for r in result['scores']] == ['b.jsonl']
    assert json.dumps(source) == before


def test_insufficient_valid_parent_tasks_does_not_shrink_batch(tmp_path):
    search = Search.__new__(Search)
    search.config = config_at(tmp_path)
    search.feedback = [{'id': 't0'}, {'id': 't1'}]
    search.result = lambda eid: dict(status='completed', min_valid_ratio=0.5, scores=[
        dict(task_id='t0', score=0, status='completed'),
        dict(task_id='t1', score=0, status='infra_error')])
    with pytest.raises(ValueError, match='insufficient valid tasks'):
        create_branches(search, {'parent_evaluation': 'e0'})
