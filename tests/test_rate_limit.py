import asyncio
from concurrent.futures import ProcessPoolExecutor
from functools import partial

import pytest

from pluginrsi.rate_limit import acquire, admit
from pluginrsi.schemas import SolverConfig


def test_shared_spacing_and_rolling_window(tmp_path):
    path = tmp_path / "rate.json"
    assert admit(path, 2, 100) == 0
    assert admit(path, 2, 100) == 30
    assert admit(path, 2, 130) == 0
    assert admit(path, 2, 140) == 20
    assert admit(path, 2, 160) == 0
    with pytest.raises(ValueError, match="same RPM"):
        admit(path, 3, 200)


def test_config_requires_shared_file():
    with pytest.raises(ValueError, match="configured together"):
        SolverConfig(model="test", rpm_limit=128)


def test_processes_share_one_admission_slot(tmp_path):
    with ProcessPoolExecutor(max_workers=4) as pool:
        waits = list(pool.map(partial(admit, tmp_path / "shared.json", 128), [100.0] * 8))
    assert waits.count(0) == 1


def test_wait_does_not_hold_file_lock(tmp_path, monkeypatch):
    import pluginrsi.rate_limit as module
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    path = tmp_path / "rate.json"
    admit(path, 60, now[0])
    async def sleep(seconds):
        now[0] += seconds
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    asyncio.run(acquire(path, 60))
    assert now[0] >= 101


def test_429_feedback_never_lowers_configured_rpm(tmp_path):
    from pluginrsi.rate_limit import note_rate_limit
    from pluginrsi.search.store import read_json
    path = tmp_path / 'rate.json'
    assert admit(path, 300, 100) == 0
    for now in (101, 102, 180, 240):
        assert note_rate_limit(path, 300, now) == 300
    assert admit(path, 300, 241) == 0
    assert admit(path, 300, 241.1) == pytest.approx(.1)
    assert read_json(path)['rpm'] == 300


def test_legacy_one_rpm_and_cooldown_are_ignored(tmp_path):
    from pluginrsi.search.store import write_json, read_json
    path = tmp_path / 'rate.json'
    write_json(path, dict(rpm=300, effective_rpm=1, cooldown_until=10000, requests=[100]))
    assert admit(path, 300, 100.2) == 0
    assert read_json(path) == dict(rpm=300, requests=[100, 100.2])
