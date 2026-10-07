import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml

from pluginrsi.runtime import BudgetExceeded, InfraError
from pluginrsi.schemas import RunConfig
from pluginrsi.search.loop import Search
from pluginrsi.search.mutation import balanced_batch, validate_local_surface
from pluginrsi.search.loop import initialize
from pluginrsi.search.store import Store, read_json, write_json
from pluginrsi.search.selection import apply_quality_policy, is_valid

ROOT = Path(__file__).resolve().parents[1]


def config_at(tmp_path):
    data = tmp_path / "heldin.jsonl"
    data.write_text("".join(json.dumps({"id": f"t{i}"}) + "\n" for i in range(4)))
    heldout = tmp_path / "heldout.jsonl"
    heldout.write_text('{"id":"heldout"}\n')
    return RunConfig(seed=ROOT / "seeds/agents/swe_bench_verified/v0001", plugin_library=ROOT / "seeds/plugin_library",
        run_dir=tmp_path / "run", search={"iterations": 1,
            "offspring_per_phase": 2, "feedback_batch_size": 2, "local_failure_tasks": 1,
            "recomposition_offspring": 2, "max_task_rollouts": 30},
        evaluation={"benchmark": "fixture", "feedback_tasks": data, "selection_tasks": data, "heldout_tasks": heldout},
        solver={"model": "fixture"}, proposer={"command": ["unused"]})


class Evaluator:
    retry_infra = False

    def __init__(self, store, interrupt=None):
        self.store = store
        self.calls = []
        self.interrupt = interrupt

    async def evaluate(self, cid, tasks, purpose, key):
        if key == self.interrupt:
            self.interrupt = None
            raise InterruptedError("fixture interruption")
        state = self.store.state
        if key in state["evaluation_keys"]:
            return read_json(self.store.root / "evaluations" / state["evaluation_keys"][key] / "evaluation.json")
        eid = self.store.allocate("evaluation")
        state["evaluation_keys"][key] = eid
        state["rollouts_started"] += len(tasks)
        self.store.save()
        self.calls.append((cid, purpose, [t["id"] for t in tasks]))
        rows = []
        for i, task in enumerate(tasks):
            score = int(i % 2 == 0) if cid == "c000000" else 1
            rows.append(dict(task_id=task["id"], score=score, status="completed", trajectory=f"{task['id']}.jsonl"))
        result = dict(id=eid, candidate_id=cid, task_ids=[t["id"] for t in tasks], scores=rows,
                      mean_score=sum(r["score"] for r in rows)/len(rows), status="completed", attempts={}, rollouts_started=len(tasks))
        write_json(self.store.root / "evaluations" / eid / "evaluation.json", result)
        return result


class Proposer:
    def __init__(self):
        self.requests = []
        self.active = 0
        self.peak = 0

    async def propose(self, request):
        self.requests.append(request)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        refs = []
        child = Path(request["output_dir"])
        if "local_contract" in request:
            source = request["local_contract"]["source_ref"]
            ref = source.rsplit("/", 1)[0] + "/" + request["version"]
            root = next(Path(p) for p in request["library_dirs"] if (Path(p) / source).exists())
            target = child / "new_plugins" / ref
            shutil.copytree(root / source, target, dirs_exist_ok=True)
            manifest = yaml.safe_load((target / "plugin.yaml").read_text())
            manifest["version"] = request["version"]
            (target / "plugin.yaml").write_text(yaml.safe_dump(manifest))
            harness = yaml.safe_load((child / "harness/harness.yaml").read_text())
            harness["plugins"][request["local_contract"]["alias"]]["ref"] = ref
            (child / "harness/harness.yaml").write_text(yaml.safe_dump(harness))
            refs = [ref]
        else:
            with (child / "harness/workflow.py").open("a") as stream:
                stream.write("\n")
        return dict(hypothesis="fixture", changes=["fixture"], new_plugin_refs=refs)


@pytest.fixture(autouse=True)
def package_path(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))


