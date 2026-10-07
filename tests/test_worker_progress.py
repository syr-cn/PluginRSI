import asyncio
from pathlib import Path

import pytest

from pluginrsi.evaluation import progress
from pluginrsi.search.store import read_json


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(progress, 'POLL_SECONDS', .01)
    monkeypatch.setattr(progress, 'GRACE_SECONDS', .02)


def test_lifecycle_reserves_verification_time_after_solver(tmp_path, monkeypatch):
    async def command(*args, **kwargs):
        progress.mark(tmp_path, 'environment')
        await asyncio.sleep(.02)
        progress.mark(tmp_path, 'solving')
        await asyncio.sleep(.15)
        progress.mark(tmp_path, 'preparing_verifier')
        await asyncio.sleep(.02)
        progress.mark(tmp_path, 'verifying')
        await asyncio.sleep(.15)
        progress.mark(tmp_path, 'done')
        return 0
    monkeypatch.setattr(progress, 'run_command', command)
    progress.mark(tmp_path, 'initializing')
    result = asyncio.run(progress.run_worker_command([],tmp_path,tmp_path/'log',2,env={},progress_path=tmp_path/'worker_stage.json',
        phase_limits=dict(initializing=1,solving=.25,verifying=.25,infrastructure=.25)))
    assert result == 0
    record = read_json(tmp_path/'worker_stage.json')
    assert record['stage'] == 'done' and record['spent_seconds']['solving'] >= .15
    assert record['spent_seconds']['verifying'] >= .15


def test_initialization_deadline_cancels_and_joins_process(tmp_path, monkeypatch):
    cancelled=[]
    async def command(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(progress, 'run_command', command)
    progress.mark(tmp_path, 'initializing')
    with pytest.raises(TimeoutError,match='initializing'):
        asyncio.run(progress.run_worker_command([],tmp_path,tmp_path/'log',2,env={},progress_path=tmp_path/'worker_stage.json',
            phase_limits=dict(initializing=.05,solving=1,verifying=1,infrastructure=1)))
    assert cancelled == [True]


def test_infrastructure_budget_is_shared_across_setup_and_transfer(tmp_path, monkeypatch):
    cancelled=[]
    async def command(*args, **kwargs):
        try:
            progress.mark(tmp_path,'environment')
            await asyncio.sleep(.1)
            progress.mark(tmp_path,'solving')
            await asyncio.sleep(.01)
            progress.mark(tmp_path,'preparing_verifier')
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(progress,'run_command',command)
    progress.mark(tmp_path,'initializing')
    with pytest.raises(TimeoutError,match='preparing_verifier'):
        asyncio.run(progress.run_worker_command([],tmp_path,tmp_path/'log',2,env={},progress_path=tmp_path/'worker_stage.json',
            phase_limits=dict(initializing=1,solving=1,verifying=1,infrastructure=.15)))
    assert cancelled == [True]


def test_harbor_hooks_record_stages_without_allocating_a_gpu(tmp_path, monkeypatch):
    import sys
    from enum import Enum
    from types import ModuleType, SimpleNamespace
    from pluginrsi.evaluation.benchmarks.terminal_bench import evaluate_harbor
    for name in ('harbor','harbor.models','harbor.models.trial','harbor.trial'):
        module=ModuleType(name);module.__path__=[];monkeypatch.setitem(sys.modules,name,module)
    configs=ModuleType('harbor.models.trial.config')
    for name in ('AgentConfig','EnvironmentConfig','TaskConfig','TrialConfig','VerifierConfig'):
        setattr(configs,name,SimpleNamespace)
    monkeypatch.setitem(sys.modules,configs.__name__,configs)
    hooks=ModuleType('harbor.trial.hooks')
    hooks.TrialEvent=Enum('TrialEvent','START ENVIRONMENT_START AGENT_START AGENT_END VERIFICATION_START END')
    monkeypatch.setitem(sys.modules,hooks.__name__,hooks)
    class Trial:
        @classmethod
        async def create(cls,config):
            assert config.environment.override_gpus==0
            assert config.agent.override_timeout_sec==600
            assert config.verifier.override_timeout_sec==180
            return cls()
        def __init__(self): self.hooks={}
        def add_hook(self,event,callback): self.hooks[event]=callback
        async def run(self):
            for event in hooks.TrialEvent:
                await self.hooks[event](None)
            return SimpleNamespace(model_dump=lambda **kwargs:dict(verifier_result=dict(rewards=dict(reward=1))))
    trials=ModuleType('harbor.trial.trial');trials.Trial=Trial;monkeypatch.setitem(sys.modules,trials.__name__,trials)
    progress.mark(tmp_path,'initializing')
    request=dict(work_dir=str(tmp_path),task=dict(task_path=str(tmp_path),id='fixture'),solver=dict(model='fixture'),
        harness=str(tmp_path),libraries=[],evaluation=dict(task_timeout_seconds=600,verifier_timeout_seconds=180,
        worker_initialization_timeout_seconds=600,infrastructure_timeout_seconds=300,benchmark_options={}))
    assert asyncio.run(evaluate_harbor(request))==dict(score=1.0,status='completed')
    assert read_json(tmp_path/'worker_stage.json')['stage']=='done'


def test_process_exit_during_kill_does_not_crash_controller(tmp_path, monkeypatch):
    from pluginrsi.search import proposer
    class Process:
        pid=123456789
        returncode=None
        calls=0
        async def wait(self):
            self.calls+=1
            if self.calls<3: raise TimeoutError()
            self.returncode=0
            return 0
    process=Process()
    async def spawn(*args,**kwargs):return process
    def gone(*args):raise ProcessLookupError()
    monkeypatch.setattr(proposer.asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(proposer.os,'killpg',gone)
    with pytest.raises(TimeoutError):
        asyncio.run(proposer.run_command([],tmp_path,tmp_path/'command.log',.1))
    assert process.returncode==0
