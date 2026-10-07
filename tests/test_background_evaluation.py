import asyncio
import json

import pytest

from pluginrsi.evaluation.runner import Evaluator
from pluginrsi.search.loop import Search
from pluginrsi.search.loop import initialize
from pluginrsi.search.selection import is_valid
from pluginrsi.search.store import read_json, write_json
from test_full_search import config_at


def config(tmp_path, count, ready):
    c = config_at(tmp_path)
    c.seed = tmp_path / 'fixture_seed'
    c.seed.mkdir()
    c.plugin_library = tmp_path / 'fixture_library'
    c.plugin_library.mkdir()
    c.evaluation.feedback_tasks.write_text(''.join(json.dumps(dict(id=f't{i}'))+'\n' for i in range(count)))
    c.evaluation.background_completion = True
    c.evaluation.search_ready_tasks = ready
    c.evaluation.concurrency = count + 1
    c.evaluation.startup_stagger_max_seconds = .001
    c.evaluation.min_valid_ratio = .9
    c.evaluation.evaluation_timeout_seconds = 10
    c.search.max_task_rollouts = count * 4
    return c


@pytest.mark.parametrize('count,ready', [(400,390), (59,55), (150,145)])
def test_threshold_releases_search_without_cancelling_tail(tmp_path, monkeypatch, count, ready):
    c = config(tmp_path, count, ready)
    store = initialize(c)
    tasks = [dict(id=f't{i}') for i in range(count)]

    async def exercise():
        release = asyncio.Event()
        finished = []
        async def command(command, cwd, log_path, timeout, env=None):
            index = int(read_json(cwd/'request.json')['task']['id'][1:])
            if index >= ready:
                await release.wait()
            write_json(cwd/'result.json', dict(status='completed', score=int(index < ready)))
            finished.append(index)
            return 0
        monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', command)
        evaluator = Evaluator(c, store)
        result = await asyncio.wait_for(evaluator.evaluate('c000000', tasks, 'selection', 'seed:selection'), 10)
        assert is_valid(result) and result['status'] == 'search_ready'
        assert result['valid_task_count'] == ready and result['mean_score'] == 1
        assert len(finished) == ready and evaluator._background
        path = store.root/'evaluations'/result['id']
        assert read_json(path/'evaluation.json')['status'] == 'running'
        release.set()
        await evaluator.finish_background()
        final = read_json(path/'evaluation.json')
        assert final['status'] == 'completed' and final['valid_task_count'] == count
        assert final['mean_score'] == ready/count and len(finished) == count
        assert read_json(path/'search_snapshot.json')['mean_score'] == 1
        assert (await evaluator.evaluate('c000000', tasks, 'selection', 'seed:selection'))['mean_score'] == 1
    asyncio.run(exercise())


def test_search_enters_next_phase_while_selection_and_heldout_continue(tmp_path, monkeypatch):
    c = config(tmp_path, 4, 3)
    c.evaluation.parallel_heldout = True
    store = initialize(c)
    async def exercise():
        release = asyncio.Event()
        phase_started = asyncio.Event()
        async def command(command, cwd, log_path, timeout, env=None):
            task = read_json(cwd/'request.json')['task']['id']
            if task in ('t3', 'heldout'):
                await release.wait()
            write_json(cwd/'result.json', dict(score=1, status='completed'))
            return 0
        monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', command)
        evaluator = Evaluator(c, store)
        search = Search(c, store, evaluator=evaluator)
        async def phase(index):
            phase_started.set()
            return True
        search.phase = phase
        job = asyncio.create_task(search.run())
        await asyncio.wait_for(phase_started.wait(), 5)
        assert not release.is_set()
        assert store.state['archive']['c000000']['decision_snapshot']
        release.set()
        assert await asyncio.wait_for(job, 5) == 'c000000'
        assert not evaluator._background
        assert len(store.state['evaluation_keys']) == 2
    asyncio.run(exercise())


