import fcntl
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pluginrsi.schemas import load_config
from test_full_search import config_at

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location('experiment_launcher', ROOT / 'scripts/experiment.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def experiment_file(tmp_path):
    config = config_at(tmp_path).model_dump(mode='json')
    config.pop('run_dir')
    config['solver'].pop('api_metrics_path')
    config['proposer']['agent'] = dict(model='fixture')
    config['experiment'] = dict(name='fixture', output_root=str(tmp_path / 'persistent'),
                                local_root=str(tmp_path / 'local'), expected_feedback_tasks=4,
                                expected_selection_tasks=4, expected_heldout_tasks=1)
    path = tmp_path / 'experiment.yaml'
    path.write_text(yaml.safe_dump(config))
    return path


def test_definition_is_strict_and_paths_do_not_depend_on_cwd(launcher, experiment_file, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings, config = launcher.definition(experiment_file)
    assert launcher.inspect_config(settings, config)['planned_total_rollouts'] == 18
    raw = yaml.safe_load(experiment_file.read_text())
    raw['experiment']['dashbord'] = {}
    experiment_file.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match='dashbord'):
        launcher.definition(experiment_file)


def test_worker_interpreter_keeps_venv_symlink(launcher, experiment_file, tmp_path):
    worker = tmp_path / 'venv/bin/python3'
    worker.parent.mkdir(parents=True)
    worker.symlink_to('/usr/bin/python3')
    raw = yaml.safe_load(experiment_file.read_text())
    raw['evaluation']['benchmark_options'] = dict(worker_python=str(worker))
    experiment_file.write_text(yaml.safe_dump(raw))
    _, config = launcher.definition(experiment_file)
    assert config.evaluation.benchmark_options['worker_python'] == str(worker)


@pytest.mark.parametrize('error', ['duplicate', 'overlap', 'budget', 'count', 'run_path'])
def test_invalid_definition_cannot_prepare(launcher, experiment_file, error):
    raw = yaml.safe_load(experiment_file.read_text())
    if error == 'duplicate':
        Path(raw['evaluation']['feedback_tasks']).write_text('{"id":"a"}\n' * 4)
    elif error == 'overlap':
        Path(raw['evaluation']['heldout_tasks']).write_text('{"id":"t0"}\n')
    elif error == 'budget':
        raw['search']['max_task_rollouts'] = 1
    elif error == 'count':
        raw['experiment']['expected_feedback_tasks'] = 10
    else:
        raw['run_dir'] = '/tmp/should-not-be-used'
    experiment_file.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        launcher.prepare(experiment_file, 'rejected')
    assert not Path(raw['experiment']['output_root']).exists()
    assert not Path(raw['experiment']['local_root']).exists()


def test_prepare_freezes_inputs_backups_and_isolates_replicates(launcher, experiment_file):
    first = launcher.prepare(experiment_file, 'first')
    second = launcher.prepare(experiment_file, 'second')
    config = load_config(first / 'config.yaml')
    assert config.run_dir != load_config(second / 'config.yaml').run_dir
    assert not config.run_dir.exists()
    assert (first / 'run').is_symlink()
    assert config.seed.is_relative_to((first / 'code_snapshot').resolve())
    assert (first / 'runtime_backup/code_snapshot/scripts/experiment.py').is_file()
    assert (first / 'runtime_backup/datasets/feedback.jsonl').is_file()
    source = yaml.safe_load(experiment_file.read_text())
    Path(source['evaluation']['feedback_tasks']).write_text('changed')
    experiment_file.write_text('changed')
    assert len(config.evaluation.feedback_tasks.read_text().splitlines()) == 4
    assert launcher.manifest_at(first)['settings']['name'] == 'fixture'
    assert json.loads((first / 'runtime_snapshot.json').read_text())['source'] == 'working_tree'
    local = Path(launcher.manifest_at(first)['local'])
    assert (local / 'config.yaml').read_bytes() == (first / 'config.yaml').read_bytes()
    assert not (local / 'config.yaml').is_symlink()
    assert (first / 'logs/run.log').is_symlink()
    assert json.loads((local / 'plan.json').read_text())['planned_total_rollouts'] == 18
    jobs = launcher.commands(first, launcher.manifest_at(first), resume=True)
    assert 'backup' not in jobs
    assert '--resume' in jobs['run']
    assert jobs['run'][jobs['run'].index('--max-infra-resumes') + 1] == '0'
    assert str(first / 'code_snapshot/scripts/run_full_experiment.py') in jobs['run']


def test_duplicate_run_and_missing_state_refuse_launch(launcher, experiment_file):
    output = launcher.prepare(experiment_file, 'one')
    with pytest.raises(ValueError, match='already exists'):
        launcher.prepare(experiment_file, 'one')
    with pytest.raises(ValueError, match='initialized state'):
        launcher.launch(output, resume=True)


def test_launch_controller_and_clean_up_failure(launcher, experiment_file, monkeypatch):
    output = launcher.prepare(experiment_file, 'one')
    calls = []
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: '/usr/bin/' + name)

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'has-session':
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, 'run', run)
    launcher.launch(output)
    windows = [c for c in calls if c[1] in ('new-session', 'new-window')]
    assert [w[w.index('-n') + 1] for w in windows] == ['run']
    assert all(str((output / 'code_snapshot/src').resolve()) in c[-1] for c in windows)
    calls.clear()

    def fail(command, **kwargs):
        calls.append(command)
        if command[1] == 'has-session':
            return SimpleNamespace(returncode=1)
        if command[1] == 'set-option':
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        launcher.launch(output)
    assert calls[-1][1] == 'kill-session'


