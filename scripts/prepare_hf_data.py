"""Download public benchmark sources and materialize the published experiment splits."""

import argparse
import io
import json
from pathlib import Path
import shutil
import tarfile

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / 'data_raw'
SWE_REPO = 'SWE-bench/SWE-bench_Verified'
SWE_REVISION = '78f471bf655a3137b2e8a75af1501690ec009ec3'
TERMINAL_REPO = 'harborframework/terminal-bench-2.1'
TERMINAL_REVISION = '3e235dff6880252a587fa479c09bcb1e16edf2eb'
SWE_RESOURCES = {'cpus': 1, 'memory_mb': 1024, 'storage_mb': 10240}
QA_REPOS = {
    'Hothan/OlympiadBench': ['OlympiadBench/OE_TO_maths_en_COMP/*', 'OlympiadBench/OE_TO_physics_en_COMP/*'],
    'm-a-p/SuperGPQA': ['SuperGPQA-all.jsonl'],
    'AfterQuery/FinanceQA': ['FinanceQA/test.csv'],
    'openai/frontierscience': ['olympiad/test.jsonl'],
}
SCICITE_REPO = 'allenai/scicite'
SCICITE_REVISION = 'refs/convert/parquet'
SCICITE_LABELS = ['method', 'background', 'result']
QA_REVISIONS = {
    'Hothan/OlympiadBench': '91184b52131e7fc9455fef848035173aea8cc01a',
    'm-a-p/SuperGPQA': '4430d4458112c7d4497fdcf94d7cc223313d6acf',
    'AfterQuery/FinanceQA': '6eb03b46c2e7f71ad52f7db4d12a9eabd523f575',
    'openai/frontierscience': '25ed67db7da8f4591484e764008ff585544f5a30',
    'allenai/scicite': 'f6a02e9afbd45c39b5ee6caf38e110466e36b650',
}


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def fixed_splits(directory, sizes):
    result = {name: [row['id'] for row in read_jsonl(directory / f'{name}.jsonl')] for name in sizes}
    all_ids = [iid for ids in result.values() for iid in ids]
    if len(set(all_ids)) != len(all_ids) or any(len(result[k]) != v for k, v in sizes.items()):
        raise ValueError('Published split IDs must have the expected sizes and be disjoint')
    if any(Path(iid).name != iid or iid in ('.', '..') for iid in all_ids):
        raise ValueError('Unsafe task ID')
    return result


