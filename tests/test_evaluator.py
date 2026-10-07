import asyncio
import json
from pathlib import Path

import pytest

from pluginrsi.evaluation.runner import Evaluator
from pluginrsi.runtime import BudgetExceeded
from pluginrsi.schemas import RunConfig
from pluginrsi.search.loop import initialize
from pluginrsi.search.store import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


def config_at(tmp_path, offspring=2):
    tasks = tmp_path / "heldin.jsonl"
    tasks.write_text("".join(json.dumps({"id": tid}) + "\n" for tid in ("s1", "s2", "s3")))
    return RunConfig(seed=ROOT / "seeds/agents/swe_bench_verified/v0001", plugin_library=ROOT / "seeds/plugin_library",
        run_dir=tmp_path / "run", search={"iterations": 1, "offspring_per_phase": offspring, "feedback_batch_size": 2},
        evaluation={"benchmark": "fixture", "feedback_tasks": tasks, "selection_tasks": tasks},
        solver={"model": "test"}, proposer={"command": ["unused"]})


def test_evaluation_resume_keeps_completed_tasks_and_counts_attempts(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    store = initialize(config)
    calls = []

    async def command(command, cwd, log_path, timeout, env=None):
        request = read_json(cwd / "request.json")
        calls.append(request["task"]["id"])
        write_json(cwd / "result.json", {"score": 1, "status": "completed"})
        if len(calls) == 1:
            raise InterruptedError("worker finished before controller interruption")
        return 0

    monkeypatch.setattr("pluginrsi.evaluation.runner.run_command", command)
    evaluator = Evaluator(config, store)
    tasks = [{"id": "one"}, {"id": "two"}]
    with pytest.raises(InterruptedError):
        asyncio.run(evaluator.evaluate("c000000", tasks, "selection", "key"))
    result = asyncio.run(evaluator.evaluate("c000000", tasks, "selection", "key"))
    assert result["mean_score"] == 1
    assert calls == ["one", "two"]
    assert store.state["rollouts_started"] == 2
    asyncio.run(evaluator.evaluate("c000000", tasks, "selection", "key"))
    assert len(calls) == 2
    assert len(result["scores"]) == 2


def test_budget_reserves_whole_evaluation_before_first_task(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    config.search.max_task_rollouts = 1
    store = initialize(config)
    with pytest.raises(BudgetExceeded):
        asyncio.run(Evaluator(config, store).evaluate("c000000", [{"id": "one"}, {"id": "two"}], "selection", "key"))
    assert store.state["rollouts_started"] == 0


def test_infra_error_is_not_a_comparable_partial_mean(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    store = initialize(config)

    async def command(command, cwd, log_path, timeout, env=None):
        task_id = read_json(cwd / "request.json")["task"]["id"]
        write_json(cwd / "result.json", {"score": 1 if task_id == "one" else 0,
                                       "status": "completed" if task_id == "one" else "infra_error"})
        return 0

    monkeypatch.setattr("pluginrsi.evaluation.runner.run_command", command)
    result = asyncio.run(Evaluator(config, store).evaluate("c000000", [{"id": "one"}, {"id": "two"}], "selection", "key"))
    assert result["mean_score"] is None
    assert result["status"] == "infra_error"
    assert len(result["scores"]) == 2


@pytest.mark.parametrize("recovers", [True, False])
def test_automatic_infra_retries_preserve_results_and_are_bounded(tmp_path, monkeypatch, recovers):
    config = config_at(tmp_path)
    config.evaluation.infra_retry_attempts = 2
    store = initialize(config)
    calls = []

    async def command(command, cwd, log_path, timeout, env=None):
        tid = read_json(cwd / "request.json")["task"]["id"]
        calls.append(tid)
        failed = tid == "two" and (not recovers or calls.count(tid) < 2)
        write_json(cwd / "result.json", {"score": 0, "status": "infra_error" if failed else "completed"})
        return 0

    monkeypatch.setattr("pluginrsi.evaluation.runner.run_command", command)
    result = asyncio.run(Evaluator(config, store).evaluate("c000000", [{"id": "one"}, {"id": "two"}], "selection", "key"))
    assert calls == (["one", "two", "two"] if recovers else ["one", "two", "two", "two"])
    assert store.state["rollouts_started"] == len(calls)
    assert result["status"] == ("completed" if recovers else "infra_error")
    assert read_json(store.root / "evaluations" / result["id"] / "tasks/two/attempt_1/result.json")["status"] == "infra_error"
