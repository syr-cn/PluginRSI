import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('supervisor_retry', ROOT/'scripts/run_full_experiment.py')
supervisor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(supervisor)


@pytest.mark.parametrize('reasons,expected,call_count', [
    (['proposal_infra_error', 'iterations_completed'], 0, 2),
    (['proposal_infra_error', 'proposal_infra_error'], 2, 2),
    (['all_local_proposals_invalid'], 2, 1),
])
def test_proposal_infra_resume_is_bounded_and_does_not_require_evaluations(tmp_path, monkeypatch, reasons, expected, call_count):
    run = tmp_path/'run'
    run.mkdir()
    config = SimpleNamespace(run_dir=run, evaluation=SimpleNamespace(heldout_tasks=None))
    monkeypatch.setattr(supervisor, 'load_config', lambda _: config)
    monkeypatch.setattr(supervisor.sys, 'argv', ['runner', '--config', str(tmp_path/'config.yaml'), '--resume', '--max-infra-resumes', '1'])
    commands, delays = [], []
    monkeypatch.setattr(supervisor.time, 'sleep', delays.append)

    def command(args):
        reason = reasons[len(commands)]
        commands.append(args)
        (run/'state.json').write_text(json.dumps(dict(stop_reason=reason, beam=['c000000'])))
        return SimpleNamespace(returncode=0 if reason == 'iterations_completed' else 2)

    monkeypatch.setattr(supervisor.subprocess, 'run', command)
    assert supervisor.main() == expected
    assert len(commands) == call_count
    assert all('--retry-infra' in cmd for cmd in commands)
    assert delays == ([60] if call_count == 2 else [])
    status = json.loads((tmp_path/'supervisor.json').read_text())
    assert status['stage'] == ('completed' if expected == 0 else 'stopped')
