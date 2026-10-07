import asyncio
import json

import httpx
import pytest
from openai import APITimeoutError, AsyncOpenAI
from openai.types.chat import ChatCompletion

from pluginrsi.model_transports import (
    ChatCompletionsTransport, ResponsesTransport,
    _chat_messages, _chat_request, _chat_response,
)
from pluginrsi.runtime import InvalidToolCall, SolverAgent
from pluginrsi.schemas import SolverConfig


def completion(content=None, calls=None):
    return ChatCompletion.model_validate({"id": "test", "created": 0, "model": "fixture",
        "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
        "message": {"role": "assistant", "content": content, "tool_calls": calls}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})


def test_tool_roundtrip_preserves_multiple_call_ids():
    calls = [{"id": name, "type": "function", "function": {"name": "terminal", "arguments": '{}'}} for name in ("a", "b")]
    normalized = _chat_response(completion("Checking", calls))
    messages = [{"role": "user", "content": "Inspect two files"}] + normalized["output"]
    messages += [{"type": "function_call_output", "call_id": name, "output": name + " result"} for name in ("a", "b")]
    actual = _chat_messages(messages)
    assert len(actual) == 4
    assert [c["id"] for c in actual[1]["tool_calls"]] == ["a", "b"]
    assert actual[2] == {"role": "tool", "tool_call_id": "a", "content": "a result"}
    assert actual[3]["tool_call_id"] == "b"
    assert normalized["usage"]["input_tokens"] == 3
    assert normalized["usage"]["total_tokens"] == 5


def test_chat_parameters_and_json_schema():
    request = {"model": "fixture", "input": [{"role": "user", "content": "JSON"}],
               "max_output_tokens": 500, "reasoning": {"effort": "xhigh"},
               "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
               "text": {"format": {"type": "json_schema", "name": "answer", "strict": True, "schema": {"type": "object"}}}}
    actual = _chat_request(request)
    assert actual["max_tokens"] == 500 and actual["reasoning_effort"] == "xhigh"
    assert actual["tools"][0]["function"]["name"] == "lookup"
    assert actual["response_format"]["json_schema"]["name"] == "answer"
    assert "input" not in actual and "store" not in actual


@pytest.mark.parametrize('history', [
    [{'role': 'assistant', 'content': ''}],
    [{'type': 'function_call', 'call_id': 'other', 'name': 'terminal', 'arguments': '{}'}],
    [{'type': 'function_call', 'call_id': 'one', 'name': 'terminal', 'arguments': '{}'},
     {'type': 'function_call_output', 'call_id': 'one', 'output': 'already returned'}],
])
def test_orphan_tool_output_is_candidate_error_without_http_or_retry(monkeypatch, history):
    from pluginrsi.model_transports import InvalidModelRequest
    from pluginrsi.runtime import InfraError

    monkeypatch.setenv('TEST_KEY', 'fixture')
    monkeypatch.setenv('TEST_URL', 'https://fixture.invalid/v1')
    agent = SolverAgent(SolverConfig(model='fixture', api_mode='chat_completions',
        api_key_env='TEST_KEY', base_url_env='TEST_URL', rpm_limit=None, max_attempts=4),
        lambda *args, **kwargs: None)
    requests = []

    async def create(**request):
        requests.append(request)
        raise AssertionError('Invalid history must not reach the API')

    agent.transport.client.chat.completions.create = create

    async def run():
        try:
            with pytest.raises(InvalidModelRequest, match='no matching function_call') as error:
                await agent.complete([*history,
                    {'type': 'function_call_output', 'call_id': 'one', 'output': 'result'}])
            assert not isinstance(error.value, InfraError)
            assert agent.calls == 1
            assert requests == []
        finally:
            await agent.close()

    asyncio.run(run())


