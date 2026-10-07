"""Resolve, validate and freeze evaluator-owned QA data."""

import json
from pathlib import Path
import shutil

from ..schemas import QABenchmarkOptions, SolverConfig, read_yaml
from .qa_grading import METHODS

DATASET_COMPANIONS = ('schema.json', 'provenance.json')


def resolve_qa_paths(options, root):
    for key in ('data_file', 'judge_config'):
        options[key] = str((Path(root) / options[key]).resolve())


def read_qa_data(path):
    rows = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        iid = row['id']
        if not isinstance(iid, str) or not iid or Path(iid).name != iid or iid in ('.', '..') or iid in rows:
            raise ValueError('QA data requires unique filename-safe IDs')
        if row.get('schema_version') != 1 or row.get('split') not in ('heldin', 'heldout'):
            raise ValueError(f'Invalid QA record version/split: {iid}')
        mode = row['answer_mode']
        method = row['grading']['method']
        expected = {'choice': {'exact_choice'}, 'label': {'exact_label'},
                    'free_form': {'olympiadbench', 'reference_judge'}}
        if method not in METHODS or method not in expected.get(mode, set()):
            raise ValueError(f'Incompatible QA answer mode/grading method: {iid}')
        public, target = row['input'], row['target']
        if (set(public) != {'question', 'context', 'choices'}
                or not isinstance(public['question'], str) or not public['question'].strip()
                or not isinstance(public['context'], str)
                or not isinstance(public['choices'], list)
                or not isinstance(target['answers'], list) or not target['answers']
                or any(not isinstance(a, str) or not a.strip() for a in target['answers'])):
            raise ValueError(f'Invalid QA input/target: {iid}')
        choices = public['choices']
        if any(set(c) != {'key', 'text'} or not all(isinstance(c[k], str) and c[k] for k in ('key', 'text')) for c in choices):
            raise ValueError(f'Invalid QA choices: {iid}')
        mapping = {c['key']: c['text'] for c in choices}
        if mode != 'free_form':
            if (len(mapping) != len(choices) or target['answer_key'] not in mapping
                    or target['answers'] != [mapping[target['answer_key']]]):
                raise ValueError(f'Invalid QA reference key: {iid}')
        elif choices or target['answer_key'] is not None:
            raise ValueError(f'Unexpected QA choices/key: {iid}')
        rows[iid] = row
    if not rows:
        raise ValueError('QA dataset must not be empty')
    return rows


def inspect_qa_inputs(options, splits):
    QABenchmarkOptions.model_validate(options)
    rows = read_qa_data(options['data_file'])
    judge = SolverConfig.model_validate(read_yaml(Path(options['judge_config'])))
    if judge.rpm_state_path is not None and not judge.rpm_state_path.is_absolute():
        raise ValueError('Judge RPM state path must be absolute')
    for split, tasks in splits.items():
        expected = 'heldout' if split == 'heldout' else 'heldin'
        for task in tasks:
            if set(task) != {'id'}:
                raise ValueError('QA manifests contain IDs only; gold data stays with evaluator')
            if task['id'] not in rows or rows[task['id']]['split'] != expected:
                raise ValueError(f'QA manifest ID absent or in wrong split: {task["id"]}')
    return rows


def freeze_qa_inputs(options, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    source = Path(options['data_file'])
    files = {'data.jsonl': source, 'judge.yaml': Path(options['judge_config'])}
    files.update({name: source.parent / name for name in DATASET_COMPANIONS if (source.parent / name).is_file()})
    for name, path in files.items():
        target = destination / name
        if target.exists() and target.read_bytes() != path.read_bytes():
            raise ValueError(f'Frozen QA input differs: {target}')
    for name, path in files.items():
        target = destination / name
        if path.resolve() != target.resolve():
            shutil.copyfile(path, target)
    options.update(data_file=str((destination / 'data.jsonl').resolve()),
                   judge_config=str((destination / 'judge.yaml').resolve()))
