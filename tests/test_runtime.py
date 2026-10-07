import asyncio
from types import SimpleNamespace

import pytest

from pluginrsi.runtime import BudgetExceeded, InfraError, SolverAgent
from pluginrsi.schemas import SolverConfig


class Response:
    output_text = ""

    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, **kwargs):
        return self.payload


def agent_with_responses(monkeypatch, responses, **kwargs):
    monkeypatch.setenv("TEST_API_KEY", "fixture")
    monkeypatch.setenv("TEST_BASE_URL", "http://localhost:1/v1")
    events = []
    agent = SolverAgent(SolverConfig(model="fixture", api_key_env="TEST_API_KEY", base_url_env="TEST_BASE_URL",
                                    retry_delay_seconds=0, **kwargs), lambda name, **data: events.append((name, data)))

    async def create(**request):
        return responses.pop(0)

    agent.transport.client.responses.create = create
    return agent, events


def test_http_success_with_invalid_prompt_is_permanent(monkeypatch):
    from pluginrsi.model_transports import InvalidModelRequest
    agent, events = agent_with_responses(monkeypatch, [Response({"status": "failed", "output": [],
        "error": {"code": "invalid_prompt", "message": "invalid app"}})])

    async def run():
        try:
            with pytest.raises(InvalidModelRequest, match="invalid app"):
                await agent.complete([])
            assert agent.calls == 1
        finally:
            await agent.close()

    asyncio.run(run())


def test_tool_response_and_call_budget(monkeypatch):
    response = Response({"status": "completed", "output": [{"type": "function_call", "name": "terminal", "call_id": "one", "arguments": "{}"}],
                         "usage": {"total_tokens": 10}})
    agent, events = agent_with_responses(monkeypatch, [response], max_calls=1)

    async def run():
        try:
            result = await agent.complete([], [{'type': 'function', 'name': 'terminal', 'parameters': {'type': 'object'}}])
            assert result["output"][0]["name"] == "terminal"
            assert agent.tokens == 10
            with pytest.raises(BudgetExceeded):
                await agent.complete([])
        finally:
            await agent.close()

    asyncio.run(run())


def test_concurrent_responses_keep_their_request_ids(monkeypatch):
    agent, events = agent_with_responses(monkeypatch, [], max_calls=2)

    async def run():
        both_started = asyncio.Event()
        started = 0

        async def create(**request):
            nonlocal started
            started += 1
            label = request["input"][0]["content"]
            if started == 2:
                both_started.set()
            await both_started.wait()
            if label == "first":
                await asyncio.sleep(0)
            response = Response({"status": "completed", "output": [], "usage": {"total_tokens": 1}})
            response.output_text = label
            response.payload["label"] = label
            return response

        agent.transport.client.responses.create = create
        try:
            await asyncio.gather(agent.complete([{"role": "user", "content": "first"}]),
                                 agent.complete([{"role": "user", "content": "second"}]))
        finally:
            await agent.close()

    asyncio.run(run())
    responses = {data["response"]["label"]: data["call"] for name, data in events if name == "model/response"}
    assert responses == {"first": 1, "second": 2}
    assert agent.calls == 2 and agent.tokens == 2


def test_embedded_server_error_retries_within_the_existing_budget(monkeypatch):
    failed = Response({"status": "failed", "output": [], "error": {"code": "server_error", "message": "504 gateway timeout"}})
    good = Response({"status": "completed", "output": [], "usage": {"total_tokens": 3}})
    good.output_text = "recovered"
    agent, events = agent_with_responses(monkeypatch, [failed, good], max_attempts=2, max_calls=2)

    async def run():
        try:
            assert (await agent.complete([]))["output_text"] == "recovered"
        finally:
            await agent.close()

    asyncio.run(run())
    assert agent.calls == 2
    assert [data["call"] for name, data in events if name == "model/response"] == [1, 2]
    assert any(name == "model/retry" for name, _ in events)


def test_embedded_server_error_cannot_retry_past_call_budget(monkeypatch):
    failed = Response({"status": "failed", "output": [], "error": {"code": "server_error", "message": "504 gateway timeout"}})
    agent, events = agent_with_responses(monkeypatch, [failed], max_attempts=3, max_calls=1)

    async def run():
        try:
            with pytest.raises(InfraError, match="remaining call budget"):
                await agent.complete([])
        finally:
            await agent.close()

    asyncio.run(run())
    assert agent.calls == 1


@pytest.mark.parametrize("max_calls,max_attempts", [(2, 2), (1, 2), (2, 1)])
def test_embedded_rate_limit_uses_independent_retry_budget(monkeypatch, max_calls, max_attempts):
    failed = Response({"status": "failed", "output": [], "error": {"code": "rate_limit_exceeded"}})
    good = Response({"status": "completed", "output": []})
    good.output_text = "recovered"
    agent, events = agent_with_responses(monkeypatch, [failed] * 5 + [good], max_calls=max_calls,
                                        max_attempts=max_attempts)
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("pluginrsi.runtime.asyncio.sleep", sleep)

    async def run():
        try:
            assert (await agent.complete([]))["output_text"] == "recovered"
        finally:
            await agent.close()

    asyncio.run(run())
    assert agent.calls == 1 and agent.failed_calls == 5
    assert delays == [10, 20, 40, 60, 60]
    outcomes = [d for name, d in events if name == 'model/request_outcome']
    assert len({d['request_id'] for d in outcomes}) == 6


