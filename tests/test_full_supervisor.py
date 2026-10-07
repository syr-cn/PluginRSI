import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pluginrsi.search.store import read_json, write_json
from test_full_search import config_at

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('resumes', [0, 1])
def test_explicit_infra_resume_limit(tmp_path, monkeypatch, resumes):
    spec = importlib.util.spec_from_file_location('full_supervisor', ROOT / 'scripts/run_full_experiment.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = config_at(tmp_path)
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config.model_dump(mode='json')))
    monkeypatch.setattr(module.sys, 'argv', ['supervisor', '--config', str(path), '--max-infra-resumes', str(resumes),
                                          '--infra-resume-delay-seconds', '0'])
    calls = []

    def execute(command):
        calls.append(command)
        write_json(config.run_dir / 'state.json', {'stop_reason': 'invalid_seed_evaluation'})
        write_json(config.run_dir / 'evaluations/e000000/evaluation.json', {'status': 'infra_error'})
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(module.subprocess, 'run', execute)
    assert module.main() == 2
    assert len(calls) == resumes + 1
    assert read_json(tmp_path / 'supervisor.json')['stage'] == 'stopped'


@pytest.mark.parametrize("infra", [True, False])
def test_supervisor_retries_only_infra_then_reports(tmp_path, monkeypatch, infra):
    spec = importlib.util.spec_from_file_location("full_supervisor", ROOT / "scripts/run_full_experiment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = config_at(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    monkeypatch.setattr(module.sys, "argv", ["run_full_experiment.py", "--config", str(path),
                                          "--infra-resume-delay-seconds", "0"])
    calls = []

    def execute(command):
        calls.append(command)
        if len(calls) == 1:
            write_json(config.run_dir / "state.json", {"stop_reason": "invalid_seed_evaluation" if infra else "budget_exhausted"})
            write_json(config.run_dir / "evaluations/e000000/evaluation.json", {"status": "infra_error" if infra else "completed"})
            return SimpleNamespace(returncode=2)
        if "resume" in command:
            assert "--retry-infra" in command
            write_json(config.run_dir / "state.json", {"stop_reason": "iterations_completed"})
        else:
            assert "report-heldout" in command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", execute)
    assert module.main() == (0 if infra else 2)
    assert len(calls) == (3 if infra else 1)
    assert read_json(tmp_path / "supervisor.json")["stage"] == ("completed" if infra else "stopped")


@pytest.mark.parametrize('reason,code,beam,reports', [
    ('search budget exhausted; held-out allowance reserved', 0, ['c000000'], True),
    ('insufficient rollout budget for a complete evaluation', 0, ['c000000'], True),
    ('insufficient rollout budget for infrastructure retries', 0, ['c000000'], True),
    ('proposal_infra_error', 2, ['c000000'], False),
    ('search budget exhausted; held-out allowance reserved', 0, [], False),
])
def test_opt_in_budget_stop_reports_only_a_usable_incumbent(tmp_path, monkeypatch, reason, code, beam, reports):
    spec = importlib.util.spec_from_file_location('full_supervisor', ROOT/'scripts/run_full_experiment.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = config_at(tmp_path)
    path = tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump(config.model_dump(mode='json')))
    monkeypatch.setattr(module.sys, 'argv', ['run_full_experiment.py', '--config', str(path), '--report-on-budget-stop',
                                          '--max-infra-resumes', '0'])
    calls = []

    def execute(command):
        calls.append(command)
        if len(calls) == 1:
            write_json(config.run_dir/'state.json', {'stop_reason': reason, 'beam': beam})
            return SimpleNamespace(returncode=code)
        assert 'report-heldout' in command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, 'run', execute)
    assert module.main() == (0 if reports else 2)
    assert len(calls) == (2 if reports else 1)
    if reports:
        assert read_json(tmp_path/'supervisor.json')['search_stop_reason'] == reason


@pytest.mark.parametrize('resume', [False, True])
def test_no_heldout_finishes_without_report_and_resume_uses_run_config(tmp_path, monkeypatch, resume):
    spec = importlib.util.spec_from_file_location('full_supervisor', ROOT / 'scripts/run_full_experiment.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = config_at(tmp_path)
    config.evaluation.heldout_tasks = None
    path = tmp_path / 'config.yaml'
    if resume:
        config.run_dir.mkdir()
        (config.run_dir / 'config.yaml').write_text(yaml.safe_dump(config.model_dump(mode='json')))
        config.evaluation.heldout_tasks = tmp_path / 'stale-outer-heldout.jsonl'
    path.write_text(yaml.safe_dump(config.model_dump(mode='json')))
    monkeypatch.setattr(module.sys, 'argv', ['supervisor', '--config', str(path)] + (['--resume'] if resume else []))
    calls = []

    def execute(command):
        calls.append(command)
        write_json(config.run_dir / 'state.json', {'stop_reason': 'iterations_completed'})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, 'run', execute)
    assert module.main() == 0
    assert len(calls) == 1
    assert read_json(tmp_path / 'supervisor.json')['report'] is None
