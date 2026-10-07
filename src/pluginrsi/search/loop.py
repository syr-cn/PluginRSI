"""PluginRSI optimization loop (Algorithm 1).

Every iteration runs two stages against the incumbent harness H = H(W, P):
  1. plugin mutation (``mutation.py``): N branches each mutate one plugin on a balanced
     minibatch; strictly improving variants are published to the plugin library L.
  2. harness recomposition: the proposer selects plugins P' from L and revises the
     workflow W'; H(W', P') is evaluated on the full held-in set and replaces the
     incumbent only if it scores strictly higher.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import yaml

from ..evaluation.runner import Evaluator, heldout_reserve, quality_threshold, read_tasks, retryable_infra
from ..loader import library_refs, tree_contents
from ..runtime import BudgetExceeded, InfraError
from ..schemas import HarnessManifest, RunConfig, read_yaml
from .mutation import plugin_mutation, validate_local_surface
from .proposer import CommandProposer, prepare_local_plugin, run_command, validate_proposal
from .selection import apply_quality_policy, is_valid, top_w, valid_scores
from .store import Store, read_json, snapshot, write_json
from .usage import write_iteration_usage

PHASES = ("plugin_mutation", "harness_recomposition")
PROMPTS = Path(__file__).parents[1] / "prompts"
BUDGET_STOP_REASONS = {
    'search budget exhausted; held-out allowance reserved',
    'insufficient rollout budget for a complete evaluation',
    'insufficient rollout budget for infrastructure retries',
}


def initialize(config: RunConfig) -> Store:
    store = Store(config.run_dir)
    if store.path.exists():
        raise ValueError("run already exists; use resume")
    splits = {name: read_tasks(path) for name, path in (
        ("feedback", config.evaluation.feedback_tasks), ("selection", config.evaluation.selection_tasks),
        ("heldout", config.evaluation.heldout_tasks)) if path is not None}
    if splits["feedback"] != splits["selection"]:
        raise ValueError("feedback and selection must be the identical ordered held-in dataset")
    if "heldout" in splits and {t["id"] for t in splits["heldout"]} & {t["id"] for t in splits["selection"]}:
        raise ValueError("task splits overlap: held-in, held-out")
    if config.search.feedback_batch_size > len(splits["feedback"]):
        raise ValueError("feedback_batch_size exceeds feedback dataset")
    if config.evaluation.benchmark == 'qa_transfer':
        from ..evaluation.qa_data import inspect_qa_inputs
        inspect_qa_inputs(config.evaluation.benchmark_options, splits)
    store.root.mkdir(parents=True, exist_ok=True)
    snapshot(config.plugin_library, store.root / "initial_plugins")
    (store.root / "plugin_library").mkdir(exist_ok=True)
    snapshot(config.seed, store.candidate("c000000") / "harness")
    write_json(store.candidate("c000000") / "candidate.yaml", dict(schema_version=1, id="c000000", parent_id=None,
               iteration=0, stage="seed", harness_path="harness", new_plugins=[], hypothesis="seed", changes=[]))
    for name, tasks in splits.items():
        path = store.root / "datasets" / f"{name}.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text("".join(json.dumps(task) + "\n" for task in tasks))
        setattr(config.evaluation, f"{name}_tasks", path)
    if config.evaluation.benchmark == 'qa_transfer':
        from ..evaluation.qa_data import freeze_qa_inputs
        freeze_qa_inputs(config.evaluation.benchmark_options, store.root / 'datasets' / 'qa')
    (store.root / "config.yaml").write_text(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False))
    store.save()
    return store


class Search:
    def __init__(self, config, store, evaluator=None, proposer=None):
        self.config, self.store = config, store
        self.evaluator = evaluator or Evaluator(config, store)
        self.proposer = proposer or CommandProposer(config.proposer)
        self.feedback = read_tasks(config.evaluation.feedback_tasks)
        self.selection = read_tasks(config.evaluation.selection_tasks)
        # libraries[0] accumulates published plugins (L); libraries[1] is the frozen initial library.
        self.libraries = [store.root / "plugin_library", store.root / "initial_plugins"]

    async def run(self):
        background = self.config.evaluation.background_completion
        try:
            if background:
                await self.evaluator.restore_background()
            result = await self._run()
            if background:
                await self.evaluator.finish_background()
                usage_path = self.store.root / 'usage.json'
                if usage_path.exists():
                    usage = read_json(usage_path)
                    usage['task_rollouts'] = self.store.state['rollouts_started']
                    write_json(usage_path, usage)
            return result
        finally:
            if background:
                await self.evaluator.finish_background(cancel=True)
            write_iteration_usage(self.config, self.store)

    async def _run(self):
        state = self.store.state
        state["stop_reason"] = None
        try:
            if not state["archive"]:
                evaluate = self.checked_evaluate if self.config.evaluation.parallel_heldout else self.evaluator.evaluate
                result = await evaluate("c000000", self.selection, "selection", "seed:selection")
                if not is_valid(result):
                    state["stop_reason"] = "invalid_seed_evaluation"
                    self.store.save()
                    return None
                self.admit("c000000", result)
                write_iteration_usage(self.config, self.store)
            while state["phase_index"] < self.config.search.iterations * len(PHASES):
                state["iteration"] = state["phase_index"] // len(PHASES) + 1
                state["phase"] = PHASES[state["phase_index"] % len(PHASES)]
                self.store.save()
                if not await self.phase(state["phase_index"]):
                    self.store.save()
                    return self.best()
                state["phase_index"] += 1
                completed = f"{state['iteration']}:{state['phase']}"
                if completed not in state.setdefault("completed_steps", []):
                    state["completed_steps"].append(completed)
                self.store.save()
                write_iteration_usage(self.config, self.store)
            state["stop_reason"] = "iterations_completed"
            state["phase"] = "completed"
        except BudgetExceeded as error:
            state["stop_reason"] = str(error)
        self.store.save()
        best = self.best()
        proposer_usage = [read_json(path) for path in self.store.root.glob("candidates/*/proposer_usage.json")]
        write_json(self.store.root / "usage.json", {
            "task_rollouts": state["rollouts_started"],
            "proposer_model_calls": (None if any(item["model_calls"] is None for item in proposer_usage)
                                     else sum(item["model_calls"] for item in proposer_usage)),
            "proposer_tokens": sum(item["tokens"] for item in proposer_usage),
            "proposer_command_calls": sum(1 for _ in self.store.root.glob("candidates/*/proposal_request.json")),
        })
        if best:
            write_json(self.store.root / "best.json", {"candidate_id": best, **state["archive"][best]})
        return best

    def best(self):
        beam = top_w(self.store.state["archive"], 1)
        return beam[0] if beam else None

    def admit(self, cid, result):
        self.store.state["archive"][cid] = {"evaluation_id": result["id"], "mean_score": result["mean_score"],
                                             "per_task_scores": {row["task_id"]: row["score"] for row in result["scores"] if row["status"] in ("completed", "solver_limit")},
                                             "valid_task_count": result.get("valid_task_count", len(result["scores"])),
                                             "excluded_task_ids": result.get("excluded_task_ids", []),
                                             "decision_snapshot": result.get("decision_snapshot", False)}
        self.store.state["beam"] = top_w(self.store.state["archive"], 1)
        self.store.save()

    async def phase(self, index):
        iteration, number = divmod(index, len(PHASES))
        phase = PHASES[number]
        path = self.store.root / "iterations" / f"iter_{iteration + 1:04d}" / f"{phase}.json"
        if path.exists():
            record = read_json(path)
            if record["completed"]:
                return True
        else:
            parent = self.best()
            record = dict(iteration=iteration + 1, phase=phase, completed=False,
                          parent=parent, parent_evaluation=self.store.state["archive"][parent]["evaluation_id"],
                          library_refs=library_refs(self.libraries), slots=[], published_plugins=[], plugin_evidence=[])
            write_json(path, record)
        stages = {"plugin_mutation": lambda: plugin_mutation(self, record, path),
                  "harness_recomposition": lambda: self.harness_recomposition(record, path)}
        ok = await stages[phase]()
        if ok:
            record["completed"] = True
            record["beam"] = self.store.state["beam"]
            write_json(path, record)
        return ok

    async def harness_recomposition(self, record, path):
        local = read_json(path.with_name("plugin_mutation.json"))
        if not local["completed"]:
            raise ValueError("harness recomposition requires a completed plugin mutation stage")
        full = self.result(record["parent_evaluation"])
        requests = []
        for number in range(self.config.search.recomposition_offspring):
            if len(record["slots"]) <= number:
                record["slots"].append(dict(id=self.store.allocate("candidate"), parent=record["parent"], status="allocated"))
                write_json(path, record)
            slot = record["slots"][number]
            request_path = self.store.candidate(slot["id"]) / "proposal_request.json"
            request = read_json(request_path) if request_path.exists() else self.make_request(
                slot, "harness_recomposition", full, record["library_refs"], [], evidence=local["slots"])
            requests.append(request)
        if not await self.proposal_wave(record["slots"], requests, record, path):
            return False

        async def evaluate_full(slot):
            result = await self.checked_evaluate(slot["id"], self.selection, "selection", f"full:{slot['id']}")
            slot["evaluation_id"] = result["id"]
            if not is_valid(result):
                if result["status"] == "infra_error":
                    self.store.state["stop_reason"] = "invalid_parent_evaluation"
                    write_json(path, record)
                    return False
                slot.update(status="rejected", reason="invalid_evaluation")
            else:
                slot.update(status="evaluated", mean_score=result["mean_score"])
            write_json(path, record)
            return True

        if not await self.evaluation_wave(record["slots"], evaluate_full):
            return False
        # Rank only after the wave finishes; completion order cannot choose the winner.
        for slot in record["slots"]:
            if slot["status"] == "evaluated":
                self.admit(slot["id"], self.result(slot["evaluation_id"]))
        return True

    # ---- evaluation -------------------------------------------------------------------

    async def checked_evaluate(self, cid, tasks, purpose, key):
        if purpose == 'selection' and self.config.evaluation.parallel_heldout:
            heldout = self._checked_evaluate(cid, read_tasks(self.config.evaluation.heldout_tasks),
                                             'heldout', f'final:heldout:{cid}')
            if self.config.evaluation.background_completion:
                self.evaluator.start_background(heldout)
                return await self._checked_evaluate(cid, tasks, purpose, key)
            jobs = [asyncio.create_task(self._checked_evaluate(cid, tasks, purpose, key)), asyncio.create_task(heldout)]
            try:
                result, _ = await asyncio.gather(*jobs)
                return result
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
        return await self._checked_evaluate(cid, tasks, purpose, key)

    async def _checked_evaluate(self, cid, tasks, purpose, key):
        """Refuse to start an evaluation that would eat into the reserved held-out budget."""
        if self.config.evaluation.background_completion and key in self.evaluator._jobs:
            return await self.evaluator.evaluate(cid, tasks, purpose, key)
        state = self.store.state
        eid = state["evaluation_keys"].get(key)
        cached = self.store.root / "evaluations" / str(eid) / "evaluation.json"
        pending = len(tasks)
        if cached.exists():
            record = read_json(cached)
            if record['status'] != 'running' and 'scores' in record:
                record = apply_quality_policy(record, quality_threshold(self.config, purpose))
            retry_completed = (self.evaluator.retry_infra and self.config.evaluation.infra_retry_attempts > 0
                               and not (record["status"] == "completed"
                                        and "scores" in record and is_valid(record)))
            if record["status"] == "completed" and not retry_completed:
                pending = 0
            else:
                pending = 0
                for task in tasks:
                    attempt = record["attempts"].get(task["id"], 0)
                    if attempt >= 1 + self.config.evaluation.infra_retry_attempts:
                        continue
                    result = cached.parent / "tasks" / task["id"] / f"attempt_{attempt}" / "result.json"
                    if not result.exists() or (self.evaluator.retry_infra and retryable_infra(read_json(result))):
                        pending += 1
        reserve = heldout_reserve(self.config, self.store) if purpose != 'heldout' else 0
        cap = self.config.search.max_task_rollouts
        if cap is not None and state["rollouts_started"] + pending + reserve > cap:
            raise BudgetExceeded("search budget exhausted; held-out allowance reserved")
        return await self.evaluator.evaluate(cid, tasks, purpose, key)

    def result(self, eid):
        directory = self.store.root / 'evaluations' / eid
        decision = directory / 'search_snapshot.json'
        return read_json(decision if decision.exists() else directory / 'evaluation.json')

    def branch_feedback(self, branch):
        """Project the incumbent's full evaluation onto a branch minibatch, reusing its rollouts."""
        source = self.result(branch["evaluation_id"])
        rows = {row["task_id"]: row for row in source["scores"]}
        projection = dict(source, task_ids=list(branch["batch_task_ids"]),
                          scores=[rows[tid] for tid in branch["batch_task_ids"]],
                          source_evaluation_id=source["id"], reused_result=True, rollouts_started=0)
        return apply_quality_policy(projection, self.config.evaluation.min_valid_ratio)

    async def evaluation_wave(self, slots, evaluate):
        jobs = [asyncio.create_task(evaluate(slot)) for slot in slots if slot["status"] == "ready"]
        try:
            return all(await asyncio.gather(*jobs))
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    # ---- proposals --------------------------------------------------------------------

    def extra_evidence(self, entries):
        files = {}
        for entry in entries:
            cid = entry["id"]
            candidate = self.store.candidate(cid)
            for directory in ("harness", "new_plugins"):
                for path in (candidate / directory).rglob("*"):
                    if path.is_file() and "__pycache__" not in path.parts:
                        files[f"evidence/{cid}/{path.relative_to(candidate)}"] = str(path)
            eid = entry.get("evaluation_id")
            if eid:
                root = self.store.root / "evaluations" / eid
                for row in self.result(eid)["scores"]:
                    trace = root / row["trajectory"]
                    if trace.exists():
                        files[f"evidence/{cid}/{row['task_id']}.jsonl"] = str(trace)
            parent_eid = entry.get("parent_evaluation_id")
            if parent_eid:
                root = self.store.root / "evaluations" / parent_eid
                selected = set(entry["batch_task_ids"])
                for row in self.result(parent_eid)["scores"]:
                    trace = root / row["trajectory"]
                    if row["task_id"] in selected and trace.exists():
                        files[f"evidence/{entry['parent']}/{row['task_id']}.jsonl"] = str(trace)
        return files

    def proposer_options(self):
        options = self.config.proposer
        compact = options.compact_evidence
        return dict(compact_evidence=compact,
                    proposer_max_protocol_errors=options.max_protocol_errors,
                    proposer_budget_reserve_calls=options.budget_reserve_calls,
                    proposer_context_max_bytes=options.context_max_bytes,
                    proposer_read_max_chars=options.read_max_chars if compact else 60000,
                    proposer_read_batch_max_chars=options.read_batch_max_chars if compact else None,
                    evidence_top_k=options.evidence_top_k)

    def make_request(self, slot, phase, feedback, refs, private_roots, alias=None, evidence=()):
        cid, parent = slot["id"], slot["parent"]
        candidate = self.store.candidate(cid)
        if not (candidate / "harness").exists():
            snapshot(self.store.candidate(parent) / "harness", candidate / "harness")
        request = dict(phase=phase, candidate_id=cid, version=f"v_{cid}",
                       parent_dir=str(self.store.candidate(parent) / "harness"), output_dir=str(candidate),
                       library_dirs=[str(p) for p in [*private_roots, *self.libraries]],
                       allowed_plugin_refs=refs, feedback=feedback, history=self.store.state["archive"],
                       feedback_dir=str(self.store.root / "evaluations" / feedback["id"]),
                       contracts=Path(__file__).parents[1].joinpath("contracts.py").read_text(),
                       runtime_contract=PROMPTS.joinpath("interfaces.md").read_text(),
                       instructions=PROMPTS.joinpath(f"{phase}.md").read_text(),
                       fitness_contract=PROMPTS.joinpath("fitness_contract.md").read_text().strip().format(
                           recomposition_offspring=self.config.search.recomposition_offspring),
                       local_evidence=list(evidence), extra_readable_files=self.extra_evidence(evidence),
                       validate_on_submit=True, **self.proposer_options())
        if alias:
            manifest = HarnessManifest.model_validate(read_yaml(self.store.candidate(parent) / "harness/harness.yaml"))
            request["local_contract"] = dict(alias=alias, source_ref=manifest.plugins[alias].ref)
            prepare_local_plugin(request)
        if self.config.proposer.agent:
            request["proposer_agent"] = self.config.proposer.agent.model_dump(mode="json")
        return request

    async def propose_slot(self, slot, request, record, path):
        cid = slot["id"]
        directory = self.store.candidate(cid)
        if (self.evaluator.retry_infra and (slot["status"] == "proposing" or
                (slot["status"] == "infra_error" and slot.get("reason") in ("interrupted_proposal", "proposal_infra_error")))
                and not (directory / "proposal_result.json").exists()):
            slot.update(status="allocated")
            slot.pop("reason", None)
        if (slot["status"] == "infra_error" and slot.get("reason") == "validation_infra_error"
                and self.evaluator.retry_infra and (directory / "proposal_result.json").exists()):
            slot["status"] = "proposing"
            slot.pop("reason", None)
            slot.pop("detail", None)
            write_json(path, record)
        if slot["status"] == "allocated":
            request["iteration"] = record["iteration"]
            write_json(directory / "proposal_request.json", request)
            slot["status"] = "proposing"
            slot["proposal_attempts"] = slot.get("proposal_attempts", 0) + 1
            write_json(path, record)
            try:
                result = await self.proposer.propose(request)
                write_json(directory / "proposal_result.json", result)
            except (InfraError, OSError, TimeoutError, asyncio.TimeoutError) as error:
                slot.update(status="infra_error", reason="proposal_infra_error", detail=str(error))
                write_json(path, record)
                return
            except ValueError as error:
                slot.update(status="rejected", reason="proposal_failed", detail=str(error))
                write_json(path, record)
                return
        if slot["status"] != "proposing":
            return
        result_path = directory / "proposal_result.json"
        if not result_path.exists():
            slot.update(status="infra_error", reason="interrupted_proposal")
            write_json(path, record)
            return
        try:
            result = read_json(result_path)
            refs = validate_proposal(directory, cid, request["phase"], list(map(Path, request["library_dirs"])),
                                     request["allowed_plugin_refs"], result, load_code=False)
            if "local_contract" in request:
                validate_local_surface(Path(request["parent_dir"]), directory / "harness", request["local_contract"]["alias"], refs[0])
            command = [sys.executable, "-m", "pluginrsi.cli", "validate", "--harness", str(directory / "harness")]
            for root in [directory / "new_plugins", *map(Path, request["library_dirs"])]:
                command += ["--library", str(root)]
            await self.validate_command(command, directory, slot, record, path)
            slot.update(status="ready", new_plugins=refs)
            write_json(directory / "candidate.yaml", dict(id=cid, parent_id=slot["parent"],
                iteration=record["iteration"], stage=request["phase"], **result))
        except (InfraError, OSError, TimeoutError, asyncio.TimeoutError) as error:
            slot.update(status="infra_error", reason="validation_infra_error", detail=str(error))
        except ValueError as error:
            slot.update(status="rejected", reason="invalid_candidate", detail=str(error))
        write_json(path, record)

    async def validate_command(self, command, directory, slot, record, path):
        options = self.config.proposer
        for attempt in range(options.validation_max_attempts):
            slot["validation_attempts"] = slot.get("validation_attempts", 0) + 1
            write_json(path, record)
            try:
                code = await run_command(command, directory, directory / "validation.log", options.validation_timeout_seconds)
                if code < 0:
                    raise InfraError(f"candidate validation terminated by signal {-code}")
            except (InfraError, OSError, TimeoutError, asyncio.TimeoutError) as error:
                detail = (f"validation {type(error).__name__}; limit={options.validation_timeout_seconds}s; "
                          f"attempt={attempt + 1}/{options.validation_max_attempts}: {error}")
                slot.setdefault("validation_errors", []).append(detail)
                write_json(path, record)
                if attempt + 1 == options.validation_max_attempts:
                    raise InfraError(detail) from error
                continue
            if code:
                raise ValueError("candidate loader validation failed; see validation.log")
            return

    async def proposal_wave(self, slots, requests, record, path):
        protected = [tree_contents(root) for root in self.libraries]
        pool = asyncio.Semaphore(self.config.proposer.concurrency or max(1, len(slots)))

        async def propose(slot, request):
            async with pool:
                await self.propose_slot(slot, request, record, path)

        jobs = [asyncio.create_task(propose(slot, request)) for slot, request in zip(slots, requests)]
        try:
            await asyncio.gather(*jobs)
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
        if protected != [tree_contents(root) for root in self.libraries]:
            raise RuntimeError("proposer modified frozen published library")
        if any(slot["status"] == "infra_error" for slot in slots):
            self.store.state["stop_reason"] = "proposal_infra_error"
            return False
        return True

    # ---- reporting --------------------------------------------------------------------

    async def report_heldout(self):
        try:
            return await self._report_heldout()
        finally:
            write_iteration_usage(self.config, self.store)

    async def _report_heldout(self):
        budget_stopped = self.store.state.get('stop_reason') in BUDGET_STOP_REASONS
        if self.store.state["phase_index"] != self.config.search.iterations * len(PHASES) and not budget_stopped:
            raise ValueError("held-out reporting requires completed search")
        best = self.best()
        if best is None or self.config.evaluation.heldout_tasks is None:
            raise ValueError("no best candidate or held-out dataset")
        tasks = read_tasks(self.config.evaluation.heldout_tasks)
        results = {}
        task_scores = {}
        candidates = list(dict.fromkeys(["c000000", best]))
        iterations = {'c000000': 0}
        if self.config.evaluation.parallel_heldout:
            candidates = ['c000000']
            for path in sorted((self.store.root / 'iterations').glob('*/harness_recomposition.json')):
                phase = read_json(path)
                for slot in phase['slots']:
                    if slot.get('evaluation_id'):
                        candidates.append(slot['id'])
                        iterations[slot['id']] = phase['iteration']
            candidates = list(dict.fromkeys([*candidates, best]))
        pool = asyncio.Semaphore(max(1, self.config.evaluation.concurrency // len(tasks)))

        async def evaluate(cid):
            async with pool:
                result = await self.evaluator.evaluate(cid, tasks, "heldout", f"final:heldout:{cid}")
            task_scores[cid] = valid_scores(result)
            results[cid] = {"evaluation_id": result["id"], "mean_score": result["mean_score"], "valid": is_valid(result),
                            "valid_task_count": result.get("valid_task_count", len(result["scores"])),
                            "excluded_task_ids": result.get("excluded_task_ids", [])}

        jobs = [asyncio.create_task(evaluate(cid)) for cid in candidates]
        try:
            await asyncio.gather(*jobs)
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
        results = {cid: results[cid] for cid in candidates}
        valid = all(results[cid]['valid'] for cid in dict.fromkeys(['c000000', best]))
        common = sorted(task_scores[best].keys() & task_scores["c000000"].keys())
        report = {"best_candidate": best, "results": results,
                  "matched_task_count": len(common), "matched_task_ids": common,
                  "raw_mean_difference": results[best]["mean_score"] - results["c000000"]["mean_score"] if valid else None,
                  "gain_over_seed": sum(task_scores[best][t] - task_scores["c000000"][t] for t in common) / len(common) if valid and common else None}
        if self.config.evaluation.parallel_heldout:
            report['curve'] = [dict(iteration=iterations.get(cid), candidate_id=cid, **results[cid]) for cid in candidates]
        write_json(self.store.root / "heldout_report.json", report)
        return report
