from __future__ import annotations

import asyncio
import copy
import time
import json
import math
import os
import random
import sys
from pathlib import Path

from ..runtime import BudgetExceeded
from ..search.proposer import run_command
from ..search.store import read_json, write_json
from ..search.selection import apply_quality_policy, is_valid


def retryable_infra(row):
    return row.get('status') == 'infra_error' and row.get('retryable', True)


def valid_score(row):
    score = row.get('score')
    return (row.get('status') in ('completed', 'solver_limit')
            and isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score))


def quality_threshold(config, purpose):
    full = purpose in ('selection', 'heldout')
    override = config.evaluation.full_min_valid_ratio
    return override if full and override is not None else config.evaluation.min_valid_ratio


def heldout_candidate_limit(config):
    return 1 + config.search.iterations * config.search.recomposition_offspring if config.evaluation.parallel_heldout else 2


def heldout_reserve(config, store):
    if config.evaluation.heldout_tasks is None:
        return 0
    candidates = heldout_candidate_limit(config)
    if config.evaluation.parallel_heldout:
        candidates -= sum(key.startswith('final:heldout:') for key in store.state['evaluation_keys'])
    return max(0, candidates) * len(read_tasks(config.evaluation.heldout_tasks))


def read_tasks(path: Path) -> list[dict]:
    tasks = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ids = [row["id"] for row in tasks]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError(f"task set must be nonempty with unique IDs: {path}")
    if any(not isinstance(tid, str) or not tid or Path(tid).name != tid or tid in (".", "..") for tid in ids):
        raise ValueError("task IDs must be simple nonempty filenames")
    return tasks


