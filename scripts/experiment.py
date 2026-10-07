"""Prepare isolated runs from a declarative experiment and launch their frozen tools."""

import argparse
import copy
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pluginrsi.evaluation.runner import heldout_candidate_limit, read_tasks
from pluginrsi.loader import validate
from pluginrsi.schemas import RunConfig, load_config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / 'configs/swe.yaml'
SNAPSHOT_PATHS = ('src', 'scripts', 'seeds')
NAME_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9_-]*$'


class Experiment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=NAME_PATTERN)
    output_root: Path = Path('runs')
    local_root: Path
    env_files: list[Path] = Field(default_factory=list)
    expected_feedback_tasks: int = Field(gt=0)
    expected_selection_tasks: int = Field(gt=0)
    expected_heldout_tasks: int = Field(default=0, ge=0)
    report_on_budget_stop: bool = True
    max_infra_resumes: int = Field(default=0, ge=0)


def absolute(value, root=ROOT):
    return (root / Path(value)).resolve()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def definition(path, root=ROOT):
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError('Experiment YAML must contain a mapping')
    raw = copy.deepcopy(raw)
    experiment = Experiment.model_validate(raw.pop('experiment'))
    for name in ('output_root', 'local_root'):
        setattr(experiment, name, absolute(getattr(experiment, name), root))
    experiment.env_files = [absolute(p, root) for p in experiment.env_files]
    if 'run_dir' in raw or any('api_metrics_path' in options for options in
                             (raw.get('solver', {}), raw.get('proposer', {}).get('agent') or {})):
        raise ValueError('run_dir and api_metrics_path are generated per run; omit them from the definition')
    raw['run_dir'] = str(experiment.local_root / experiment.name / 'unprepared' / 'run')
    for name in ('seed', 'plugin_library'):
        raw[name] = str(absolute(raw[name], root))
    for name in ('feedback_tasks', 'selection_tasks', 'heldout_tasks'):
        if raw['evaluation'].get(name) is not None:
            raw['evaluation'][name] = str(absolute(raw['evaluation'][name], root))
    options = raw['evaluation'].get('benchmark_options', {})
    if raw['evaluation']['benchmark'] == 'qa_transfer':
        from pluginrsi.evaluation.qa_data import resolve_qa_paths
        resolve_qa_paths(options, root)
    if options.get('worker_python'):
        # Resolving a venv interpreter symlink would select the system environment.
        options['worker_python'] = os.path.abspath(root / options['worker_python'])
    if 'module_paths' in options:
        options['module_paths'] = [str(absolute(p, root)) for p in options['module_paths']]
    for model in (raw['solver'], raw['proposer'].get('agent')):
        if model and model.get('rpm_state_path'):
            model['rpm_state_path'] = str(absolute(model['rpm_state_path'], root))
    config = RunConfig.model_validate(raw)
    if config.proposer.agent is None:
        raise ValueError('This launcher requires proposer.agent for API monitoring')
    for name in ('seed', 'plugin_library'):
        value = getattr(config, name)
        if not value.is_relative_to(root / 'seeds'):
            raise ValueError(f'{name} must be inside the project seeds directory to freeze it')
    return experiment, config