@pytest.mark.parametrize('call_ids', [('',), (None,), ('one', 'one')])
def test_chat_history_rejects_missing_and_duplicate_call_ids(call_ids):
    from pluginrsi.model_transports import InvalidModelRequest

    with pytest.raises(InvalidModelRequest, match='missing or duplicate call_id'):
        _chat_messages([{'type': 'function_call', 'call_id': call_id,
                         'name': 'terminal', 'arguments': '{}'} for call_id in call_ids])


def test_explicit_thinking_switch_requires_chat_transport():
    with pytest.raises(ValueError, match='thinking_mode requires chat_completions'):
        SolverConfig(model='fixture', thinking_mode='disabled')


def test_disabled_thinking_reaches_chat_wire_without_reasoning_effort(monkeypatch):
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['thinking'] == {'type': 'disabled'}
        assert 'reasoning_effort' not in body and 'extra_body' not in body
        return httpx.Response(200, json=completion('OK').model_dump(mode='json'))

    def client(**kwargs):
        return AsyncOpenAI(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), **kwargs)

    monkeypatch.setattr('pluginrsi.model_transports.AsyncOpenAI', client)
    config = SolverConfig(model='glm-5.2', api_mode='chat_completions', reasoning_effort=None, thinking_mode='disabled')
    transport = ChatCompletionsTransport(config, lambda *a, **kw: None, api_key='fixture', base_url='https://fixture.invalid/v1')

    async def run():
        try:
            result = await transport.complete([{'role': 'user', 'content': 'OK'}])
            assert result['output_text'] == 'OK'
        finally:
            await transport.close()

    asyncio.run(run())
    assert len(requests) == 1


def test_reasoning_only_reply_is_not_promoted_to_final_answer():
    response = completion()
    response.choices[0].message.model_extra['reasoning_content'] = 'internal reasoning'
    result = _chat_response(response)
    assert result['output_text'] == '' and result['output'] == []


@pytest.mark.parametrize('text,passed', [('{"answer":42}', True), ('[]', False),
                                       ('not JSON', False), ('{"answer":NaN}', False)])
def test_prompt_json_mode_validates_object_without_native_format(monkeypatch, text, passed):
    def handle(request):
        body = json.loads(request.content)
        assert 'response_format' not in body
        assert body['messages'][0]['role'] == 'system'
        assert 'JSON object' in body['messages'][0]['content']
        return httpx.Response(200, json=completion(text).model_dump(mode='json'))

    def client(**kwargs):
        return AsyncOpenAI(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), **kwargs)

    monkeypatch.setattr('pluginrsi.model_transports.AsyncOpenAI', client)
    config = SolverConfig(model='kimi-k3-test-dy', api_mode='chat_completions', chat_json_mode='prompt')
    transport = ChatCompletionsTransport(config, lambda *a, **kw: None, api_key='fixture', base_url='https://fixture.invalid/v1')

    async def run():
        try:
            result = await transport.complete([{'role': 'user', 'content': 'answer'}], text_format={'type': 'json_object'})
            assert (result['status'] == 'completed') == passed
            if not passed:
                assert result['error']['code'] == 'server_error'
        finally:
            await transport.close()

    asyncio.run(run())


def test_prompt_json_mode_does_not_silently_weaken_json_schema():
    from pluginrsi.model_transports import InvalidModelRequest
    with pytest.raises(InvalidModelRequest, match='JSON Schema is not emulated'):
        _chat_request(dict(model='fixture', input=[], max_output_tokens=10,
                           text={'format': {'type': 'json_schema', 'schema': {'type': 'object'}}}), 'prompt')
    with pytest.raises(ValueError, match='chat_json_mode requires chat_completions'):
        SolverConfig(model='fixture', chat_json_mode='prompt')