def test_mutation_publishes_then_recomposition_ranks_and_resumes(tmp_path):
    config = config_at(tmp_path)
    store = initialize(config)
    evaluator, proposer = Evaluator(store), Proposer()
    search = Search(config, store, evaluator, proposer)
    assert asyncio.run(search.run()) == "c000003"
    assert proposer.peak == 2
    local = read_json(store.root / "iterations/iter_0001/plugin_mutation.json")
    published = [p["ref"] for p in local["published_plugins"]]
    assert len(published) == 2 and len(local["slots"]) == 2
    assert all(s["retained"] for s in local["slots"])
    assert proposer.requests[0]["allowed_plugin_refs"] == proposer.requests[1]["allowed_plugin_refs"]
    assert not set(published) & set(proposer.requests[0]["allowed_plugin_refs"])
    assert proposer.requests[2]["local_evidence"][0]["id"] == "c000001"
    assert set(published) <= set(proposer.requests[2]["allowed_plugin_refs"])
    assert len([c for c in evaluator.calls if c[1] == "selection"]) == 3
    assert all(len(c[2]) == 4 for c in evaluator.calls if c[1] == "selection")
    assert set(store.state["archive"]) == {"c000000", "c000003", "c000004"}
    assert store.state["rollouts_started"] == 16
    assert asyncio.run(Search(config, store, evaluator, proposer).run()) == "c000003"
    assert len(proposer.requests) == 4
    before = json.dumps(store.state["archive"], sort_keys=True)
    asyncio.run(search.report_heldout())
    assert before == json.dumps(store.state["archive"], sort_keys=True)


def test_candidate_error_rejects_recomposition_and_continues_search(tmp_path):
    config = config_at(tmp_path)
    config.search.iterations = 2
    config.search.max_task_rollouts = 100
    config.search.offspring_per_phase = 1
    config.search.recomposition_offspring = 1
    store = initialize(config)

    class InvalidWorkflowEvaluator(Evaluator):
        async def evaluate(self, cid, tasks, purpose, key):
            result = await super().evaluate(cid, tasks, purpose, key)
            if key.startswith('full:'):
                result.update(status='candidate_error', mean_score=None)
                for row in result['scores']:
                    row.update(status='candidate_error', score=0.0)
                write_json(self.store.root / 'evaluations' / result['id'] / 'evaluation.json', result)
            return result

    evaluator = InvalidWorkflowEvaluator(store)
    assert asyncio.run(Search(config, store, evaluator, Proposer()).run()) == 'c000000'
    assert store.state['stop_reason'] == 'iterations_completed'
    assert set(store.state['archive']) == {'c000000'}
    for iteration in (1, 2):
        record = read_json(store.root / f'iterations/iter_{iteration:04d}/harness_recomposition.json')
        assert record['completed']
        assert all(slot['status'] == 'rejected' and slot['reason'] == 'invalid_evaluation'
                   for slot in record['slots'])


def test_resume_inside_local_evaluation_reuses_proposals(tmp_path):
    config = config_at(tmp_path)
    store = initialize(config)
    evaluator, proposer = Evaluator(store, interrupt="local:c000002"), Proposer()
    with pytest.raises(InterruptedError):
        asyncio.run(Search(config, store, evaluator, proposer).run())
    assert len(proposer.requests) == 2
    batches = [b["batch_task_ids"] for b in read_json(store.root / "iterations/iter_0001/plugin_mutation.json")["branches"]]
    config.search.random_seed += 100
    resumed = Store(store.root)
    evaluator.store = resumed
    assert asyncio.run(Search(config, resumed, evaluator, proposer).run()) == "c000003"
    assert len(proposer.requests) == 4
    assert resumed.state["rollouts_started"] == 16
    assert [b["batch_task_ids"] for b in read_json(store.root / "iterations/iter_0001/plugin_mutation.json")["branches"]] == batches


