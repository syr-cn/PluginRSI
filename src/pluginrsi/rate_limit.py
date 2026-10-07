"""Shared host-side request admission across solver and proposer processes."""

import asyncio
import fcntl
import json
import time
from pathlib import Path

from .search.store import write_json

WINDOW_SECONDS = 60.0
POLL_SECONDS = 0.25


def admit(path: Path, rpm: int, now: float) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(path.read_text()) if path.exists() else {"rpm": rpm, "requests": []}
        if record["rpm"] != rpm:
            raise ValueError("processes sharing a rate-limit file must use the same RPM")
        recent = [t for t in record["requests"] if t > now - WINDOW_SECONDS]
        # Ignore legacy adaptive caps; only the configured RPM controls admission.
        effective = rpm
        spacing = WINDOW_SECONDS / effective
        wait = max(0.0, recent[-1] + spacing - now) if recent else 0.0
        if len(recent) >= effective:
            wait = max(wait, recent[0] + WINDOW_SECONDS - now)
        if wait <= 0:
            write_json(path, {"rpm": rpm, "requests": recent + [now]})
        return wait


def note_rate_limit(path: Path, rpm: int, now: float, cooldown: float = WINDOW_SECONDS):
    """Compatibility hook: retries are request-local and never lower shared RPM."""
    return rpm


async def acquire(path: Path, rpm: int):
    while True:
        wait = admit(path, rpm, time.monotonic())
        if wait <= 0:
            return
        await asyncio.sleep(min(wait + 0.001, POLL_SECONDS))
