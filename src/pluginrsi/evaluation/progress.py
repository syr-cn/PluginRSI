"""Worker lifecycle markers and bounded process supervision."""

import asyncio
import json
import time
from pathlib import Path

from ..search.proposer import run_command
from ..search.store import read_json, write_json

POLL_SECONDS = 1.0
GRACE_SECONDS = 15.0
CATEGORIES = {'initializing': 'initializing', 'environment': 'infrastructure',
              'solving': 'solving', 'preparing_verifier': 'infrastructure',
              'verifying': 'verifying', 'done': None}


def limits(evaluation):
    return dict(initializing=evaluation.worker_initialization_timeout_seconds,
                solving=evaluation.task_timeout_seconds,
                verifying=evaluation.verifier_timeout_seconds,
                infrastructure=evaluation.infrastructure_timeout_seconds)


def mark(work, stage):
    path = Path(work) / 'worker_stage.json'
    now = time.monotonic()
    previous = read_json(path) if path.exists() else {}
    spent = dict(previous.get('spent_seconds', {}))
    category = CATEGORIES.get(previous.get('stage'))
    if category:
        spent[category] = spent.get(category, 0) + max(0, now - previous['started_monotonic'])
    write_json(path, dict(stage=stage, sequence=previous.get('sequence', -1) + 1,
                         started_monotonic=now, timestamp=time.time(), spent_seconds=spent))
    milestone(work, stage)


def milestone(work, name):
    with (Path(work) / 'worker_timing.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(event=name, timestamp=time.time(), monotonic=time.monotonic())) + '\n')


async def run_worker_command(command, cwd, log_path, timeout, *, env, progress_path, phase_limits):
    job = asyncio.create_task(run_command(command, cwd, log_path, timeout, env=env))
    overall_deadline = time.monotonic() + timeout
    current = dict(stage='initializing', sequence=-1, started_monotonic=time.monotonic(), spent_seconds={})
    try:
        while not job.done():
            try:
                observed = read_json(progress_path)
            except (OSError, json.JSONDecodeError):
                observed = current
            if observed.get('sequence', -1) >= current['sequence']:
                current = observed
            category = CATEGORIES[current['stage']]
            deadline = overall_deadline
            if category:
                spent = current.get('spent_seconds', {})
                allowance = phase_limits[category] - spent.get(category, 0)
                # Harbor bounds verification itself; its teardown shares unused infrastructure time.
                if category == 'verifying':
                    allowance += max(0, phase_limits['infrastructure'] - spent.get('infrastructure', 0))
                deadline = min(deadline, current['started_monotonic'] + allowance + GRACE_SECONDS)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"worker stage timeout: {current['stage']}; limits={phase_limits}")
            await asyncio.wait({job}, timeout=min(POLL_SECONDS, remaining))
        return job.result()
    finally:
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)
