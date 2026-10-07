"""Build the fixed QA/classification transfer cohort from local source datasets."""

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import random
import re
import tarfile
import unicodedata

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / 'huggingface.co/datasets'
DEFAULT_OUTPUT = ROOT / 'datasets/qa_transfer_v1'
SCHEMA_PATH = ROOT / 'datasets/qa_transfer_v1/schema.json'
DEFAULT_SEED = 42
SCICITE_URL = 'https://s3-us-west-2.amazonaws.com/ai2-s2-research/scicite/scicite.tar.gz'
LABELS = ['method', 'background', 'result']
HELDIN_TASKS = ['olympiad_math', 'supergpqa_economics', 'scicite']
HELDOUT_TASKS = ['olympiad_physics', 'frontierscience_olympiad', 'financeqa',
                 'supergpqa_medicine', 'supergpqa_law']
SPLIT_COUNTS = {'heldin': 150, 'heldout': 250}
TASK_SIZE = 50
SCICITE_QUOTAS = dict(zip(LABELS, [17, 17, 16]))
FINANCE_QUOTAS = {'basic': 13, 'assumption': 15, 'conceptual': 22}
FRONTIER_QUOTAS = {'physics': 20, 'chemistry': 20, 'biology': 10}
FINANCE_CONCEPT_GROUPS = {87: 'investment_leverage', 88: 'investment_leverage',
                         99: 'enterprise_equity_acquisition', 100: 'enterprise_equity_acquisition'}
