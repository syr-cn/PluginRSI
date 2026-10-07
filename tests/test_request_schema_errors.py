import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from pluginrsi.model_transports import InvalidModelRequest, response_schema
from pluginrsi.runtime import SolverAgent, ModelRequestRejected
from pluginrsi.schemas import SolverConfig


def test_apply_patch_union_conversion_preserves_accepted_inputs():
    path=Path(__file__).resolve().parents[1]/'seeds/plugin_library/tool/prior_apply_patch/v0001/schema.json'
    original=json.loads(path.read_text())['parameters'];converted=response_schema(original)
    assert 'oneOf' in original['properties']['patch']
    assert 'anyOf' in converted['properties']['patch']
    Draft202012Validator.check_schema(converted)
    for patch in ('diff text',[],42,None,{},[dict(path='a',old='x',new='y')]):
        value=dict(patch=patch)
        assert Draft202012Validator(original).is_valid(value)==Draft202012Validator(converted).is_valid(value)


def test_overlapping_oneof_is_not_silently_weakened():
    with pytest.raises(InvalidModelRequest,match='disjoint'):
        response_schema(dict(oneOf=[dict(type='number'),dict(type='integer')]))
    schema=dict(type='object',properties={'oneOf':dict(type='string')})
    assert response_schema(schema)==schema


def test_invalid_prompt_is_permanent_and_not_infrastructure_retry(monkeypatch):
    monkeypatch.setenv('TEST_API_KEY','fixture');monkeypatch.setenv('TEST_BASE_URL','http://localhost:1/v1')
    agent=SolverAgent(SolverConfig(model='fixture',api_key_env='TEST_API_KEY',base_url_env='TEST_BASE_URL',max_attempts=4),lambda *a,**k:None)
    calls=[]
    async def complete(*args,**kwargs):
        calls.append(True)
        return dict(output=[],output_text='',status='failed',error=dict(code='invalid_prompt',message='invalid tool schema'))
    agent.transport.complete=complete
    async def exercise():
        try:
            with pytest.raises(InvalidModelRequest,match='invalid tool schema'):
                await agent.complete([dict(role='user',content='fixture')])
        finally:
            await agent.close()
    asyncio.run(exercise())
    assert len(calls)==1 and agent.failed_calls==1


@pytest.mark.parametrize('transport_error', [True, False])
def test_provider_policy_rejection_is_single_task_nonretryable_infra(monkeypatch, transport_error):
    import httpx
    from openai import BadRequestError
    monkeypatch.setenv('TEST_API_KEY', 'fixture')
    monkeypatch.setenv('TEST_BASE_URL', 'http://localhost:1/v1')
    agent = SolverAgent(SolverConfig(model='fixture', api_key_env='TEST_API_KEY',
                        base_url_env='TEST_BASE_URL', max_attempts=4), lambda *a, **k: None)
    calls = []
    error = dict(code='validation_error', type='invalid_request_error',
                 message='Your request was flagged as potentially violating our usage policy.')
    async def complete(*args, **kwargs):
        calls.append(True)
        if transport_error:
            response = httpx.Response(400, request=httpx.Request('POST', 'http://localhost/v1'))
            raise BadRequestError(error['message'], response=response, body={'error': error})
        return dict(output=[], output_text='', status='failed', error=error)
    agent.transport.complete = complete
    async def exercise():
        try:
            with pytest.raises(ModelRequestRejected) as caught:
                await agent.complete([dict(role='user', content='fixture')])
            assert caught.value.retryable is False
        finally:
            await agent.close()
    asyncio.run(exercise())
    assert len(calls) == 1 and agent.failed_calls == 1
