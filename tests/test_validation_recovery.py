import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pluginrsi.runtime import InfraError
from pluginrsi.schemas import ProposerConfig, SearchOptions
from pluginrsi.search.loop import Search
from pluginrsi.search.store import write_json


def validator(tmp_path, attempts=2):
    search = Search.__new__(Search)
    search.config = SimpleNamespace(search=SearchOptions(), proposer=ProposerConfig(command=['unused'],
        validation_timeout_seconds=180, validation_max_attempts=attempts))
    return search


@pytest.mark.parametrize('failure', [TimeoutError(), OSError('launch failed'), -15])
def test_transient_validation_retry_is_bounded_and_recorded(tmp_path, monkeypatch, failure):
    calls = []

    async def command(*args):
        calls.append(args[-1])
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return 0

    monkeypatch.setattr('pluginrsi.search.loop.run_command', command)
    slot = dict(proposal_attempts=1)
    asyncio.run(validator(tmp_path).validate_command([], tmp_path, slot, {'slots': [slot]}, tmp_path/'phase.json'))
    assert calls == [180, 180]
    assert slot['validation_attempts'] == 2 and slot['proposal_attempts'] == 1
    assert len(slot['validation_errors']) == 1 and 'limit=180' in slot['validation_errors'][0]


def test_validation_timeout_exhaustion_has_actionable_detail(tmp_path, monkeypatch):
    async def command(*args):
        raise TimeoutError()

    monkeypatch.setattr('pluginrsi.search.loop.run_command', command)
    slot = {}
    with pytest.raises(InfraError, match='TimeoutError.*limit=180.*attempt=2/2'):
        asyncio.run(validator(tmp_path).validate_command([], tmp_path, slot, {'slots': [slot]}, tmp_path/'phase.json'))
    assert slot['validation_attempts'] == len(slot['validation_errors']) == 2


def test_invalid_candidate_is_not_retried(tmp_path, monkeypatch):
    async def command(*args):
        return 1

    monkeypatch.setattr('pluginrsi.search.loop.run_command', command)
    slot = {}
    with pytest.raises(ValueError, match='loader validation failed'):
        asyncio.run(validator(tmp_path).validate_command([], tmp_path, slot, {'slots': [slot]}, tmp_path/'phase.json'))
    assert slot['validation_attempts'] == 1 and 'validation_errors' not in slot


@pytest.mark.parametrize('retry,submission,expected', [(True, True, 'ready'), (False, True, 'infra_error'), (True, False, 'infra_error')])
def test_resume_revalidates_only_existing_validation_failure(tmp_path, monkeypatch, retry, submission, expected):
    async def command(*args):
        return 0

    monkeypatch.setattr('pluginrsi.search.loop.run_command', command)
    monkeypatch.setattr('pluginrsi.search.loop.validate_proposal', lambda *a, **k: [])
    search = validator(tmp_path)
    search.store = SimpleNamespace(candidate=lambda cid: tmp_path)
    search.evaluator = SimpleNamespace(retry_infra=retry)
    slot = dict(id='c1', parent='c0', status='infra_error', reason='validation_infra_error', detail='timeout', proposal_attempts=1)
    if submission:
        write_json(tmp_path/'proposal_result.json', dict(hypothesis='fixture', changes=[], new_plugin_refs=[]))
    request = dict(phase='harness_recomposition', library_dirs=[], allowed_plugin_refs=[])
    asyncio.run(search.propose_slot(slot, request, {'iteration': 1, 'slots': [slot]}, tmp_path/'phase.json'))
    assert slot['status'] == expected and slot['proposal_attempts'] == 1
    assert (tmp_path/'candidate.yaml').exists() == (expected == 'ready')


def test_validation_cancellation_propagates_without_retry(tmp_path, monkeypatch):
    async def command(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr('pluginrsi.search.loop.run_command', command)
    slot = {}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(validator(tmp_path).validate_command([], tmp_path, slot, {'slots': [slot]}, tmp_path/'phase.json'))
    assert slot['validation_attempts'] == 1 and 'validation_errors' not in slot


@pytest.mark.parametrize('fields', [{'validation_timeout_seconds': 0}, {'validation_max_attempts': 0}])
def test_validation_limits_reject_nonpositive_values(fields):
    with pytest.raises(ValidationError):
        ProposerConfig(command=['unused'], **fields)