NEAR_DUPLICATE_THRESHOLD = 0.8
MIN_AUDIT_TOKENS = 12


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def normalized(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def record(task, source, source_id, source_split, source_file, question, answers,
           *, context='', subject='', subfield='', mode='free_form', choices=None,
           answer_key=None, grader='reference_judge', group_id=None, attributes=None,
           parameters=None):
    return {
        'schema_version': 1, 'id': f'{task}__{re.sub(r"[^A-Za-z0-9_.-]", "_", str(source_id))}', 'task': task,
        'task_type': 'cls' if mode == 'label' else 'qa', 'answer_mode': mode,
        'subject': subject, 'language': 'en', 'split': '',
        'input': {'question': question, 'context': context or '', 'choices': choices or []},
        'target': {'answers': answers, 'answer_key': answer_key},
        'grading': {'method': grader, 'parameters': parameters or {}},
        'source': {'dataset': source, 'config': 'unspecified', 'split': source_split,
                   'id': str(source_id), 'file': source_file,
                   'group_id': group_id or f'{source}:{task}:{source_id}'},
        'attributes': {'subfield': subfield, **(attributes or {})},
    }


def load_sources(root):
    import pyarrow.parquet as pq

    pools = {}
    for task, config, subject in [('olympiad_math', 'OE_TO_maths_en_COMP', 'mathematics'),
                                  ('olympiad_physics', 'OE_TO_physics_en_COMP', 'physics')]:
        relative = f'Hothan/OlympiadBench/OlympiadBench/{config}/{config}.parquet'
        pools[task] = []
        for row in pq.read_table(root / relative).to_pylist():
            if row['modality'] != 'Text-only' or row['language'] != 'English':
                raise ValueError(f'Unexpected modality/language: {config}/{row["id"]}')
            item = record(task, 'Hothan/OlympiadBench', row['id'], 'unspecified', relative,
                row['question'], row['final_answer'], context=row['context'], subject=subject,
                subfield=row['subfield'], grader='olympiadbench',
                parameters={key: row[key] for key in ('answer_type', 'is_multiple_answer', 'unit', 'error')},
                attributes={'difficulty': row['difficulty']})
            item['source']['config'] = config
            pools[task].append(item)

    relative = 'm-a-p/SuperGPQA/SuperGPQA-all.jsonl'
    domains = {'Economics': 'supergpqa_economics', 'Medicine': 'supergpqa_medicine', 'Law': 'supergpqa_law'}
    for task in domains.values():
        pools[task] = []
    for row in read_jsonl(root / relative):
        domain = row['discipline']
        if domain not in domains or (domain == 'Law' and row['field'] != 'Law'):
            continue
        choices = [{'key': chr(65 + i), 'text': text} for i, text in enumerate(row['options'])]
        key = row['answer_letter']
        if dict((c['key'], c['text']) for c in choices).get(key) != row['answer']:
            raise ValueError(f'SuperGPQA answer/option mismatch: {row["uuid"]}')
        pools[domains[domain]].append(record(domains[domain], 'm-a-p/SuperGPQA', row['uuid'],
            'unspecified', relative, row['question'], [row['answer']], subject=domain.lower(),
            subfield=row['subfield'], mode='choice', choices=choices, answer_key=key,
            grader='exact_choice', attributes={'field': row['field'], 'difficulty': row['difficulty'],
                                               'is_calculation': row['is_calculation']}))

    relative = 'AfterQuery/FinanceQA/FinanceQA/test.csv'
    pools['financeqa'] = []
    with (root / relative).open(newline='') as stream:
        for index, row in enumerate(csv.DictReader(stream)):
            group = row['file_link'] or row['file_name'] or f'conceptual:{FINANCE_CONCEPT_GROUPS.get(index, index)}'
            pools['financeqa'].append(record('financeqa', 'AfterQuery/FinanceQA', f'row{index:04d}',
                'test', relative, row['question'], [row['answer']], context=row['context'],
                subject='finance', subfield=row['question_type'], group_id=f'FinanceQA:{group}',
                attributes={key: row[key] for key in ('question_type', 'company', 'file_link', 'file_name')}))

    relative = 'openai/frontierscience/olympiad/test.jsonl'
    pools['frontierscience_olympiad'] = []
    for index, row in enumerate(read_jsonl(root / relative)):
        item = record('frontierscience_olympiad', 'openai/frontierscience',
            f'{row["task_group_id"]}__row{index:03d}', 'test', relative, row['problem'], [row['answer']],
            subject=row['subject'], subfield=row['subject'], group_id=f'FrontierScience:{row["task_group_id"]}')
        item['source']['config'] = 'olympiad'
        pools['frontierscience_olympiad'].append(item)

    archive = root / 'allenai/scicite/scicite.tar.gz'
    if not archive.is_file():
        raise FileNotFoundError(f'Download {SCICITE_URL} to {archive} before building')
    with tarfile.open(archive, 'r:gz') as tar:
        for split, name in [('train', 'train')]:
            member = f'scicite/{name}.jsonl'
            pools[f'scicite_{split}'] = []
            with tar.extractfile(member) as stream:
                for line in stream:
                    row = json.loads(line)
                    if not row.get('citingPaperId'):
                        continue
                    pools[f'scicite_{split}'].append(record('scicite', 'allenai/scicite',
                        row['unique_id'], split, f'allenai/scicite/scicite.tar.gz::{member}',
                        row['string'], [row['label']], subject='scientific_citation', mode='label',
                        choices=[{'key': label, 'text': label} for label in LABELS],
                        answer_key=row['label'], grader='exact_label',
                        group_id=f'SciCite:{row["citingPaperId"]}',
                        attributes={key: row.get(key) for key in ('sectionName', 'citingPaperId', 'citedPaperId')}))
    return pools


def deduplicate(rows):
    seen_ids, seen_text, result = set(), set(), []
    for row in sorted(rows, key=lambda r: r['id']):
        key = normalized(row['input']['question'])
        if row['id'] in seen_ids or key in seen_text:
            continue
        seen_ids.add(row['id'])
        seen_text.add(key)
        result.append(row)
    return result


def proportional_quotas(counts, size):
    total = sum(counts.values())
    if total < size or size < 0:
        raise ValueError(f'Insufficient population: need {size}, have {total}')
    quotas = {key: size * count // total for key, count in counts.items()}
    ranked = sorted(counts, key=lambda key: (-(size * counts[key] % total), key))
    for key in ranked[:size - sum(quotas.values())]:
        quotas[key] += 1
    return quotas


def stratified_sample(rows, size, key, rng, quotas=None, excluded_groups=None, unique_groups=False):
    excluded_groups = set(excluded_groups or ())
    buckets = defaultdict(list)
    for row in sorted(rows, key=lambda r: r['id']):
        if row['source']['group_id'] not in excluded_groups:
            buckets[key(row)].append(row)
    quotas = quotas if quotas is not None else proportional_quotas({k: len(v) for k, v in buckets.items()}, size)
    if sum(quotas.values()) != size:
        raise ValueError('Quotas must sum to sample size')
    result = []
    for label, count in sorted(quotas.items()):
        candidates = buckets[label][:]
        rng.shuffle(candidates)
        selected = 0
        for row in candidates:
            group = row['source']['group_id']
            if group in excluded_groups:
                continue
            if selected == count:
                break
            result.append(row)
            selected += 1
            if unique_groups:
                excluded_groups.add(group)
        if selected != count:
            raise ValueError(f'Insufficient independent samples in stratum {label}: {selected}/{count}')
    return result


def stratum(row):
    attributes = row['attributes']
    if row['answer_mode'] == 'choice':
        return (attributes['subfield'], attributes['difficulty'], str(attributes['is_calculation']))
    return attributes['subfield']


def select_cohort(pools, seed):
    rng = random.Random(seed)
    selected = []
    for task in HELDIN_TASKS[:2] + HELDOUT_TASKS:
        rows = deduplicate(pools[task])
        existing = {normalized(r['input']['question']) for r in selected}
        rows = [r for r in rows if normalized(r['input']['question']) not in existing]
        quotas = FINANCE_QUOTAS if task == 'financeqa' else FRONTIER_QUOTAS if task == 'frontierscience_olympiad' else None
        sampled = stratified_sample(rows, TASK_SIZE, stratum, rng, quotas)
        for row in sampled:
            row['split'] = 'heldin' if task in HELDIN_TASKS else 'heldout'
        selected.extend(sampled)

    existing = {normalized(r['input']['question']) for r in selected}
    rows = [r for r in deduplicate(pools['scicite_train'])
            if normalized(r['input']['question']) not in existing]
    sampled = stratified_sample(rows, TASK_SIZE, lambda r: r['target']['answer_key'],
                                rng, SCICITE_QUOTAS, unique_groups=True)
    for row in sampled:
        row['split'] = 'heldin'
    selected.extend(sampled)
    return sorted(selected, key=lambda r: r['id'])


def near_duplicates(rows):
    tokens = [set(re.findall(r'\w+', normalized(r['input']['question']))) for r in rows]
    suspects = []
    for i, left in enumerate(tokens):
        if len(left) < MIN_AUDIT_TOKENS:
            continue
        for j in range(i):
            right = tokens[j]
            if min(len(left), len(right)) < MIN_AUDIT_TOKENS:
                continue
            overlap = len(left & right) / len(left | right)
            if overlap >= NEAR_DUPLICATE_THRESHOLD:
                suspects.append({'ids': [rows[j]['id'], rows[i]['id']], 'token_jaccard': round(overlap, 4),
                                 'cross_split': rows[j]['split'] != rows[i]['split']})
    return suspects


def validate_records(rows, schema):
    validator = jsonschema.Draft202012Validator(schema)
    ids, questions, groups = set(), set(), {}
    for row in rows:
        validator.validate(row)
        if row['id'] in ids:
            raise ValueError(f'Duplicate ID: {row["id"]}')
        ids.add(row['id'])
        text = normalized(row['input']['question'])
        if not text or text in questions:
            raise ValueError(f'Empty or duplicate question: {row["id"]}')
        questions.add(text)
        group, split = row['source']['group_id'], row['split']
        if group in groups and groups[group] != split:
            raise ValueError(f'Source group crosses splits: {group}')
        groups[group] = split
        mode, choices, target = row['answer_mode'], row['input']['choices'], row['target']
        if mode != 'free_form':
            mapping = {choice['key']: choice['text'] for choice in choices}
            if len(mapping) != len(choices) or target['answer_key'] not in mapping:
                raise ValueError(f'Invalid answer key/choices: {row["id"]}')
            if target['answers'] != [mapping[target['answer_key']]]:
                raise ValueError(f'Answer text does not match key: {row["id"]}')
        elif choices or target['answer_key'] is not None:
            raise ValueError(f'Free-form task has choices/key: {row["id"]}')
        expected_grader = {'choice': 'exact_choice', 'label': 'exact_label'}.get(mode)
        if expected_grader and row['grading']['method'] != expected_grader:
            raise ValueError('Answer mode/grader mismatch')
        if (row['task_type'] == 'cls') != (mode == 'label'):
            raise ValueError('Task type/answer mode mismatch')
    if Counter(r['split'] for r in rows) != SPLIT_COUNTS:
        raise ValueError('Incorrect split counts')
    expected = {(t, 'heldin'): TASK_SIZE for t in HELDIN_TASKS}
    expected.update({(t, 'heldout'): 50 for t in HELDOUT_TASKS})
    if Counter((r['task'], r['split']) for r in rows) != expected:
        raise ValueError('Incorrect task/split allocation')


def manifests(rows):
    return {split: [{'id': r['id']} for r in rows if r['split'] == split] for split in SPLIT_COUNTS}


def validate_directory(directory, schema):
    rows = read_jsonl(directory / 'data.jsonl')
    validate_records(rows, schema)
    for split, expected in manifests(rows).items():
        if read_jsonl(directory / f'{split}.jsonl') != expected:
            raise ValueError(f'Manifest differs from data: {split}')
    provenance = json.loads((directory / 'provenance.json').read_text())
    if provenance['counts'] != dict(Counter(r['split'] for r in rows)):
        raise ValueError('Provenance counts differ from data')
    return rows


def serialize(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def jsonl(rows):
    return ''.join(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n' for row in rows)


def write_artifacts(output, artifacts):
    for name, content in artifacts.items():
        path = output / name
        if path.exists() and path.read_text() != content:
            raise ValueError(f'Refusing to replace a different cohort artifact: {path}; use a new output directory')
    output.mkdir(parents=True, exist_ok=True)
    for name, content in artifacts.items():
        (output / name).write_text(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--seed', type=int, default=DEFAULT_SEED)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    schema = json.loads((args.output / 'schema.json' if args.validate_only else SCHEMA_PATH).read_text())
    if not args.validate_only:
        pools = load_sources(args.source_root)
        rows = select_cohort(pools, args.seed)
        validate_records(rows, schema)
        suspects = near_duplicates(rows)
        if any(pair['cross_split'] for pair in suspects):
            raise ValueError(f'Cross-split near-duplicates require review before publishing: {suspects}')
        provenance = {
            'schema_version': 1, 'seed': args.seed, 'counts': SPLIT_COUNTS,
            'source_root': 'huggingface.co/datasets (override with --source-root)',
            'scicite_download_url': SCICITE_URL,
            'source_pool_counts': {task: len(rows) for task, rows in pools.items()},
            'deduplicated_pool_counts': {task: len(deduplicate(rows)) for task, rows in pools.items()},
            'task_counts': dict(Counter(r['task'] for r in rows)),
            'finance_quotas': FINANCE_QUOTAS, 'frontier_quotas': FRONTIER_QUOTAS,
            'finance_reviewed_concept_groups': FINANCE_CONCEPT_GROUPS,
            'sampling': 'seeded proportional largest-remainder strata; explicit quotas for FinanceQA, FrontierScience and SciCite',
            'scicite_policy': 'train only; 50 held-in samples, labels 17/17/16; one sample per citing paper',
            'near_duplicate_audit': {'token_jaccard_threshold': NEAR_DUPLICATE_THRESHOLD,
                                     'minimum_unique_tokens': MIN_AUDIT_TOKENS, 'pairs': suspects},
            'limitations': ['Lexical deduplication does not establish semantic or historical pretraining decontamination.',
                           'FinanceQA document-backed questions are concentrated in Costco reports.',
                           'SciCite is held-in only; the held-out cohort does not test classification transfer.',
                           'Grading methods are contracts; QA evaluation adapters are not implemented by this dataset builder.'],
        }
        artifacts = {'data.jsonl': jsonl(rows), 'schema.json': serialize(schema), 'provenance.json': serialize(provenance)}
        artifacts.update({f'{split}.jsonl': jsonl(items) for split, items in manifests(rows).items()})
        write_artifacts(args.output, artifacts)
    rows = validate_directory(args.output, schema)
    print(serialize({'output': str(args.output), 'samples': len(rows), 'splits': dict(Counter(r['split'] for r in rows)),
                     'tasks': dict(Counter(r['task'] for r in rows)), 'validation': 'passed'}), end='')


if __name__ == '__main__':
    main()
