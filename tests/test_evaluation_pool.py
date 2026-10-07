import asyncio
from collections import Counter

from pluginrsi.evaluation.runner import Evaluator
from pluginrsi.runtime import BudgetExceeded
from pluginrsi.search.loop import initialize
from pluginrsi.search.store import read_json, write_json
from test_full_search import config_at


def test_candidates_share_128_slots_and_refill_through_retries(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    config.evaluation.concurrency = 128
    config.evaluation.infra_retry_attempts = 1
    config.search.max_task_rollouts = 200
    store = initialize(config)
    evaluator = Evaluator(config, store)
    active = peak = 0
    candidates = Counter()
    attempts = Counter()

    async def exercise():
        nonlocal active, peak
        saturated, release = asyncio.Event(), asyncio.Event()

        async def command(command, cwd, log_path, timeout, env=None):
            nonlocal active, peak
            request = read_json(cwd / "request.json")
            cid, tid = request["harness"], request["task"]["id"]
            attempts[cid, tid] += 1
            candidates[cid] += 1
            active += 1
            peak = max(peak, active)
            if active == 128:
                saturated.set()
            try:
                await release.wait()
                failed = tid == "t0" and cid.endswith("c000000/harness") and attempts[cid, tid] == 1
                write_json(cwd / "result.json", {"score": 1, "status": "infra_error" if failed else "completed"})
                return 0
            finally:
                active -= 1

        monkeypatch.setattr("pluginrsi.evaluation.runner.run_command", command)
        tasks = [{"id": f"t{i}"} for i in range(20)]
        jobs = [asyncio.create_task(evaluator.evaluate(f"c{i:06d}", tasks, "screening", f"pool:{i}")) for i in range(8)]
        try:
            await asyncio.wait_for(saturated.wait(), 5)
            assert active == 128
            assert len(candidates) >= 7
            assert store.state["rollouts_started"] == 128
            release.set()
            results = await asyncio.gather(*jobs)
        finally:
            release.set()
            await asyncio.gather(*jobs, return_exceptions=True)
        assert all(r["status"] == "completed" and r["valid_task_count"] == 20 for r in results)

    asyncio.run(exercise())
    assert peak == 128 and active == 0
    assert store.state["rollouts_started"] == 161
    assert evaluator._reserved_rollouts == 0


def test_parallel_reservations_preserve_complete_batches_and_heldout(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    config.evaluation.concurrency = 2
    config.search.max_task_rollouts = 6
    store = initialize(config)
    evaluator = Evaluator(config, store)
    calls = []

    async def command(command, cwd, log_path, timeout, env=None):
        calls.append(read_json(cwd / "request.json")["harness"])
        await asyncio.sleep(0)
        write_json(cwd / "result.json", {"score": 1, "status": "completed"})
        return 0

    monkeypatch.setattr("pluginrsi.evaluation.runner.run_command", command)

    async def exercise():
        tasks = [{"id": f"t{i}"} for i in range(4)]
        return await asyncio.gather(
            evaluator.evaluate("c000000", tasks, "screening", "first"),
            evaluator.evaluate("c000001", tasks, "screening", "second"),
            return_exceptions=True,
        )

    results = asyncio.run(exercise())
    assert results[0]["status"] == "completed"
    assert isinstance(results[1], BudgetExceeded)
    assert len(calls) == 4 and all(c.endswith("c000000/harness") for c in calls)
    assert store.state["rollouts_started"] == 4
    assert evaluator._reserved_rollouts == 0


def test_cancelled_pool_releases_queued_budget_and_resumes(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    config.evaluation.concurrency = 2
    config.evaluation.infra_retry_attempts = 1
    store = initialize(config)
    evaluator = Evaluator(config, store)
    active = 0

    async def exercise():
        nonlocal active
        saturated, release = asyncio.Event(), asyncio.Event()

        async def command(command, cwd, log_path, timeout, env=None):
            nonlocal active
            active += 1
            if active == 2:
                saturated.set()
            try:
                await release.wait()
                write_json(cwd / "result.json", {"score": 1, "status": "completed"})
                return 0
            finally:
                active -= 1

        monkeypatch.setattr("pluginrsi.evaluation.runner.run_command", command)
        tasks = [{"id": f"t{i}"} for i in range(5)]
        jobs = [asyncio.create_task(evaluator.evaluate(f"c{i:06d}", tasks, "screening", f"pool:{i}")) for i in range(2)]
        await asyncio.wait_for(saturated.wait(), 5)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        assert active == 0 and evaluator._reserved_rollouts == 0
        assert store.state["rollouts_started"] == 2
        release.set()
        results = await asyncio.gather(*(evaluator.evaluate(f"c{i:06d}", tasks, "screening", f"pool:{i}") for i in range(2)))
        assert all(r["status"] == "completed" for r in results)
        assert store.state["rollouts_started"] == 12
        assert active == 0 and evaluator._reserved_rollouts == 0

    asyncio.run(exercise())