def test_chat_runtime_retries_and_records_attempt_failure_rate(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_API_KEY", "fixture")
    monkeypatch.setenv("TEST_BASE_URL", "http://localhost:1/v1")
    metrics = tmp_path / "api.jsonl"
    agent = SolverAgent(SolverConfig(model="fixture", api_mode="chat_completions",
        api_key_env="TEST_API_KEY", base_url_env="TEST_BASE_URL", api_metrics_path=metrics,
        max_calls=2, max_attempts=2, retry_delay_seconds=0), lambda *a, **kw: None)
    requests = []

    async def create(**request):
        requests.append(request)
        if len(requests) == 1:
            raise APITimeoutError(request=httpx.Request("POST", "http://localhost/chat/completions"))
        return completion('{"ok":true}')

    agent.transport.client.chat.completions.create = create

    async def run():
        try:
            result = await agent.complete([{"role": "user", "content": "Return JSON"}], text_format={"type": "json_object"})
            assert result["output_text"] == '{"ok":true}'
        finally:
            await agent.close()

    asyncio.run(run())
    assert requests[-1]["response_format"] == {"type": "json_object"}
    events = [json.loads(line) for line in metrics.read_text().splitlines()]
    starts = [e for e in events if e["event"] == "api/request_start"]
    ends = [e for e in events if e["event"] == "api/request_end"]
    assert len(starts) == len(ends) == 2
    assert [e["success"] for e in ends] == [False, True]
    assert ends[0]["error_type"] == "APITimeoutError"
    assert ends[0]['usage'] is None
    assert ends[1]['usage']['input_tokens'] == 3 and ends[1]['usage']['output_tokens'] == 2
    assert [e["request_id"] for e in starts] == [e["request_id"] for e in ends]
    assert agent.failed_calls == 1 and agent.tokens == 5


def test_html_is_not_a_successful_chat_response():
    result = _chat_response('<html>gateway</html>')
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_response"


def test_unsupported_content_is_not_silently_dropped():
    with pytest.raises(ValueError, match="unsupported chat content"):
        _chat_messages([{"role": "user", "content": [{"type": "input_image"}]}])


def test_missing_chat_usage_is_not_reported_as_zero_tokens():
    response = completion('ok')
    response.usage = None
    assert _chat_response(response)['usage'] is None


def test_mixed_solver_evolver_use_independent_transports_and_close_clients(monkeypatch):
    from pluginrsi.schemas import ProposerConfig

    monkeypatch.setenv("SOLVER_KEY", "solver-fixture")
    monkeypatch.setenv("SOLVER_URL", "https://solver.invalid/v1")
    monkeypatch.setenv("EVOLVER_KEY", "evolver-fixture")
    monkeypatch.setenv("EVOLVER_URL", "https://evolver.invalid/v1")
    received = {}
    clients = []

    def handle(request):
        body = json.loads(request.content)
        received[request.url.host] = (request, body)
        if request.url.host == "solver.invalid":
            return httpx.Response(200, json=completion("ok").model_dump(mode="json"))
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 0,
            "model": body["model"], "status": "completed",
            "output": [{"id": "msg_test", "type": "message", "status": "completed",
                        "role": "assistant", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        })

    def client(**kwargs):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.append(http)
        return AsyncOpenAI(http_client=http, **kwargs)

    monkeypatch.setattr("pluginrsi.model_transports.AsyncOpenAI", client)
    events = []
    emit = lambda name, **data: events.append((name, data))
    solver_config = SolverConfig(model="terra-fixture", api_mode="chat_completions",
        api_key_env="SOLVER_KEY", base_url_env="SOLVER_URL", max_output_tokens=64, max_calls=1)
    proposer_config = ProposerConfig(command=["unused"], agent={
        "model": "sol-fixture", "api_mode": "responses", "api_key_env": "EVOLVER_KEY",
        "base_url_env": "EVOLVER_URL", "max_output_tokens": 128, "reasoning_effort": "xhigh", "max_calls": 2,
    })
    solver = SolverAgent(solver_config, emit)
    evolver = SolverAgent(proposer_config.agent, emit)
    assert isinstance(solver.transport, ChatCompletionsTransport)
    assert isinstance(evolver.transport, ResponsesTransport)

    async def run():
        try:
            results = await asyncio.gather(solver.complete([{"role": "user", "content": "solve"}]),
                evolver.complete([{"role": "user", "content": "evolve"}], text_format={"type": "json_object"}))
            assert [r["output_text"] for r in results] == ["ok", "ok"]
            assert all(r["status"] == "completed" and r["usage"]["total_tokens"] == 5 for r in results)
        finally:
            await solver.close()
            await evolver.close()

    asyncio.run(run())
    request, body = received["solver.invalid"]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer solver-fixture"
    assert body["model"] == "terra-fixture" and body["max_tokens"] == 64
    assert body["reasoning_effort"] == "none" and body["messages"][0]["content"] == "solve"
    request, body = received["evolver.invalid"]
    assert request.url.path == "/v1/responses"
    assert request.headers["authorization"] == "Bearer evolver-fixture"
    assert body["model"] == "sol-fixture" and body["max_output_tokens"] == 128
    assert body["reasoning"] == {"effort": "xhigh"}
    assert body["text"] == {"format": {"type": "json_object"}}
    assert solver.calls == evolver.calls == 1 and solver.tokens == evolver.tokens == 5
    requests = [data for name, data in events if name == "model/request"]
    assert {r["api_mode"] for r in requests} == {"responses", "chat_completions"}
    assert all(r["call"] == 1 and r["attempt"] == 0 and "input" in r["request"] for r in requests)
    assert all(c.is_closed for c in clients)


@pytest.mark.parametrize('policy', ['strict', 'passthrough'])
def test_configured_tool_validation_preserves_terminal_compatibility(monkeypatch, policy):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    from pluginrsi.contracts import ToolResult

    calls = []
    arguments = {'command': 'printf OK', 'timeout': 3}
    reply = completion(calls=[{'id': 'call-1', 'type': 'function',
                              'function': {'name': 'terminal', 'arguments': json.dumps(arguments)}}])

    def handle(request):
        body = json.loads(request.content)
        assert body['tools'][0]['function']['parameters']['additionalProperties'] is False
        calls.append(body)
        return httpx.Response(200, json=reply.model_dump(mode='json'))

    def client(**kwargs):
        return AsyncOpenAI(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), **kwargs)

    monkeypatch.setattr('pluginrsi.model_transports.AsyncOpenAI', client)
    monkeypatch.setenv('TEST_API_KEY', 'fixture')
    monkeypatch.setenv('TEST_BASE_URL', 'https://fixture.invalid/v1')
    config = SolverConfig(model='fixture', api_mode='chat_completions', api_key_env='TEST_API_KEY',
                          base_url_env='TEST_BASE_URL', max_attempts=1, tool_call_validation=policy)
    agent = SolverAgent(config, lambda *args, **kwargs: None)
    spec = importlib.util.spec_from_file_location('terminal_fixture', Path(__file__).resolve().parents[1] /
        'seeds/plugin_library/tool/terminal/v0001/implementation.py')
    terminal_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(terminal_module)
    executed = []

    async def execute(command, timeout):
        executed.append((command, timeout))
        return ToolResult('OK')

    terminal = terminal_module.Terminal({'timeout_seconds': 180, 'max_output_bytes': 30000},
                                       SimpleNamespace(environment=SimpleNamespace(execute=execute)))

    async def run():
        try:
            if policy == 'strict':
                with pytest.raises(InvalidToolCall, match='invalid_tool_call'):
                    await agent.complete([{'role': 'user', 'content': 'inspect'}], [terminal.schema()])
            else:
                result = await agent.complete([{'role': 'user', 'content': 'inspect'}], [terminal.schema()])
                received = json.loads(result['output'][0]['arguments'])
                assert received == arguments
                await terminal.execute(received)
        finally:
            await agent.close()

    asyncio.run(run())
    assert len(calls) == 1
    assert executed == ([] if policy == 'strict' else [('printf OK', 180)])


def test_tool_validation_default_is_strict_and_unknown_policy_is_rejected():
    assert SolverConfig(model='fixture').tool_call_validation == 'strict'
    with pytest.raises(ValueError):
        SolverConfig(model='fixture', tool_call_validation='v3.1')
