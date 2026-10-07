import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pluginrsi.schemas import ProposerConfig
from pluginrsi.search.loop import Search


def search_with_pool(capacity, propose):
    search = Search.__new__(Search)
    search.config = SimpleNamespace(proposer=ProposerConfig(command=['unused'], concurrency=capacity))
    search.libraries = []
    search.propose_slot = propose
    return search


@pytest.mark.parametrize('capacity,peak_expected', [(None, 8), (1, 1), (2, 2)])
def test_proposal_pool_limits_live_work_without_removing_candidates(capacity, peak_expected):
    async def run():
        active = peak = 0
        completed = []

        async def propose(slot, request, record, path):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                completed.append(slot['id'])
            finally:
                active -= 1

        search = search_with_pool(capacity, propose)
        slots = [dict(id=i, status='allocated') for i in range(8)]
        assert await search.proposal_wave(slots, [{}] * 8, {}, None)
        assert sorted(completed) == list(range(8))
        assert peak == peak_expected and active == 0

    asyncio.run(run())


def test_proposal_pool_cancels_running_and_queued_work():
    async def run():
        active = 0
        started = []
        full = asyncio.Event()

        async def propose(slot, request, record, path):
            nonlocal active
            active += 1
            started.append(slot['id'])
            if active == 2:
                full.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        search = search_with_pool(2, propose)
        slots = [dict(id=i, status='allocated') for i in range(8)]
        wave = asyncio.create_task(search.proposal_wave(slots, [{}] * 8, {}, None))
        await asyncio.wait_for(full.wait(), 1)
        wave.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wave
        assert active == 0 and len(started) == 2

    asyncio.run(run())


@pytest.mark.parametrize('capacity', [0, -1])
def test_proposal_concurrency_rejects_nonpositive_values(capacity):
    with pytest.raises(ValidationError):
        ProposerConfig(command=['unused'], concurrency=capacity)