def test_live_supervisor_refuses_start(launcher, experiment_file, monkeypatch):
    output = launcher.prepare(experiment_file, 'one')
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    with (output / '.supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            launcher.launch(output)


def test_historical_run_uses_original_launcher(launcher, tmp_path):
    with pytest.raises(ValueError, match='original launcher'):
        launcher.launch(tmp_path)


def test_frontier_check_through_bash_from_other_directory(tmp_path):
    env = dict(__import__('os').environ, PLUGINRSI_LAUNCH_LOG_DIR=str(tmp_path / 'logs'), PLUGINRSI_PYTHON=__import__('sys').executable)
    result = subprocess.run(['bash', str(ROOT / 'scripts/launch_experiment.sh'), 'check'],
                            cwd=tmp_path, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert 'Fill solver.model' in result.stdout + result.stderr
    assert list((tmp_path / 'logs').glob('*.log'))


def test_real_tmux_launches_frozen_entry_and_writes_service_log(launcher, experiment_file, tmp_path):
    import time
    if launcher.shutil.which('tmux') is None:
        pytest.skip('tmux is not installed')
    output = launcher.prepare(experiment_file, 'tmux-' + tmp_path.parent.name)
    runner = output / 'code_snapshot/scripts/run_full_experiment.py'
    runner.write_text('''import argparse
from pathlib import Path
import yaml
p = argparse.ArgumentParser()
p.add_argument('--config')
p.add_argument('--report-on-budget-stop', action='store_true')
p.add_argument('--max-infra-resumes', type=int)
a = p.parse_args()
c = yaml.safe_load(Path(a.config).read_text())
r = Path(c['run_dir'])
r.mkdir()
(r / 'state.json').write_text('{"stop_reason":"fixture_completed"}')
print('CPU fixture finished', flush=True)
''')
    manifest = launcher.manifest_at(output)
    session = 'pluginrsi-fixture-' + output.name
    try:
        launcher.launch(output)
        # The shared-filesystem interpreter can take tens of seconds to import dependencies.
        deadline = time.monotonic() + 60
        log = output / 'logs/run.log'
        while not log.is_file() or 'CPU fixture finished' not in log.read_text():
            assert time.monotonic() < deadline, {
                p.name: p.read_text() for p in (output / 'logs').glob('*.log')}
            time.sleep(.1)
        assert json.loads((output / 'run/state.json').read_text())['stop_reason'] == 'fixture_completed'
        assert Path(manifest['local']).is_dir()
    finally:
        subprocess.run(['tmux', 'kill-session', '-t', session], capture_output=True, check=True)
        assert subprocess.run(['tmux', 'has-session', '-t', session], capture_output=True).returncode != 0
