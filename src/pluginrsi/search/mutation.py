"""Stage 1 of PluginRSI: plugin mutation (Algorithm 1, lines 5-12).

Each of N independent branches samples a balanced minibatch, mutates one plugin of the
incumbent harness while freezing the workflow and all other plugins, and publishes the
mutated plugin to the shared library only if it strictly improves the minibatch score.
"""

import asyncio
import math
import random

from ..loader import tree_contents
from ..schemas import HarnessManifest, read_yaml
from .selection import is_valid, valid_scores
from .store import read_json, snapshot, write_json


def balanced_batch(tasks, scores, size, failures, seed):
    rng = random.Random(seed)
    wrong = [t for t in tasks if scores[t["id"]] < 1]
    right = [t for t in tasks if scores[t["id"]] == 1]
    chosen = rng.sample(wrong, min(failures, len(wrong))) + rng.sample(right, min(size - failures, len(right)))
    selected = {t["id"] for t in chosen}
    chosen += rng.sample([t for t in tasks if t["id"] not in selected], size - len(chosen))
    return chosen


def paired(parent, child, task_ids, min_ratio):
    """Compare child against parent on the tasks both evaluated validly; retain on positive mean gain."""
    old, new = valid_scores(parent), valid_scores(child)
    common = sorted(set(task_ids) & old.keys() & new.keys())
    wins = [tid for tid in common if new[tid] > old[tid]]
    losses = [tid for tid in common if new[tid] < old[tid]]
    gain = sum(new[tid] - old[tid] for tid in common) / len(common) if common else None
    selected = set(task_ids)
    minimum_common = max(1, math.ceil(len(selected) * (2 * min_ratio - 1) - 1e-12))
    valid = (is_valid(parent) and is_valid(child) and len(selected & old.keys()) / len(selected) >= min_ratio
             and len(selected & new.keys()) / len(selected) >= min_ratio
             and len(common) >= minimum_common)
    return dict(valid=valid, matched_gain=gain, matched_task_count=len(common),
                wins=len(wins), losses=len(losses), win_task_ids=wins[:3], loss_task_ids=losses[:3],
                retained=bool(valid and gain > 0))


def validate_local_surface(parent, child, alias, new_ref):
    before = tree_contents(parent)
    after = tree_contents(child)
    before.pop("harness.yaml")
    after.pop("harness.yaml")
    if before != after:
        raise ValueError("plugin mutation cannot change workflow files")
    expected = HarnessManifest.model_validate(read_yaml(parent / "harness.yaml"))
    actual = HarnessManifest.model_validate(read_yaml(child / "harness.yaml"))
    source = expected.plugins[alias].ref
    if new_ref.rsplit("/", 1)[0] != source.rsplit("/", 1)[0]:
        raise ValueError("mutated plugin must retain the assigned kind/name")
    expected.plugins[alias].ref = new_ref
    if expected != actual:
        raise ValueError("plugin mutation may change only the assigned alias ref")


def create_branches(search, record):
    options = search.config.search
    full = search.result(record["parent_evaluation"])
    usable = valid_scores(full)
    eligible = [t for t in search.feedback if t["id"] in usable]
    if not is_valid(full) or len(eligible) < options.feedback_batch_size:
        raise ValueError("Incumbent full evaluation has insufficient valid tasks for the configured branch batch")
    manifest = HarnessManifest.model_validate(read_yaml(search.store.candidate(record["parent"]) / "harness/harness.yaml"))
    aliases = sorted(manifest.plugins)
    branches = []
    for i in range(options.offspring_per_phase if aliases else 0):
        seed = f"{options.random_seed}:{record['iteration']}:{i}"
        batch = balanced_batch(eligible, usable, options.feedback_batch_size, options.local_failure_tasks, seed)
        branches.append(dict(alias=aliases[i % len(aliases)], incumbent=record["parent"],
                             evaluation_id=full["id"], initial_evaluation_id=full["id"], private_refs=[],
                             sampling_seed=seed, batch_task_ids=[t["id"] for t in batch],
                             initial_mean_score=sum(usable[t["id"]] for t in batch) / len(batch)))
    return branches