def task_manifests(directory, splits, tasks):
    for name, ids in splits.items():
        rows = [{'id': iid, 'task_path': str((tasks / iid).resolve())} for iid in ids]
        (directory / f'{name}.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))


def verifier_script(script):
    """Preserve the experiment's test-process exit-code reward, before cleanup/markers."""
    lines = script.rstrip().splitlines()
    if not lines or not lines[-1].startswith('git checkout '):
        raise ValueError('Unsupported SWE eval script: expected final git checkout cleanup')
    index = len(lines) - 1
    if index and '>>>>> End Test Output' in lines[index - 1]:
        index -= 1
    lines.insert(index, 'swe_test_status=$?')
    lines.extend(['mkdir -p /logs/verifier',
                  'if [ "$swe_test_status" -eq 0 ]; then',
                  "    printf '1\\n' > /logs/verifier/reward.txt", 'else',
                  "    printf '0\\n' > /logs/verifier/reward.txt", 'fi', 'exit 0'])
    return '\n'.join(lines) + '\n'


def materialize_swe(rows, output, splits):
    records = {row['instance_id']: row for row in rows}
    ids = [iid for group in splits.values() for iid in group]
    if len(records) != len(rows) or set(ids) - records.keys():
        raise ValueError('SWE source has duplicate IDs or is missing published split IDs')
    # Validate every verifier before writing a partially usable dataset.
    scripts = {iid: verifier_script(records[iid]['eval_script']) for iid in ids}
    for iid in ids:
        row = records[iid]
        task = output / 'tasks' / iid
        (task / 'environment').mkdir(parents=True, exist_ok=True)
        (task / 'tests').mkdir(exist_ok=True)
        (task / 'instruction.md').write_text(row['problem_statement'])
        settings = ['version = "1.0"', '', '[agent]', 'timeout_sec = 3600', '',
                    '[verifier]', 'timeout_sec = 1800', '', '[environment]',
                    f'docker_image = {json.dumps(row["image"])}']
        settings += [f'{key} = {value}' for key, value in SWE_RESOURCES.items()]
        settings += ['gpus = 0', 'allow_internet = true']
        (task / 'task.toml').write_text('\n'.join(settings) + '\n')
        verifier = task / 'tests/test.sh'
        verifier.write_text(scripts[iid])
        verifier.chmod(0o755)
    task_manifests(output, splits, output / 'tasks')


def prepare_swe(args):
    from datasets import load_dataset
    output = ROOT / 'datasets/swe'
    splits = fixed_splits(output, {'heldin400': 400, 'heldout': 100})
    dataset = load_dataset(args.repo or SWE_REPO, split='test', revision=args.revision or SWE_REVISION,
                           cache_dir=str(args.cache / 'datasets'))
    materialize_swe(list(dataset), output, splits)
    write_json(output / 'provenance.json', {'repo': args.repo or SWE_REPO,
               'revision': args.revision or SWE_REVISION, 'counts': {k: len(v) for k, v in splits.items()},
               'grading': 'test process exit code before cleanup; matches original experiment adapter'})


def prepare_terminal(args):
    from huggingface_hub import snapshot_download
    output = ROOT / 'datasets/terminal_bench_2_1'
    splits = fixed_splits(output, {'heldin59': 59, 'heldout30': 30})
    source = Path(snapshot_download(args.repo or TERMINAL_REPO, repo_type='dataset',
                  revision=args.revision or TERMINAL_REVISION, cache_dir=str(args.cache / 'hub')))
    ids = [iid for group in splits.values() for iid in group]
    for iid in ids:
        for name in ('task.toml', 'instruction.md', 'tests/test.sh'):
            if not (source / 'tasks' / iid / name).is_file():
                raise ValueError(f'HF snapshot is missing task asset: {iid}/{name}')
    for iid in ids:
        destination = output / 'tasks' / iid
        if destination.exists():
            raise ValueError(f'Task directory already exists: {destination}; use a clean data directory')
    for iid in ids:
        shutil.copytree(source / 'tasks' / iid, output / 'tasks' / iid)
    task_manifests(output, splits, output / 'tasks')
    write_json(output / 'provenance.json', {'repo': args.repo or TERMINAL_REPO,
               'revision': args.revision or TERMINAL_REVISION, 'counts': {k: len(v) for k, v in splits.items()}})


def prepare_qa(args):
    from huggingface_hub import HfApi, snapshot_download
    import pyarrow.parquet as pq
    from prepare_qa_transfer import (load_sources, select_cohort, validate_records, near_duplicates,
                                     manifests, jsonl, serialize, write_artifacts, validate_directory,
                                     DEFAULT_SEED)
    api = HfApi()
    source_root = args.cache / 'qa'
    revisions = dict(QA_REVISIONS)
    if args.revisions:
        revisions.update(json.loads(args.revisions.read_text()))
    sources = {}
    for repo, patterns in QA_REPOS.items():
        revision = revisions.get(repo) or api.dataset_info(repo).sha
        snapshot_download(repo, repo_type='dataset', revision=revision,
                          allow_patterns=patterns, local_dir=source_root / repo)
        sources[repo] = revision
    revision = revisions.get(SCICITE_REPO) or api.dataset_info(SCICITE_REPO, revision=SCICITE_REVISION).sha
    source = Path(snapshot_download(SCICITE_REPO, repo_type='dataset', revision=revision,
                  allow_patterns=['default/train/*.parquet'], cache_dir=str(args.cache / 'hub')))
    shards = sorted((source / 'default/train').glob('*.parquet'))
    if not shards:
        raise ValueError('SciCite snapshot contains no training parquet shards')
    rows = [row for shard in shards for row in pq.read_table(shard).to_pylist()]
    for row in rows:
        if isinstance(row['label'], int):
            row['label'] = SCICITE_LABELS[row['label']]
        row['unique_id'] = f"{row['id']}_{row['excerpt_index']}"
    # Preserve the original builder's source schema without executing an HF loading script.
    archive = source_root / SCICITE_REPO / 'scicite.tar.gz'
    archive.parent.mkdir(parents=True, exist_ok=True)
    content = jsonl(rows).encode()
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo('scicite/train.jsonl')
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    sources[SCICITE_REPO] = revision
    output = ROOT / 'datasets/qa_transfer_v1'
    schema = json.loads((output / 'schema.json').read_text())
    pools = load_sources(source_root)
    selected = select_cohort(pools, DEFAULT_SEED)
    validate_records(selected, schema)
    pairs = near_duplicates(selected)
    if any(pair['cross_split'] for pair in pairs):
        raise ValueError('Cross-split near-duplicates found')
    expected = fixed_splits(output, {'heldin': 150, 'heldout': 250})
    actual = manifests(selected)
    if any(expected[k] != [row['id'] for row in actual[k]] for k in expected):
        raise ValueError('HF data differs from the published cohort; inspect/pin source revisions before proceeding')
    artifacts = {'data.jsonl': jsonl(selected), 'schema.json': serialize(schema),
                 'provenance.json': serialize({'counts': {'heldin': 150, 'heldout': 250},
                     'seed': DEFAULT_SEED, 'sources': sources, 'near_duplicate_audit': pairs})}
    artifacts.update({f'{key}.jsonl': jsonl(value) for key, value in actual.items()})
    write_artifacts(output, artifacts)
    validate_directory(output, schema)
    write_json(output / 'hf_sources.json', sources)


def main():
    load_dotenv(ROOT / '.env', override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('benchmark', choices=['swe', 'terminal_bench', 'qa_transfer'])
    parser.add_argument('--cache', type=Path, default=DEFAULT_CACHE)
    parser.add_argument('--repo', help='Override SWE/Terminal HF dataset repository')
    parser.add_argument('--revision', help='Override SWE/Terminal immutable source revision')
    parser.add_argument('--revisions', type=Path, help='QA repo-to-commit JSON mapping')
    args = parser.parse_args()
    if args.benchmark == 'qa_transfer' and (args.repo or args.revision):
        parser.error('QA uses --revisions because it combines multiple datasets')
    if args.benchmark != 'qa_transfer' and args.revisions:
        parser.error('--revisions is only valid for QA')
    {'swe': prepare_swe, 'terminal_bench': prepare_terminal, 'qa_transfer': prepare_qa}[args.benchmark](args)
    print(json.dumps({'benchmark': args.benchmark, 'status': 'prepared'}))


if __name__ == '__main__':
    main()