def test_http_429_honors_retry_after_and_preserves_call_budget(monkeypatch):
    import httpx
    from openai import RateLimitError
    agent, events = agent_with_responses(monkeypatch, [], max_calls=1, max_attempts=1)
    attempts = []
    delays = []

    async def complete(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            response = httpx.Response(429, headers={'retry-after': '37'}, request=httpx.Request('POST', 'http://localhost'))
            raise RateLimitError('limited', response=response, body=None)
        return dict(output_text='ok', output=[], status='completed')

    async def sleep(delay):
        delays.append(delay)

    agent.transport.complete = complete
    monkeypatch.setattr('pluginrsi.runtime.asyncio.sleep', sleep)

    async def run():
        try:
            assert (await agent.complete([]))['output_text'] == 'ok'
        finally:
            await agent.close()

    asyncio.run(run())
    assert delays == [37] and agent.calls == 1


def test_rate_limit_retry_deadline_is_bounded(monkeypatch):
    failed = Response({'status': 'failed', 'output': [], 'error': {'code': 'rate_limit_exceeded'}})
    agent, events = agent_with_responses(monkeypatch, [failed], max_calls=1, max_attempts=1,
                                        rate_limit_retry_seconds=.01)

    async def run():
        try:
            with pytest.raises(InfraError, match='rate-limit retry deadline'):
                await asyncio.wait_for(agent.complete([]), .5)
        finally:
            await agent.close()

    asyncio.run(run())
    assert agent.calls == 0


def test_text_format_is_forwarded_without_native_tools(monkeypatch):
    agent, events = agent_with_responses(monkeypatch, [])
    recorded = []

    async def create(**request):
        recorded.append(request)
        response = Response({"status": "completed", "output": []})
        response.output_text = '{"ok":true}'
        return response

    agent.transport.client.responses.create = create

    async def run():
        try:
            await agent.complete([], text_format={"type": "json_object"})
        finally:
            await agent.close()

    asyncio.run(run())
    assert recorded[0]["text"] == {"format": {"type": "json_object"}}
    assert "tools" not in recorded[0]


@pytest.mark.parametrize("failure", ["server_error", "empty", "timeout"])
def test_recovered_api_failure_cannot_become_a_normal_budget_loss(monkeypatch, failure):
    good = Response({"status": "completed", "output": [{"type": "function_call", "name": "terminal", "call_id": "one", "arguments": "{}"}]})
    if failure == "server_error":
        failed = Response({"status": "failed", "output": [], "error": {"code": "server_error"}})
    else:
        failed = Response({"status": "completed", "output": []})
    agent, events = agent_with_responses(monkeypatch, [failed, good], max_calls=2, max_attempts=2)
    if failure == "timeout":
        import httpx
        from openai import APITimeoutError
        attempts = 0

        async def create(**request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise APITimeoutError(request=httpx.Request("POST", "http://localhost/responses"))
            return good

        agent.transport.client.responses.create = create

    async def run():
        try:
            await agent.complete([], [{'type': 'function', 'name': 'terminal', 'parameters': {'type': 'object'}}])
            assert agent.calls == 2 and agent.failed_calls == 1
            with pytest.raises(InfraError, match="remaining call budget"):
                await agent.complete([])
            assert agent.calls == 2
        finally:
            await agent.close()

    asyncio.run(run())


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
def test_runtime_only_uses_transport_complete_and_close(monkeypatch, api_mode):
    monkeypatch.setenv("TEST_API_KEY", "fixture")
    monkeypatch.setenv("TEST_BASE_URL", "http://localhost:1/v1")
    selected = []

    class Transport:
        closed = False

        def __init__(self):
            self.requests = []

        async def complete(self, messages, tools=None, *, text_format=None, **context):
            self.requests.append((messages, tools, text_format, context))
            return {"status": "completed", "output": [], "output_text": "ok", "usage": {"total_tokens": 2}}

        async def close(self):
            self.closed = True

    transport = Transport()

    def factory(config, emit, **credentials):
        selected.append(config.api_mode)
        assert credentials == {"api_key": "fixture", "base_url": "http://localhost:1/v1"}
        return transport

    monkeypatch.setattr("pluginrsi.runtime.create_model_transport", factory)
    agent = SolverAgent(SolverConfig(model="fixture", api_mode=api_mode,
        api_key_env="TEST_API_KEY", base_url_env="TEST_BASE_URL", max_calls=2), lambda *a, **kw: None)

    async def run():
        try:
            for _ in range(2):
                assert (await agent.complete([], text_format={"type": "json_object"}))["output_text"] == "ok"
        finally:
            await agent.close()

    asyncio.run(run())
    assert selected == [api_mode]
    assert [r[3]["call"] for r in transport.requests] == [1, 2]
    assert all(r[2] == {"type": "json_object"} for r in transport.requests)
    assert agent.calls == 2 and agent.tokens == 4 and transport.closed
