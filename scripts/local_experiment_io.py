import argparse
import fcntl
import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import yaml

BACKUP_INTERVAL_SECONDS = 30
BACKUP_TIMEOUT_SECONDS = int(os.environ.get('PLUGINRSI_BACKUP_TIMEOUT_SECONDS', '120'))


def locations(experiment):
    config = yaml.safe_load((experiment / 'config.yaml').read_text())
    paths = {
        'run': Path(config['run_dir']),
        'api_solver.jsonl': Path(config['solver']['api_metrics_path']),
        'api_evolver.jsonl': Path(config['proposer']['agent']['api_metrics_path']),
    }
    root = paths['run'].parent
    if not root.is_absolute() or root == experiment or experiment in root.parents:
        raise ValueError('Runtime storage must be an absolute directory outside the experiment')
    if any(path != root / name for name, path in paths.items()):
        raise ValueError('Run and API metrics must share one runtime directory')
    return root, paths


def prepare(experiment):
    root, paths = locations(experiment)
    for name, target in paths.items():
        link = experiment / name
        if link.is_symlink():
            if link.resolve() != target.resolve():
                raise ValueError(f'Existing runtime link points elsewhere: {link}')
        elif link.exists():
            raise ValueError(f'Archive existing runtime data before preparing local I/O: {link}')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, target in paths.items():
        link = experiment / name
        if not link.is_symlink():
            link.symlink_to(target, target_is_directory=name == 'run')
    return root


def backup_once(experiment, root, previous=None):
    destination = experiment / 'runtime_backup'
    destination.mkdir(exist_ok=True)
    previous = {} if previous is None else previous
    current = {}
    for path in root.rglob('*'):
        if path == root / '.backup.lock':
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            current[str(path.relative_to(root))] = (info.st_mtime_ns, info.st_size, info.st_mode)
    changed = [name for name, signature in current.items() if previous.get(name) != signature]
    result = subprocess.run([
        'rsync', '-a', '--no-owner', '--no-group', '--ignore-times', '--delay-updates',
        '--from0', '--files-from=-',
        str(root) + '/', str(destination) + '/',
    ], input=''.join(name + '\0' for name in changed).encode(),
        timeout=BACKUP_TIMEOUT_SECONDS, check=False)
    if result.returncode not in (0, 24):
        raise RuntimeError(f'Runtime backup failed with rsync exit code {result.returncode}')
    if result.returncode == 0:
        for name in previous.keys() - current.keys():
            (destination / name).unlink(missing_ok=True)
        previous.clear()
        previous.update(current)
    return result.returncode


def watch(experiment):
    root = prepare(experiment)
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (root / '.backup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = {}
        while True:
            final_pass = stopping
            try:
                code = backup_once(experiment, root, previous)
                record = dict(timestamp=time.time(), status='copied', rsync_exit_code=code)
            except (subprocess.TimeoutExpired, RuntimeError) as error:
                record = dict(timestamp=time.time(), status='failed', error=str(error))
            temporary = experiment / '.local_io_backup.json.tmp'
            temporary.write_text(json.dumps(record) + '\n')
            temporary.replace(experiment / 'local_io_backup.json')
            print(json.dumps(record), flush=True)
            if final_pass:
                break
            deadline = time.monotonic() + BACKUP_INTERVAL_SECONDS
            while not stopping and time.monotonic() < deadline:
                time.sleep(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('prepare', 'once', 'backup'))
    parser.add_argument('--experiment', type=Path, required=True)
    args = parser.parse_args()
    experiment = args.experiment.resolve()
    if args.operation == 'prepare':
        print(prepare(experiment))
    elif args.operation == 'once':
        root = prepare(experiment)
        with (root / '.backup.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise SystemExit(backup_once(experiment, root))
    else:
        watch(experiment)
