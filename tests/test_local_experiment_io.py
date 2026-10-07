import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml


def setup(tmp_path):
    path = Path(__file__).resolve().parents[1] / 'scripts/local_experiment_io.py'
    spec = importlib.util.spec_from_file_location('local_experiment_io', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    runtime = tmp_path / 'local/runtime'
    config = dict(run_dir=str(runtime / 'run'),
                  solver=dict(api_metrics_path=str(runtime / 'api_solver.jsonl')),
                  proposer=dict(agent=dict(api_metrics_path=str(runtime / 'api_evolver.jsonl'))))
    (experiment / 'config.yaml').write_text(yaml.safe_dump(config))
    return module, experiment, runtime


def test_prepare_links_runtime_without_initializing_run(tmp_path):
    module, experiment, runtime = setup(tmp_path)
    assert module.prepare(experiment) == runtime
    assert (experiment / 'run').is_symlink()
    assert not (experiment / 'run').exists()
    (runtime / 'run').mkdir()
    (runtime / 'run/state.json').write_text('state')
    module.prepare(experiment)
    assert (experiment / 'run/state.json').read_text() == 'state'


@pytest.mark.parametrize('existing', ['directory', 'foreign_symlink'])
def test_prepare_preserves_existing_data(tmp_path, existing):
    module, experiment, runtime = setup(tmp_path)
    target = experiment / 'run'
    if existing == 'directory':
        target.mkdir()
    else:
        target.symlink_to(tmp_path / 'other')
    with pytest.raises(ValueError):
        module.prepare(experiment)
    assert not runtime.exists()


def test_backup_converges_and_leaves_live_data_untouched(tmp_path):
    module, experiment, runtime = setup(tmp_path)
    module.prepare(experiment)
    (runtime / 'run').mkdir()
    (runtime / 'run/state.json').write_text('first')
    (runtime / 'api_solver.jsonl').write_text('one\n')
    previous = {}
    assert module.backup_once(experiment, runtime, previous) == 0
    backup = experiment / 'runtime_backup'
    assert (backup / 'run/state.json').read_text() == 'first'
    (runtime / 'run/state.json').write_text('final')
    (runtime / 'api_solver.jsonl').write_text('one\ntwo\n')
    module.backup_once(experiment, runtime, previous)
    assert (backup / 'run/state.json').read_text() == 'final'
    assert (backup / 'api_solver.jsonl').read_text() == 'one\ntwo\n'
    assert (runtime / 'run/state.json').read_text() == 'final'
    (runtime / 'api_solver.jsonl').unlink()
    module.backup_once(experiment, runtime, previous)
    assert not (backup / 'api_solver.jsonl').exists()


def test_backup_process_flushes_after_termination_request(tmp_path):
    module, experiment, runtime = setup(tmp_path)
    module.prepare(experiment)
    (runtime / 'run').mkdir()
    state = runtime / 'run/state.json'
    state.write_text('first')
    with (tmp_path / 'backup.log').open('w') as log:
        process = subprocess.Popen([sys.executable, module.__file__, 'backup',
                                    '--experiment', str(experiment)], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while not (experiment / 'local_io_backup.json').exists():
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(.05)
            state.write_text('final')
            process.terminate()
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert (experiment / 'runtime_backup/run/state.json').read_text() == 'final'
    assert json.loads((experiment / 'local_io_backup.json').read_text())['status'] == 'copied'