async def plugin_mutation(search, record, path):
    options = search.config.search
    ratio = search.config.evaluation.min_valid_ratio
    lookup = {t["id"]: t for t in search.feedback}
    if "branches" not in record:
        record["branches"] = create_branches(search, record)
        write_json(path, record)
    pool = asyncio.Semaphore(search.config.proposer.concurrency or max(1, len(record["branches"])))
    protected = [tree_contents(root) for root in search.libraries]

    # Allocate every candidate ID up front so asynchronous completion order cannot change them.
    for index, branch in enumerate(record["branches"]):
        if not any(s["branch"] == index for s in record["slots"]):
            record["slots"].append(dict(id=search.store.allocate("candidate"), parent=branch["incumbent"],
                                        status="allocated", branch=index, step=0,
                                        batch_task_ids=list(branch["batch_task_ids"]),
                                        parent_evaluation_id=branch["evaluation_id"]))
    write_json(path, record)

    def slot_of(index):
        return next(s for s in record["slots"] if s["branch"] == index)

    async def run_branch(index):
        branch = record["branches"][index]
        slot = slot_of(index)
        if slot["status"] in ("evaluated", "rejected", "skipped"):
            return
        request_path = search.store.candidate(slot["id"]) / "proposal_request.json"
        request = read_json(request_path) if request_path.exists() else search.make_request(
            slot, "plugin_mutation", search.branch_feedback(branch), record["library_refs"], [], branch["alias"])
        async with pool:
            await search.propose_slot(slot, request, record, path)
        if slot["status"] != "ready":
            return
        parent = search.branch_feedback(branch)
        result = await search.checked_evaluate(slot["id"], [lookup[t] for t in branch["batch_task_ids"]],
                                               "screening", f"local:{slot['id']}")
        comparison = paired(parent, result, branch["batch_task_ids"], ratio)
        slot.update(comparison, evaluation_id=result["id"], mean_score=result["mean_score"],
                    parent_mean=parent["mean_score"], status="evaluated" if comparison["valid"] else "rejected")
        if not comparison["valid"]:
            slot["reason"] = "invalid_evaluation"
        if comparison["retained"]:
            branch.update(incumbent=slot["id"], evaluation_id=result["id"], private_refs=slot["new_plugins"])
        write_json(path, record)

    async def bounded_branch(index):
        branch = record["branches"][index]
        if branch.get("completed"):
            return
        try:
            await asyncio.wait_for(run_branch(index), timeout=options.local_branch_timeout_seconds)
        except asyncio.TimeoutError:
            branch["stopped_reason"] = "branch_timeout"
            slot = slot_of(index)
            if slot["status"] in ("allocated", "proposing", "ready"):
                slot.update(status="skipped", reason="branch_timeout")
        branch["completed"] = True
        write_json(path, record)

    jobs = [asyncio.create_task(bounded_branch(i)) for i in range(len(record["branches"]))]
    try:
        await asyncio.gather(*jobs)
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
    if protected != [tree_contents(root) for root in search.libraries]:
        raise RuntimeError("proposer modified frozen published library")

    record["plugin_evidence"] = [s for s in record["slots"] if s.get("evaluation_id")]
    for index, branch in enumerate(record["branches"]):
        cid = branch["incumbent"]
        if cid == record["parent"] or branch.get("stopped_reason"):
            continue
        for ref in branch["private_refs"]:
            snapshot(search.store.candidate(cid) / "new_plugins" / ref, search.libraries[0] / ref)
            publication = dict(ref=ref, candidate_id=cid, alias=branch["alias"], evaluation_id=branch["evaluation_id"],
                               branch=index, batch_task_ids=branch["batch_task_ids"],
                               initial_parent_evaluation_id=branch["initial_evaluation_id"])
            if publication not in record["published_plugins"]:
                record["published_plugins"].append(publication)
    write_json(path, record)
    if record["slots"] and not any(s.get("evaluation_id") for s in record["slots"]):
        search.store.state["stop_reason"] = "all_local_proposals_invalid"
        return False
    return True