def test_candidate_evaluations_overlap_without_changing_tie_ranking(tmp_path):
    config = config_at(tmp_path)
    store = initialize(config)

    class OverlappingEvaluator(Evaluator):
        active = 0
        peaks = {}
        finished = []
        arrived = {}

        async def evaluate(self, cid, tasks, purpose, key):
            if cid == "c000000":
                return await super().evaluate(cid, tasks, purpose, key)
            self.active += 1
            self.peaks[purpose] = max(self.peaks.get(purpose, 0), self.active)
            try:
                # Barrier: both candidates of a wave must be in flight at the same time.
                event = self.arrived.setdefault(purpose, asyncio.Event())
                if self.peaks[purpose] == 2:
                    event.set()
                await asyncio.wait_for(event.wait(), 60)
                await asyncio.sleep(0.05 if cid == "c000003" else 0)
                result = await super().evaluate(cid, tasks, purpose, key)
                self.finished.append(cid)
                return result
            finally:
                self.active -= 1

    evaluator = OverlappingEvaluator(store)
    assert asyncio.run(Search(config, store, evaluator, Proposer()).run()) == "c000003"
    assert evaluator.peaks == {"screening": 2, "selection": 2}
    assert evaluator.finished.index("c000004") < evaluator.finished.index("c000003")
    assert evaluator.active == 0


def test_failed_evaluation_wave_cancels_and_joins_other_candidates(tmp_path):
    config = config_at(tmp_path)
    search = Search(config, initialize(config))

    async def exercise():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def evaluate(slot):
            if slot["id"] == "first":
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            await started.wait()
            raise InterruptedError("fixture interruption")

        with pytest.raises(InterruptedError):
            await search.evaluation_wave([{"id": cid, "status": "ready"} for cid in ("first", "second")], evaluate)
        assert cancelled.is_set()

    asyncio.run(exercise())


