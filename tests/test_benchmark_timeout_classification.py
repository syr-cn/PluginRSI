import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from pluginrsi.evaluation.benchmarks.terminal_bench import evaluate_harbor
from pluginrsi.search.store import write_json


@pytest.mark.parametrize("failures,agent_status,expected,exception_type", [
    (0, "completed", "infra_error", "AgentTimeoutError"),
    (1, "completed", "infra_error", "AgentTimeoutError"),
    (0, "infra_error", "infra_error", "AgentTimeoutError"),
    (0, "candidate_error", "candidate_error", "AgentTimeoutError"),
    (1, "infra_error", "infra_error", "ModelRequestRejected"),
])
def test_trial_timeout_preserves_infrastructure_and_candidate_failure_causes(tmp_path, monkeypatch, failures, agent_status, expected, exception_type):
    for name in ("harbor", "harbor.models", "harbor.models.trial", "harbor.trial"):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    config = ModuleType("harbor.models.trial.config")
    for name in ("AgentConfig", "EnvironmentConfig", "TaskConfig", "TrialConfig"):
        setattr(config, name, SimpleNamespace)
    monkeypatch.setitem(sys.modules, config.__name__, config)

    class Trial:
        @classmethod
        async def create(cls, config):
            assert config.environment.override_gpus == 0
            return cls()

        async def run(self):
            return SimpleNamespace(model_dump=lambda **kwargs: {"exception_info": {"exception_type": exception_type}})

    trial = ModuleType("harbor.trial.trial")
    trial.Trial = Trial
    monkeypatch.setitem(sys.modules, trial.__name__, trial)
    write_json(tmp_path / "usage.json", {"model_calls": 20, "model_api_failures": failures})
    write_json(tmp_path / "agent_status.json", {"status": agent_status, "retryable": exception_type != 'ModelRequestRejected'})
    request = {"work_dir": str(tmp_path), "task": {"task_path": str(tmp_path), "id": "fixture"},
               "solver": {"model": "fixture"}, "harness": str(tmp_path), "libraries": [],
               "evaluation": {"task_timeout_seconds": 10, "benchmark_options": {}}}
    result = asyncio.run(evaluate_harbor(request))
    assert result['score'] == 0 and result['status'] == expected and result['detail'] == exception_type
    if exception_type == 'ModelRequestRejected':
        assert result['retryable'] is False
