import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from pluginrsi.evaluation.runner import Evaluator
from pluginrsi.evaluation.benchmarks.terminal_bench import evaluate_harbor
from pluginrsi.model_transports import validate_tool_calls
from pluginrsi.runtime import InvalidToolCall
from pluginrsi.search.loop import initialize
from pluginrsi.search.selection import is_valid
from pluginrsi.search.store import read_json, write_json
from test_full_search import config_at

TOOLS = [{'type': 'function', 'name': 'terminal', 'parameters': {
    'type': 'object', 'properties': {'command': {'type': 'string'}},
    'required': ['command'], 'additionalProperties': False}}]
BAD_NAME = 'garbage text <tool_call>terminal'


def call():
    return dict(type='function_call', call_id='one', name='terminal', arguments='{"command":"true"}')


@pytest.mark.parametrize('change', [
    {'name': BAD_NAME}, {'name': 'content_filter'}, {'name': None}, {'call_id': ''},
    {'arguments': 'invalid'}, {'arguments': '[]'}, {'arguments': '{}'},
    {'arguments': '{"command":42}'}, {'arguments': None},
])
def test_invalid_tool_output_is_identified_before_dispatch(change):
    item = call()
    item.update(change)
    result = validate_tool_calls({'status': 'completed', 'output': [item], 'usage': {'total_tokens': 12}}, TOOLS)
    assert result['error']['code'] == 'invalid_tool_call'
    assert result['status'] == 'failed' and result['usage']['total_tokens'] == 12


def test_duplicate_call_ids_reject_whole_reply_before_any_dispatch():
    result = validate_tool_calls({'status': 'completed', 'output': [call(), call()]}, TOOLS)
    assert result['error']['code'] == 'invalid_tool_call'
    result = validate_tool_calls({'status': 'completed', 'output': [call()]}, [])
    assert result['error']['code'] == 'invalid_tool_call'


@pytest.mark.parametrize('bad_name,arguments', [
    (BAD_NAME, '{"command":"true"}'),
    ('content_filter', '{"command":"true"}'),
    ('terminal', '{"command":"true","timeout":120000}'),
    ('terminal', '{"command":"true","timeout_ms":120000}'),
    ('terminal', 'invalid JSON'),
])
def test_invalid_tool_reply_is_valid_zero_without_api_or_task_retry(tmp_path, monkeypatch, bad_name, arguments):
    for name in ('harbor', 'harbor.agents', 'harbor.models', 'harbor.models.trial', 'harbor.trial'):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    base = ModuleType('harbor.agents.base')
    base.BaseAgent = object
    monkeypatch.setitem(sys.modules, base.__name__, base)
    path = Path(__file__).resolve().parents[1] / 'src/pluginrsi/evaluation/benchmarks/harbor_agent.py'
    spec = importlib.util.spec_from_file_location('pluginrsi.evaluation.benchmarks.test_harbor_agent', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = config_at(tmp_path)
    config.solver.api_mode = 'chat_completions'
    config.solver.api_key_env = 'TEST_KEY'
    config.solver.base_url_env = 'TEST_URL'
    config.solver.max_attempts = 2
    config.solver.max_calls = 2
    config.solver.retry_delay_seconds = 0
    config.evaluation.infra_retry_attempts = 1
    config.evaluation.fail_fast_candidate_errors = True
    monkeypatch.setenv('TEST_KEY', 'fixture')
    monkeypatch.setenv('TEST_URL', 'https://fixture.invalid/v1')
    store = initialize(config)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        message = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'type': 'function', 'id': 'one', 'function': {'name': bad_name, 'arguments': arguments}}]}
        return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 0,
            'model': 'fixture', 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': message}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 2, 'total_tokens': 4}})

    def client(**kwargs):
        return AsyncOpenAI(http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), **kwargs)

    monkeypatch.setattr('pluginrsi.model_transports.AsyncOpenAI', client)
    attempts = []

    trial_config = ModuleType('harbor.models.trial.config')
    for name in ('AgentConfig', 'EnvironmentConfig', 'TaskConfig', 'TrialConfig'):
        setattr(trial_config, name, SimpleNamespace)
    monkeypatch.setitem(sys.modules, trial_config.__name__, trial_config)

    class Trial:
        @classmethod
        async def create(cls, config):
            instance = cls()
            instance.agent = module.HarnessAgent(**config.agent.kwargs)
            return instance

        async def run(self):
            with pytest.raises(InvalidToolCall):
                await self.agent.run('Return done.', SimpleNamespace(), None)
            return SimpleNamespace(model_dump=lambda **kwargs: {
                'exception_info': {'exception_type': 'InvalidToolCall'}})

    trial_module = ModuleType('harbor.trial.trial')
    trial_module.Trial = Trial
    monkeypatch.setitem(sys.modules, trial_module.__name__, trial_module)

    async def worker(command, cwd, log_path, timeout, env=None):
        request = read_json(cwd / 'request.json')
        attempts.append(cwd)
        request['evaluation']['verifier_timeout_seconds'] = None
        result = await evaluate_harbor(request)
        assert result['score'] == 0 and result['status'] == 'completed'
        assert result['detail'] == 'invalid_tool_call' and not result['retryable']
        assert result['failure_origin'] == 'solver'
        status = read_json(cwd / 'agent_status.json')
        assert status['status'] == 'solver_error'
        usage = read_json(cwd / 'usage.json')
        assert usage['model_calls'] == 1 and usage['solver_tokens'] == 4
        assert usage['model_api_failures'] == 0
        write_json(cwd / 'result.json', result)
        return 0

    monkeypatch.setattr('pluginrsi.evaluation.runner.run_command', worker)
    tasks = [{'id': 'probe', 'task_path': str(tmp_path)}]
    result = asyncio.run(Evaluator(config, store).evaluate('c000000', tasks, 'selection', 'probe'))
    assert result['status'] == 'completed' and result['valid_task_count'] == 1
    assert is_valid(result) and result['mean_score'] == 0 and result['excluded_task_ids'] == []
    result = asyncio.run(Evaluator(config, store, retry_infra=True).evaluate('c000000', tasks, 'selection', 'probe'))
    assert is_valid(result) and result['mean_score'] == 0
    assert [path.name for path in attempts] == ['attempt_1']
    assert len(requests) == 1