def test_local_surface_rejects_workflow_and_other_config_changes(tmp_path):
    parent, child = tmp_path / "parent", tmp_path / "child"
    shutil.copytree(ROOT / "seeds/agents/swe_bench_verified/v0001", parent)
    shutil.copytree(parent, child)
    ref = "role/coder/v_c000001"
    manifest = yaml.safe_load((child / "harness.yaml").read_text())
    manifest["plugins"]["coder"]["ref"] = ref
    (child / "harness.yaml").write_text(yaml.safe_dump(manifest))
    validate_local_surface(parent, child, "coder", ref)
    manifest["plugins"]["terminal"]["config"]["timeout_seconds"] = 1
    (child / "harness.yaml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="assigned alias"):
        validate_local_surface(parent, child, "coder", ref)
    (child / "workflow.py").write_text("changed")
    with pytest.raises(ValueError, match="workflow"):
        validate_local_surface(parent, child, "coder", ref)


def test_full_splits_keep_heldout_disjoint(tmp_path):
    config = config_at(tmp_path)
    config.evaluation.heldout_tasks = config.evaluation.feedback_tasks
    with pytest.raises(ValueError, match="overlap"):
        initialize(config)


def test_batch_shortage_fills_without_duplicates():
    tasks = [{"id": str(i)} for i in range(6)]
    scores = {str(i): int(i != 0) for i in range(6)}
    chosen = balanced_batch(tasks, scores, 4, 2, 42)
    assert len({t["id"] for t in chosen}) == 4
    assert "0" in [t["id"] for t in chosen]
    assert chosen == balanced_batch(tasks, scores, 4, 2, 42)


def test_holdout_budget_is_reserved(tmp_path):
    config = config_at(tmp_path)
    store = initialize(config)
    evaluator = Evaluator(store)
    search = Search(config, store, evaluator, Proposer())
    store.state["rollouts_started"] = 27
    with pytest.raises(BudgetExceeded, match="reserved"):
        asyncio.run(search.checked_evaluate("c000000", search.feedback, "feedback", "new"))
    assert not evaluator.calls


def test_full_splits_must_match_in_order(tmp_path):
    config = config_at(tmp_path)
    other = tmp_path / "reordered.jsonl"
    other.write_text("\n".join(reversed(config.evaluation.feedback_tasks.read_text().splitlines())) + "\n")
    config.evaluation.selection_tasks = other
    with pytest.raises(ValueError, match="identical ordered"):
        initialize(config)


def test_completed_local_evidence_uses_new_phase_format(tmp_path):
    from pluginrsi.search.feedback import published_plugin_evidence
    write_json(tmp_path / "iterations/iter_0001/plugin_mutation.json", {
        "completed": True,
        "published_plugins": [{"ref": "role/coder/v_c000001", "candidate_id": "c000001"}],
        "plugin_evidence": [{"id": "c000001", "matched_gain": 0.1}]})
    request = {"feedback_dir": str(tmp_path / "evaluations/e000002"), "allowed_plugin_refs": ["role/coder/v_c000001"]}
    assert published_plugin_evidence(request)[0]["local_results"][0]["matched_gain"] == 0.1
    request["allowed_plugin_refs"] = []
    assert published_plugin_evidence(request) == []


def test_invalid_local_wave_stops_before_expensive_recomposition(tmp_path):
    config = config_at(tmp_path)
    store = initialize(config)
    evaluator = Evaluator(store)
    class BrokenProposer:
        async def propose(self, request):
            raise ValueError("fixture delivery error")
    asyncio.run(Search(config,store,evaluator,BrokenProposer()).run())
    assert store.state["stop_reason"] == "all_local_proposals_invalid"
    assert len([c for c in evaluator.calls if c[1] == "selection"]) == 1


@pytest.mark.parametrize("failed_phase", ["plugin_mutation", "harness_recomposition"])
def test_infra_failed_proposal_drops_branch_or_blocks_recomposition(tmp_path, failed_phase):
    config = config_at(tmp_path)
    store = initialize(config)
    evaluator = Evaluator(store)

    class FailingProposer(Proposer):
        async def propose(self, request):
            if request["phase"] == failed_phase and request["candidate_id"] in ("c000001", "c000003"):
                raise InfraError("fixture transport unavailable")
            return await super().propose(request)

    asyncio.run(Search(config, store, evaluator, FailingProposer()).run())
    record = read_json(store.root / "iterations/iter_0001" / f"{failed_phase}.json")
    assert record["slots"][0]["status"] == "infra_error"
    if failed_phase == "plugin_mutation":
        # Mutation branches are independent: an infra failure drops only its own branch.
        assert record["completed"]
        assert [p["candidate_id"] for p in record["published_plugins"]] == ["c000002"]
        assert [call[0] for call in evaluator.calls if call[1] == "screening"] == ["c000002"]
    else:
        assert not record["completed"]
        assert store.state["stop_reason"] == "proposal_infra_error"
        assert set(store.state["archive"]) == {"c000000"}
        assert len([call for call in evaluator.calls if call[1] == "selection"]) == 1


@pytest.mark.parametrize("purpose,total,valid", [
    ("feedback", 20, 19), ("screening", 20, 19),
    ("selection", 100, 95), ("selection", 100, 99),
    ("selection", 100, 100), ("selection", 100, 94),
])
def test_quality_threshold_allows_different_valid_task_sets(tmp_path, purpose, total, valid):
    config = config_at(tmp_path)
    config.evaluation.min_valid_ratio = 0.95
    config.search.max_task_rollouts = 1000
    store = initialize(config)
    store.state["archive"]["c000000"] = {"per_task_scores": {f"t{i}": 0 for i in range(99)}}

    class PartialEvaluator(Evaluator):
        async def evaluate(self, *args):
            result = await super().evaluate(*args)
            for row in result["scores"][valid:]:
                row["status"] = "infra_error"
            return apply_quality_policy(result, config.evaluation.min_valid_ratio)

    search = Search(config, store, PartialEvaluator(store), Proposer())
    tasks = [{"id": f"t{i}"} for i in range(total)]
    result = asyncio.run(search.checked_evaluate("c000001", tasks, purpose, "partial"))
    assert is_valid(result) == (valid / total >= 0.95)
    assert result["mean_score"] == (1.0 if valid / total >= 0.95 else None)
    assert "infrastructure_gate" not in result


def test_cached_evaluation_does_not_reserve_unrequested_retry_rollouts(tmp_path):
    config = config_at(tmp_path)
    store = initialize(config)
    evaluator = Evaluator(store)
    evaluator.retry_infra = True
    search = Search(config, store, evaluator, Proposer())
    first = asyncio.run(evaluator.evaluate("c000000", search.feedback, "feedback", "cached"))
    config.search.max_task_rollouts = store.state["rollouts_started"] + 2
    result = asyncio.run(search.checked_evaluate("c000000", search.feedback, "feedback", "cached"))
    assert result["id"] == first["id"]
    assert len(evaluator.calls) == 1