def test_background_tail_times_out_without_changing_search_snapshot(tmp_path, monkeypatch):
    c = config(tmp_path, 4, 3)
    c.evaluation.evaluation_timeout_seconds = .15
    store = initialize(c)
    async def exercise():
        cancelled = asyncio.Event()
        async def command(command, cwd, log_path, timeout, env=None):
            task = read_json(cwd/'request.json')['task']['id']
            if task == 't3':
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            write_json(cwd/'result.json', dict(score=1, status='completed'))
            return 0
        monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', command)
        e = Evaluator(c, store)
        result = await e.evaluate('c000000', [dict(id=f't{i}') for i in range(4)], 'selection', 'seed:selection')
        assert result['status'] == 'search_ready'
        await e.finish_background()
        final = read_json(store.root/'evaluations'/result['id']/'evaluation.json')
        assert final['finish_reason'] == 'evaluation_deadline' and cancelled.is_set()
        assert final['scores'][-1]['status'] == 'infra_error'
        assert result['mean_score'] == 1
    asyncio.run(exercise())


def test_infra_errors_do_not_count_toward_release_bar(tmp_path, monkeypatch):
    c = config(tmp_path, 4, 3)
    store = initialize(c)
    async def command(command, cwd, log_path, timeout, env=None):
        task = read_json(cwd/'request.json')['task']['id']
        write_json(cwd/'result.json', dict(score=0, status='completed' if task in ('t0','t1') else 'infra_error'))
        return 0
    monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', command)
    async def exercise():
        e = Evaluator(c, store)
        r = await e.evaluate('c000000', [dict(id=f't{i}') for i in range(4)], 'selection', 'seed:selection')
        assert r['status'] == 'infra_error'
        assert not (store.root/'evaluations'/r['id']/'search_snapshot.json').exists()
        await e.finish_background()
    asyncio.run(exercise())


def test_resume_restores_tail_and_keeps_original_decision(tmp_path, monkeypatch):
    c = config(tmp_path, 4, 3)
    c.evaluation.infra_retry_attempts = 1
    store = initialize(c)
    async def exercise():
        release = asyncio.Event()
        started = asyncio.Event()
        calls = []
        async def command(command, cwd, log_path, timeout, env=None):
            task = read_json(cwd/'request.json')['task']['id']
            calls.append(task)
            if task == 't3':
                started.set()
                await release.wait()
            write_json(cwd/'result.json', dict(score=int(task!='t3'), status='completed'))
            return 0
        monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', command)
        first = Evaluator(c, store)
        decision = await first.evaluate('c000000', [dict(id=f't{i}') for i in range(4)], 'selection', 'seed:selection')
        await started.wait()
        deadline = read_json(store.root/'evaluations'/decision['id']/'evaluation.json')['background_deadline_at']
        await first.finish_background(cancel=True)
        second = Evaluator(c, store, retry_infra=True)
        await second.restore_background()
        release.set()
        await asyncio.wait_for(second.finish_background(), 5)
        record = read_json(store.root/'evaluations'/decision['id']/'evaluation.json')
        assert record['valid_task_count'] == 4 and record['mean_score'] == .75
        assert record['background_deadline_at'] == deadline
        assert calls.count('t0') == 1 and calls.count('t3') == 2
        assert read_json(store.root/'evaluations'/decision['id']/'search_snapshot.json')['mean_score'] == 1
    asyncio.run(exercise())


def test_local_batch_releases_at_quality_ratio_and_does_not_cancel_tail(tmp_path, monkeypatch):
    c = config(tmp_path, 20, 19)
    c.evaluation.min_valid_ratio = .95
    c.evaluation.accept_partial_results = True
    store = initialize(c)
    async def exercise():
        release = asyncio.Event()
        tail_done = asyncio.Event()
        async def command(command, cwd, log_path, timeout, env=None):
            task = read_json(cwd/'request.json')['task']['id']
            if task == 't19':
                await release.wait()
                tail_done.set()
            write_json(cwd/'result.json', dict(score=1, status='completed'))
            return 0
        monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', command)
        e = Evaluator(c, store)
        r = await e.evaluate('c000000', [dict(id=f't{i}') for i in range(20)], 'screening', 'local:c000000')
        assert r['valid_task_count'] == 19 and not tail_done.is_set()
        release.set()
        await e.finish_background()
        assert tail_done.is_set()
    asyncio.run(exercise())
