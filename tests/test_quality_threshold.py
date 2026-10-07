import asyncio
import json
from pathlib import Path

import pytest

from pluginrsi.evaluation.runner import Evaluator
from pluginrsi.schemas import RunConfig
from pluginrsi.search.loop import initialize
from pluginrsi.search.selection import apply_quality_policy, is_valid
from pluginrsi.search.store import write_json, read_json

ROOT = Path(__file__).resolve().parents[1]


def config_at(tmp_path, offspring=2):
    tasks = tmp_path / "heldin.jsonl"
    tasks.write_text("".join(json.dumps({"id": tid}) + "\n" for tid in ("s1", "s2", "s3")))
    return RunConfig(seed=ROOT / "seeds/agents/swe_bench_verified/v0001", plugin_library=ROOT / "seeds/plugin_library",
        run_dir=tmp_path / "run", search={"iterations": 1, "offspring_per_phase": offspring, "feedback_batch_size": 2},
        evaluation={"benchmark": "fixture", "feedback_tasks": tasks, "selection_tasks": tasks},
        solver={"model": "test"}, proposer={"command": ["unused"]})


def result(valid, total=100):
    return {"status":"infra_error", "scores":[{"task_id":str(i), "score":int(i % 2 == 0),
            "status":"completed" if i < valid else "infra_error"} for i in range(total)]}


@pytest.mark.parametrize("valid,accepted", [(94,False),(95,True),(99,True),(100,True)])
def test_threshold_boundary_and_valid_denominator(valid,accepted):
    r = apply_quality_policy(result(valid), 0.95)
    assert is_valid(r) == accepted
    assert r["valid_task_count"] == valid
    if accepted:
        assert r["mean_score"] == sum(i % 2 == 0 for i in range(valid))/valid
    else:
        assert r["mean_score"] is None


def test_candidate_errors_not_hidden_by_threshold():
    r = result(99)
    r["scores"][-1]["status"] = "candidate_error"
    assert not is_valid(apply_quality_policy(r,0.95))


def test_reuse_terminal_infra_evaluation_without_another_retry(tmp_path):
    cfg = config_at(tmp_path)
    cfg.evaluation.min_valid_ratio = 0.95
    store = initialize(cfg)
    store.state["evaluation_keys"]["test"] = "e000000"
    r = result(99)
    r.update(id="e000000",candidate_id="c000000",task_ids=[str(i) for i in range(100)], attempts={}, mean_score=None)
    path = store.root / "evaluations/e000000/evaluation.json"
    write_json(path,r)
    evaluated = asyncio.run(Evaluator(cfg,store,retry_infra=True).evaluate("c000000",[{"id":str(i)} for i in range(100)],"selection","test"))
    assert is_valid(evaluated)
    assert store.state["rollouts_started"] == 0
    assert read_json(path.with_name("evaluation.before-quality-policy.json"))["status"] == "infra_error"