def inspect_config(experiment, config):
    for name, model in [('solver', config.solver), ('proposer.agent', config.proposer.agent)]:
        if model is not None and not model.model.strip():
            raise ValueError(f'Fill {name}.model in the experiment YAML before running')
    options = config.evaluation.benchmark_options
    if config.evaluation.benchmark in ('swe_harbor', 'terminal_bench_2_1'):
        environment = options.get('environment', {})
        if not (environment.get('type') or environment.get('import_path')):
            raise ValueError('Fill evaluation.benchmark_options.environment (for example type: docker)')
    splits = {}
    for name in ('feedback', 'selection', 'heldout'):
        path = getattr(config.evaluation, f'{name}_tasks')
        rows = read_tasks(path) if path is not None else []
        expected = getattr(experiment, f'expected_{name}_tasks')
        if len(rows) != expected:
            raise ValueError(f'{name}: expected {expected} tasks, found {len(rows)}')
        ids = [row['id'] for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError(f'{name}: duplicate task IDs')
        if config.evaluation.benchmark in ('swe_harbor', 'terminal_bench_2_1'):
            for row in rows:
                if not row.get('task_path') or not (Path(row['task_path']) / 'task.toml').is_file():
                    raise ValueError(f"Missing task directory: {row['id']}")
        splits[name] = rows
    if splits['feedback'] != splits['selection']:
        raise ValueError('feedback and selection must be the identical ordered held-in tasks')
    if {t['id'] for t in splits['feedback']} & {t['id'] for t in splits['heldout']}:
        raise ValueError('Held-in and held-out tasks overlap')
    if config.evaluation.benchmark == 'qa_transfer':
        from pluginrsi.evaluation.qa_data import inspect_qa_inputs
        inspect_qa_inputs(config.evaluation.benchmark_options, splits)
    search = config.search
    if search.feedback_batch_size > len(splits['feedback']):
        raise ValueError('feedback_batch_size exceeds the held-in dataset')
    local = search.offspring_per_phase * search.feedback_batch_size
    planned = len(splits['selection']) + search.iterations * (
        local + search.recomposition_offspring * len(splits['selection'])) + heldout_candidate_limit(config) * len(splits['heldout'])
    if search.max_task_rollouts is not None and planned > search.max_task_rollouts:
        raise ValueError(f'Planned rollout bound {planned} exceeds cap {search.max_task_rollouts}')
    validate(config.seed, [config.plugin_library])
    worker = config.evaluation.benchmark_options.get('worker_python')
    if worker and not Path(worker).is_file():
        raise ValueError(f'Missing worker interpreter: {worker}')
    return dict(planned_total_rollouts=planned, max_task_rollouts=search.max_task_rollouts,
                heldin_tasks=len(splits['feedback']), heldout_tasks=len(splits['heldout']),
                iterations=search.iterations, concurrency=config.evaluation.concurrency,
                proposer_concurrency=config.proposer.concurrency,
                solver_rpm_cap=config.solver.rpm_limit,
                evolver_rpm_cap=config.proposer.agent.rpm_limit,
                adaptive_evolver_admission=config.proposer.agent.adaptive_rate_limit,
                model_or_sandbox_requests=0)


def prepare(path, run_id=None, root=ROOT):
    experiment, config = definition(path, root)
    plan = inspect_config(experiment, config)
    run_id = run_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    if not re.fullmatch(NAME_PATTERN, run_id):
        raise ValueError('run-id must contain only letters, digits, underscores and hyphens')
    output = experiment.output_root / experiment.name / run_id
    local = experiment.local_root / experiment.name / run_id
    if local == output or local.is_relative_to(output) or output.is_relative_to(local):
        raise ValueError('Local and persistent run directories must be separate')
    if output.exists() or local.exists():
        raise ValueError('Run already exists; use resume with its persistent directory')
    local.mkdir(parents=True)
    output.mkdir(parents=True)
    snapshot = local / 'code_snapshot'
    for name in SNAPSHOT_PATHS:
        shutil.copytree(root / name, snapshot / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    commit, changes = None, ''
    if (root / '.git').exists():
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, text=True,
                                  capture_output=True)
        commit = revision.stdout.strip() if revision.returncode == 0 else None
        changes = subprocess.check_output(['git', 'status', '--porcelain', '--', *SNAPSHOT_PATHS],
                                          cwd=root, text=True)
    write_json(output / 'runtime_snapshot.json', dict(source='working_tree', commit=commit,
               changes=changes.splitlines(), paths=SNAPSHOT_PATHS, created_at=datetime.now(timezone.utc).isoformat()))
    for name in ('seed', 'plugin_library'):
        setattr(config, name, snapshot / getattr(config, name).relative_to(root))
    datasets = local / 'datasets'
    datasets.mkdir()
    for name in ('feedback', 'selection', 'heldout'):
        source = getattr(config.evaluation, f'{name}_tasks')
        if source is not None:
            target = datasets / f'{name}.jsonl'
            shutil.copy2(source, target)
            setattr(config.evaluation, f'{name}_tasks', target)
    config.run_dir = local / 'run'
    if config.evaluation.benchmark == 'qa_transfer':
        from pluginrsi.evaluation.qa_data import freeze_qa_inputs
        freeze_qa_inputs(config.evaluation.benchmark_options, datasets / 'qa')
    config.solver.api_metrics_path = local / 'api_solver.jsonl'
    config.proposer.agent.api_metrics_path = local / 'api_evolver.jsonl'
    # Validate the frozen inputs before publishing a launch manifest.
    inspect_config(experiment, config)
    shutil.copy2(path, output / 'source-config.yaml')
    config_text = yaml.safe_dump(config.model_dump(mode='json'), sort_keys=False)
    (output / 'config.yaml').write_text(config_text)
    (local / 'config.yaml').write_text(config_text)
    write_json(output / 'plan.json', plan)
    write_json(local / 'plan.json', plan)
    for name in ('run', 'code_snapshot', 'datasets', 'api_solver.jsonl', 'api_evolver.jsonl'):
        (output / name).symlink_to(local / name, target_is_directory=name in ('run', 'code_snapshot', 'datasets'))
    (output / 'logs').mkdir()
    (local / 'logs').mkdir()
    (output / 'logs' / 'run.log').symlink_to(local / 'logs' / 'run.log')
    manifest = dict(schema_version=1, project_root=str(root), experiment=str(output), local=str(local),
                    python=sys.executable, settings=experiment.model_dump(mode='json'))
    # The initial backup includes code and datasets, even before the first search.
    subprocess.run([sys.executable, str(snapshot / 'scripts/local_experiment_io.py'),
                    'once', '--experiment', str(output)], check=True)
    write_json(output / 'launch.json', manifest)
    print(json.dumps(dict(experiment=str(output), local=str(local), **plan)))
    return output


def manifest_at(output):
    path = output / 'launch.json'
    if not path.is_file():
        raise ValueError('No launch.json: use the original launcher for a historical experiment')
    manifest = json.loads(path.read_text())
    if manifest.get('schema_version') != 1 or Path(manifest['experiment']) != output:
        raise ValueError('Launch manifest does not match this run directory')
    return manifest


def commands(output, manifest, resume):
    settings = Experiment.model_validate(manifest['settings'])
    scripts = output / 'code_snapshot/scripts'
    python = manifest['python']
    runner = [python, '-u', str(scripts / 'run_full_experiment.py'), '--config', str(output / 'config.yaml')]
    runner += ['--max-infra-resumes', str(settings.max_infra_resumes)]
    for path in settings.env_files:
        runner += ['--env-file', str(path)]
    if settings.report_on_budget_stop:
        runner.append('--report-on-budget-stop')
    if resume:
        runner.append('--resume')
    jobs = {'run': runner}
    return jobs


def launch(output, resume=False):
    manifest = manifest_at(output)
    settings = Experiment.model_validate(manifest['settings'])
    local = Path(manifest['local'])
    snapshot = local / 'code_snapshot'
    if not snapshot.is_dir():
        raise ValueError('Local runtime is missing; restore runtime_backup to the recorded local path first')
    state = local / 'run/state.json'
    if resume != state.is_file():
        raise ValueError('resume requires initialized state; start requires a fresh prepared run')
    if not resume and (local / 'run').exists():
        raise ValueError('Uninitialized run directory exists; inspect it before starting')
    if resume:
        (local / 'config.yaml').write_text((local / 'run/config.yaml').read_text())
    for path in settings.env_files:
        if not path.is_file():
            raise ValueError(f'Missing environment file: {path}')
    if not shutil.which('tmux'):
        raise ValueError('tmux is required to launch an experiment')
    session = 'pluginrsi-' + settings.name + '-' + output.name
    with (output / '.launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if subprocess.run(['tmux', 'has-session', '-t', session], capture_output=True).returncode == 0:
            raise ValueError(f'tmux session exists: {session}; close its stopped windows before resuming')
        # Refuse a live controller even if its original tmux session is gone.
        with (output / '.supervisor.lock').open('a') as supervisor_lock:
            fcntl.flock(supervisor_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if state.is_file():
            with (local / 'run/.controller.lock').open('a') as controller_lock:
                fcntl.flock(controller_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        jobs = commands(output, manifest, resume)
        # Start services first; the controller is the last window so setup failure starts no search.
        ordered = [name for name in jobs if name != 'run'] + ['run']
        created = False
        try:
            for name in ordered:
                invocation = [manifest['python'], '-u', str(snapshot / 'scripts/experiment.py'),
                              '_pane', '--run', str(output), '--service', name]
                if resume:
                    invocation.append('--resume')
                shell_command = shlex.join(['env', f'PYTHONPATH={snapshot / "src"}', *invocation])
                shell_command = 'exec ' + shell_command + ' >> ' + shlex.quote(str(output / 'logs' / f'{name}.log')) + ' 2>&1'
                command = (['tmux', 'new-session', '-d', '-s', session, '-n', name] if not created
                           else ['tmux', 'new-window', '-d', '-t', session, '-n', name])
                subprocess.run(command + [shell_command], check=True)
                if not created:
                    created = True
                    subprocess.run(['tmux', 'set-option', '-t', session, 'remain-on-exit', 'on'], check=True)
        except BaseException:
            if created:
                subprocess.run(['tmux', 'kill-session', '-t', session], check=False)
            raise
    print(json.dumps(dict(session=session, experiment=str(output), logs=str(output / 'logs'))))


def pane(output, service, resume):
    manifest = manifest_at(output)
    command = commands(output, manifest, resume)[service]
    env = dict(os.environ, PYTHONPATH=str(Path(manifest['local']) / 'code_snapshot/src'))
    env['PATH'] = str(Path(manifest['python']).parent) + os.pathsep + env.get('PATH', '')
    config = load_config(output / 'config.yaml')
    for model in (config.solver, config.proposer.agent):
        for key in (model.api_key_env, model.base_url_env):
            if key:
                env.pop(key, None)
    with (output / 'logs' / f'{service}.log').open('ab', buffering=0) as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        os.chdir(manifest['project_root'])
        os.execve(command[0], command, env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    for name in ('check', 'prepare', 'start'):
        command = sub.add_parser(name)
        command.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
        if name != 'check':
            command.add_argument('--run-id')
        if name == 'start':
            command.add_argument('--run', type=Path, help='Start an already prepared persistent run directory')
    resume = sub.add_parser('resume')
    resume.add_argument('--run', type=Path, required=True)
    child = sub.add_parser('_pane', help=argparse.SUPPRESS)
    child.add_argument('--run', type=Path, required=True)
    child.add_argument('--service', choices=('run',), required=True)
    child.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if args.operation == '_pane':
        pane(args.run.resolve(), args.service, args.resume)
    elif args.operation == 'resume':
        launch(args.run.resolve(), resume=True)
    elif args.operation == 'check':
        experiment, config = definition(args.config.resolve())
        print(json.dumps(dict(name=experiment.name, **inspect_config(experiment, config)), indent=2))
    else:
        output = args.run.resolve() if args.operation == 'start' and args.run else prepare(args.config.resolve(), args.run_id)
        if args.operation == 'start':
            launch(output)


if __name__ == '__main__':
    main()
