import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from pluginrsi.evaluation.benchmarks.terminal_bench import evaluate_harbor
from pluginrsi.search.store import read_json, write_json


@pytest.mark.parametrize('reward', [0, 1])
@pytest.mark.parametrize('agent_status', ['completed', 'solver_limit', 'infra_error', 'candidate_error', 'solver_error'])
@pytest.mark.parametrize('exception_type', ['ServerInternalError', 'AgentTimeoutError', 'VerifierTimeoutError'])
def test_valid_reward_survives_cleanup_error(tmp_path, monkeypatch, reward, agent_status, exception_type):
    for name in ('harbor', 'harbor.models', 'harbor.models.trial', 'harbor.trial'):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    config = ModuleType('harbor.models.trial.config')
    for name in ('AgentConfig', 'EnvironmentConfig', 'TaskConfig', 'TrialConfig'):
        setattr(config, name, SimpleNamespace)
    monkeypatch.setitem(sys.modules, config.__name__, config)
    error = dict(exception_type=exception_type, exception_message='failure after verifier produced a reward')
    raw = dict(verifier_result=dict(rewards=dict(reward=reward)), exception_info=error)

    class Trial:
        @classmethod
        async def create(cls, config):
            assert config.environment.override_gpus == 0
            return cls()

        async def run(self):
            return SimpleNamespace(model_dump=lambda **kwargs: raw)

    trial = ModuleType('harbor.trial.trial')
    trial.Trial = Trial
    monkeypatch.setitem(sys.modules, trial.__name__, trial)
    agent = dict(status=agent_status)
    if agent_status == 'solver_error':
        agent['detail'] = 'invalid_tool_call'
    write_json(tmp_path / 'agent_status.json', agent)
    request = dict(work_dir=str(tmp_path), task=dict(task_path=str(tmp_path), id='fixture'),
                   solver=dict(model='fixture'), harness=str(tmp_path), libraries=[],
                   evaluation=dict(task_timeout_seconds=10, benchmark_options={}))
    result = asyncio.run(evaluate_harbor(request))
    assert read_json(tmp_path / 'harbor_result.json') == raw
    if agent_status == 'candidate_error':
        assert result['status'] == 'candidate_error'
    elif agent_status == 'solver_error':
        assert result['status'] == 'completed' and result['score'] == 0
        assert result['detail'] == 'invalid_tool_call' and result['retryable'] is False
    else:
        assert result['score'] == reward
        assert result['status'] == ('solver_limit' if agent_status == 'solver_limit' else 'completed')
        assert result['post_verification_error'] == error
