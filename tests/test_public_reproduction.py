import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from pluginrsi.evaluation import worker
from pluginrsi.runtime import EnvironmentService
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('hf_builder', ROOT / 'scripts/prepare_hf_data.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.mark.parametrize('command,expected', [('true', '1'), ('false', '0')])
@pytest.mark.parametrize('marker', ['', ": '>>>>> End Test Output'\n"])
def test_swe_reward_preserves_test_exit_before_marker_and_cleanup(tmp_path, command, expected, marker):
    script = builder.verifier_script(f'#!/bin/bash\n{command}\n{marker}git checkout fixture\n')
    script = script.replace('git checkout fixture', 'true').replace('/logs/verifier', str(tmp_path))
    subprocess.run(['bash', '-c', script], check=True)
    assert (tmp_path / 'reward.txt').read_text().strip() == expected


def test_swe_conversion_keeps_gold_patch_out_of_instructions(tmp_path):
    rows = [dict(instance_id='sample', problem_statement='Fix the failing test.',
                 patch='SECRET_REFERENCE_PATCH', image='public/image:tag',
                 eval_script='#!/bin/bash\nfalse\ngit checkout fixture\n')]
    builder.materialize_swe(rows, tmp_path, {'heldin': ['sample']})
    assert (tmp_path / 'tasks/sample/instruction.md').read_text() == 'Fix the failing test.'
    manifest = builder.read_jsonl(tmp_path / 'heldin.jsonl')
    assert manifest == [{'id': 'sample', 'task_path': str(tmp_path / 'tasks/sample')}]
    assert 'docker_image = "public/image:tag"' in (tmp_path / 'tasks/sample/task.toml').read_text()


def test_terminal_public_backend_routes_to_harbor(tmp_path, monkeypatch):
    request = {'evaluation': {'benchmark': 'terminal_bench_2_1',
                             'benchmark_options': {'backend': 'harbor'}},
               'work_dir': str(tmp_path)}
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(request))
    called = []

    async def evaluate(value):
        called.append(value)
        return {'score': 1, 'status': 'completed'}

    monkeypatch.setattr(worker, 'evaluate_harbor', evaluate)
    asyncio.run(worker.main(path))
    assert called == [request]
    assert json.loads((tmp_path / 'result.json').read_text())['score'] == 1


def test_harbor_command_prelude_applies_to_each_solver_command():
    calls = []

    class Environment:
        async def exec(self, command, **kwargs):
            calls.append(command)
            return SimpleNamespace(return_code=0, stdout='ok', stderr='')

    service = EnvironmentService(Environment(), lambda *a, **k: None, 'activate testbed')
    asyncio.run(service.execute('pwd'))
    asyncio.run(service.execute('pytest'))
    assert calls == ['activate testbed && (\npwd\n)', 'activate testbed && (\npytest\n)']