class Evaluator:
    def __init__(self, config, store, retry_infra=False):
        self.config = config
        self.store = store
        self.retry_infra = retry_infra
        self._pool_loop = None
        self._semaphore = None
        self._reserved_rollouts = 0
        self._jobs = {}
        self._ready = {}
        self._background = set()

    def start_background(self, coroutine):
        job = asyncio.create_task(coroutine)
        self._background.add(job)
        return job

    async def finish_background(self, cancel=False):
        while self._background:
            jobs = list(self._background)
            if cancel:
                for job in jobs:
                    if not job.done():
                        job.cancel()
            results = await asyncio.gather(*jobs, return_exceptions=True)
            self._background.difference_update(jobs)
            if not cancel:
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

    async def restore_background(self):
        if not self.config.evaluation.background_completion:
            return
        lookup = {}
        for purpose, field in [('selection', 'selection_tasks'), ('heldout', 'heldout_tasks'),
                               ('screening', 'feedback_tasks'), ('feedback', 'feedback_tasks')]:
            path = getattr(self.config.evaluation, field)
            if path:
                lookup[purpose] = {t['id']: t for t in read_tasks(path)}
        for key, eid in list(self.store.state['evaluation_keys'].items()):
            path = self.store.root / 'evaluations' / eid / 'evaluation.json'
            if not path.exists():
                continue
            record = read_json(path)
            if record.get('background') and record['status'] == 'running':
                tasks = [lookup[record['purpose']][tid] for tid in record['task_ids']]
                self.start_background(self.evaluate(record['candidate_id'], tasks, record['purpose'], key))
        # Register resumed jobs before the search can request the same evaluation.
        await asyncio.sleep(0)

    async def evaluate(self, cid: str, tasks: list[dict], purpose: str, key: str):
        if not self.config.evaluation.background_completion:
            return await self._evaluate(cid, tasks, purpose, key)
        if key not in self._jobs:
            ready = asyncio.get_running_loop().create_future() if purpose != 'heldout' else None
            self._ready[key] = ready
            eid = self.store.state['evaluation_keys'].get(key)
            decision = self.store.root / 'evaluations' / str(eid) / 'search_snapshot.json'
            if ready is not None and decision.exists():
                snapshot = read_json(decision)
                if snapshot['candidate_id'] != cid or snapshot['task_ids'] != [t['id'] for t in tasks]:
                    raise ValueError('evaluation resume inputs changed')
                ready.set_result(snapshot)
            self._jobs[key] = self.start_background(self._evaluate(cid, tasks, purpose, key, ready))
        job, ready = self._jobs[key], self._ready[key]
        if ready is None:
            return await asyncio.shield(job)
        await asyncio.wait([job, ready], return_when=asyncio.FIRST_COMPLETED)
        # Once published, decisions stay reproducible even after late results arrive.
        if ready.done():
            return copy.deepcopy(ready.result())
        return copy.deepcopy(job.result())

    async def _evaluate(self, cid: str, tasks: list[dict], purpose: str, key: str, ready=None):
        state = self.store.state
        evaluation = self.config.evaluation
        fast = evaluation.accept_partial_results
        full = purpose in ('selection', 'heldout')
        max_attempts = 1 + evaluation.infra_retry_attempts
        stop_on_ratio = fast and not full and not evaluation.background_completion
        threshold = quality_threshold(self.config, purpose)
        limit = (evaluation.local_timeout_seconds if purpose == "screening"
                 else evaluation.evaluation_timeout_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit if limit is not None else None
        if key not in state["evaluation_keys"]:
            eid = self.store.allocate("evaluation")
            state["evaluation_keys"][key] = eid
            self.store.save()
        eid = state["evaluation_keys"][key]
        directory = self.store.root / "evaluations" / eid
        record_path = directory / "evaluation.json"
        if record_path.exists():
            record = read_json(record_path)
            if record["candidate_id"] != cid or record["task_ids"] != [t["id"] for t in tasks]:
                raise ValueError("evaluation resume inputs changed")
            if record["status"] != "running" and "scores" in record:
                updated = apply_quality_policy(record, threshold)
                if updated != record:
                    backup = directory / "evaluation.before-quality-policy.json"
                    if not backup.exists():
                        write_json(backup, record)
                    record = updated
                    write_json(record_path, record)
            has_infra = any(row["status"] == "infra_error" for row in record.get("scores", []))
            if evaluation.fail_fast_candidate_errors and record["status"] == "candidate_error":
                return record
            if record["status"] == "completed" and is_valid(record):
                return record
            if self.retry_infra and (record["status"] == "infra_error" or
                                    (has_infra and self.config.evaluation.infra_retry_attempts > 0)):
                if record['status'] != 'running':
                    record.pop('background_deadline_at', None)
                    record.pop('finish_reason', None)
                record["status"] = "running"
                write_json(record_path, record)
            elif record["status"] != "running":
                return record
        else:
            record = dict(schema_version=1, id=eid, candidate_id=cid, purpose=purpose,
                          dataset=self.config.evaluation.benchmark, task_ids=[t["id"] for t in tasks],
                          status="running", scores_path="scores.jsonl", rollouts_started=0, attempts={},
                          iteration=None if purpose == "heldout" else state.get("iteration", 0),
                          phase="heldout" if purpose == "heldout" else state.get("phase", "seed"))
            write_json(record_path, record)
        if evaluation.background_completion:
            record['background'] = True
            if deadline is not None:
                wall_deadline = record.setdefault('background_deadline_at', time.time() + limit)
                deadline = loop.time() + max(0, wall_deadline - time.time())
            write_json(record_path, record)
        completed = {}
        for task in tasks:
            attempt = record["attempts"].get(task["id"], 0)
            result_path = directory / "tasks" / task["id"] / f"attempt_{attempt}" / "result.json"
            if attempt and result_path.exists():
                row = read_json(result_path)
                row.update(task_id=task["id"], trajectory=str(result_path.parent.relative_to(directory) / "trajectory.jsonl"))
                completed[task["id"]] = row
        ready_count = (evaluation.search_ready_tasks if purpose == 'selection' and evaluation.search_ready_tasks
                       else math.ceil(len(tasks) * threshold - 1e-12))
        if ready is not None and ready_count > len(tasks):
            raise ValueError('search_ready_tasks exceeds the selection task count')
        decision_path = directory / 'search_snapshot.json'

        def publish_ready():
            if ready is None:
                return
            rows = [completed[t['id']] for t in tasks if t['id'] in completed]
            usable = [r for r in rows if valid_score(r)]
            if ready.done():
                record.update(scores=rows, live_mean_score=sum(r['score'] for r in usable) / len(usable) if usable else None)
                write_json(record_path, record)
                return
            if decision_path.exists():
                decision = read_json(decision_path)
            elif len(usable) >= ready_count and all(r['status'] in ('completed', 'solver_limit', 'infra_error') for r in rows):
                decision = copy.deepcopy(record)
                decision.update(status='search_ready', scores=rows, mean_score=sum(r['score'] for r in usable)/len(usable),
                    valid_task_count=len(usable), valid_ratio=len(usable)/len(tasks), min_valid_ratio=ready_count/len(tasks),
                    excluded_task_ids=[t['id'] for t in tasks if t['id'] not in {r['task_id'] for r in usable}],
                    decision_snapshot=True, search_ready_at=time.time(), search_ready_tasks=ready_count)
                write_json(decision_path, decision)
            else:
                return
            record.update(search_ready=True, search_ready_at=decision['search_ready_at'],
                          search_ready_tasks=ready_count, search_decision_mean=decision['mean_score'], scores=rows)
            write_json(record_path, record)
            ready.set_result(copy.deepcopy(decision))

        publish_ready()
        def enough():
            return (sum(row["status"] in ("completed", "solver_limit") for row in completed.values())
                    >= math.ceil(len(tasks) * threshold - 1e-12)
                    and all(row["status"] in ("completed", "solver_limit", "infra_error") for row in completed.values()))

        def attempts_available(tid):
            return record['attempts'].get(tid, 0) < max_attempts

        pending = [task for task in tasks if attempts_available(task['id']) and
                   (task["id"] not in completed or
                    (self.retry_infra and retryable_infra(completed[task["id"]])))]
        if stop_on_ratio and enough():
            pending = []
        budget = self.config.search.max_task_rollouts
        if budget is not None and state["rollouts_started"] + self._reserved_rollouts + len(pending) > budget:
            raise BudgetExceeded("insufficient rollout budget for a complete evaluation")
        loop = asyncio.get_running_loop()
        if self._pool_loop is not loop:
            self._pool_loop = loop
            self._semaphore = asyncio.Semaphore(self.config.evaluation.concurrency)
        remaining = 0

        async def run(task):
            nonlocal remaining
            async with self._semaphore:
                remaining -= 1
                self._reserved_rollouts -= 1
                tid = task["id"]
                attempt = record["attempts"].get(tid, 0) + 1
                record["attempts"][tid] = attempt
                # Persist the consumed allowance before spawning. A crash may conservatively
                # consume an unused slot, but can never refund an already-started rollout.
                state["rollouts_started"] += 1
                record["rollouts_started"] += 1
                self.store.save()
                write_json(record_path, record)
                work = directory / "tasks" / tid / f"attempt_{attempt}"
                work.mkdir(parents=True, exist_ok=True)
                evaluation = self.config.evaluation
                delay = random.Random(f"{self.config.search.random_seed}:{eid}:{tid}:{attempt}").uniform(
                    evaluation.startup_stagger_min_seconds, evaluation.startup_stagger_max_seconds)
                write_json(work / "startup.json", {"delay_seconds": delay})
                await asyncio.sleep(delay)
                candidate = self.store.candidate(cid)
                request = dict(harness=str(candidate / "harness"),
                               libraries=[str(candidate / "new_plugins"), str(self.store.root / "plugin_library"),
                                          str(self.store.root / "initial_plugins")],
                               task=task, work_dir=str(work), solver=self.config.solver.model_dump(mode="json"),
                               evaluation=evaluation.model_dump(mode="json"),
                               accounting=dict(iteration=record.get("iteration"), phase=record.get("phase"),
                                               evaluation_id=eid, model_role="solver", task_attempt=attempt))
                write_json(work / "request.json", request)
                options = evaluation.benchmark_options
                worker_python = options.get("worker_python", sys.executable)
                env = dict(os.environ)
                env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parents[2]),
                    *options.get("module_paths", []), env.get("PYTHONPATH", "")])
                timeout = evaluation.task_timeout_seconds + evaluation.infrastructure_timeout_seconds
                runner = run_command
                runner_options = dict(env=env)
                if evaluation.worker_initialization_timeout_seconds is not None:
                    from .progress import GRACE_SECONDS, limits, mark, run_worker_command
                    phase_limits = limits(evaluation)
                    mark(work, 'initializing')
                    timeout = sum(phase_limits.values()) + GRACE_SECONDS
                    runner = run_worker_command
                    runner_options.update(progress_path=work / 'worker_stage.json', phase_limits=phase_limits)
                failure_detail = None
                try:
                    code = await runner([worker_python, "-m", "pluginrsi.evaluation.worker", str(work / "request.json")],
                                             work, work / "worker.log", timeout, **runner_options)
                except (TimeoutError, asyncio.TimeoutError) as error:
                    code = -1
                    failure_detail = str(error) or f"worker deadline exceeded: {timeout}s"
                result_path = work / "result.json"
                saved = read_json(result_path) if result_path.exists() else {}
                if valid_score(saved):
                    row = saved
                    if code != 0:
                        row['worker_exit_error'] = failure_detail or f"worker failed: {code}"
                else:
                    row = saved if code == 0 and saved else {
                        "score": 0.0, "status": "infra_error", "detail": failure_detail or f"worker failed: {code}"}
                if not isinstance(row.get("score"), (float, int)) or not math.isfinite(row["score"]):
                    row = {"score": 0.0, "status": "infra_error", "detail": "nonfinite verifier score"}
                write_json(result_path, row)
                row.update(task_id=tid, trajectory=str(work.relative_to(directory) / "trajectory.jsonl"))
                completed[tid] = row
                publish_ready()

        reserve = 0
        if purpose != "heldout" and self.config.evaluation.heldout_tasks:
            reserve = heldout_reserve(self.config, self.store)
        eager = fast and evaluation.eager_infra_retries
        retry_counts = {task['id']: 0 for task in tasks}
        finish_reason = None
        cancelled = False
        for _ in range(evaluation.infra_retry_attempts + 1):
            if enough() and (stop_on_ratio or len(completed) == len(tasks)):
                finish_reason = "valid_ratio_reached"
                break
            if not pending:
                break
            if deadline is not None and loop.time() >= deadline:
                finish_reason = "evaluation_deadline"
                break
            if budget is not None and state["rollouts_started"] + self._reserved_rollouts + len(pending) + reserve > budget:
                raise BudgetExceeded("insufficient rollout budget for infrastructure retries")
            remaining = len(pending)
            self._reserved_rollouts += remaining
            jobs = [asyncio.create_task(run(task)) for task in pending]
            job_ids = {job: task['id'] for job, task in zip(jobs, pending)}
            active_ids = set(job_ids.values())
            try:
                waiting = set(jobs)
                while waiting:
                    if evaluation.fail_fast_candidate_errors and any(
                            row['status'] not in ('completed', 'solver_limit', 'infra_error') for row in completed.values()):
                        finish_reason = 'candidate_error'
                        break
                    if stop_on_ratio and enough():
                        finish_reason = "valid_ratio_reached"
                        break
                    timeout = max(0, deadline - loop.time()) if deadline is not None else None
                    done, waiting = await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        finish_reason = "evaluation_deadline"
                        break
                    for job in done:
                        job.result()
                        active_ids.discard(job_ids[job])
                    if eager and not enough() and all(row['status'] in ('completed', 'solver_limit', 'infra_error')
                                                     for row in completed.values()):
                        errors = sum(row['status'] == 'infra_error' for row in completed.values())
                        tolerated = len(tasks) - math.ceil(len(tasks) * threshold - 1e-12)
                        retry = [task for task in tasks if task['id'] not in active_ids
                                 and retryable_infra(completed.get(task['id'], {}))
                                 and attempts_available(task['id'])
                                 and retry_counts[task['id']] < evaluation.infra_retry_attempts] if errors > tolerated else []
                        fits = budget is None or state['rollouts_started'] + self._reserved_rollouts + len(retry) + reserve <= budget
                        if retry and fits:
                            remaining += len(retry)
                            self._reserved_rollouts += len(retry)
                            for task in retry:
                                tid = task['id']
                                retry_counts[tid] += 1
                                job = asyncio.create_task(run(task))
                                jobs.append(job)
                                job_ids[job] = tid
                                active_ids.add(tid)
                                waiting.add(job)
            except asyncio.CancelledError:
                finish_reason = "evaluation_cancelled"
                cancelled = True
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                self._reserved_rollouts -= remaining
                remaining = 0
            if finish_reason:
                break
            if eager:
                if (not enough() and budget is not None
                        and all(row['status'] in ('completed', 'solver_limit', 'infra_error') for row in completed.values()) and any(
                        retryable_infra(row) and attempts_available(tid) and retry_counts[tid] < evaluation.infra_retry_attempts
                        for tid, row in completed.items())):
                    raise BudgetExceeded('insufficient rollout budget for infrastructure retries')
                break
            pending = [task for task in tasks if attempts_available(task['id']) and retryable_infra(completed[task["id"]])]
            if not pending:
                break
        if cancelled and not fast:
            raise asyncio.CancelledError
        # Recover a result written just before cancellation before marking an unfinished attempt.
        for task in tasks:
            tid = task["id"]
            if tid in completed and completed[tid]["status"] != "infra_error":
                continue
            attempt = record["attempts"].get(tid, 0)
            work = directory / "tasks" / tid / (f"attempt_{attempt}" if attempt else "unstarted")
            result_path = work / "result.json"
            row = read_json(result_path) if attempt and result_path.exists() else None
            if row is None:
                row = dict(score=0.0, status="infra_error", detail=finish_reason or "unfinished_task",
                           failure_origin="scheduler", excluded=True)
                if attempt:
                    write_json(result_path, row)
            if not isinstance(row.get("score"), (int, float)) or not math.isfinite(row["score"]):
                row = dict(score=0.0, status="infra_error", detail="nonfinite verifier score")
            row.update(task_id=tid, trajectory=str(work.relative_to(directory) / "trajectory.jsonl"))
            completed[tid] = row
        rows = [completed[t["id"]] for t in tasks]
        if finish_reason:
            record["finish_reason"] = finish_reason
        record["time_limit_seconds"] = limit
        record["scores"] = rows
        record = apply_quality_policy(record, threshold)
        record["solver_tokens"] = 0
        record["model_calls"] = 0
        for trace_path in directory.glob("tasks/*/attempt_*/trajectory.jsonl"):
            usage_path = trace_path.with_name("usage.json")
            if usage_path.exists():
                usage = read_json(usage_path)
                record["model_calls"] += usage.get("model_calls", 0)
                record["solver_tokens"] += usage.get("solver_tokens", 0)
                continue
            calls = tokens = 0
            with trace_path.open() as stream:
                for line in stream:
                    if not line.strip() or not line.endswith("\n"):
                        continue
                    event = json.loads(line)
                    calls += event["event"] == "model/request"
                    tokens = max(tokens, event.get("total_tokens", 0))
            record["model_calls"] += calls
            record["solver_tokens"] += tokens
        (directory / "scores.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        write_json(record_path, record)
        if cancelled:
            raise asyncio.CancelledError
        return record
