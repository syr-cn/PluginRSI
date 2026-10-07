import copy
import importlib.util
import json
from pathlib import Path
import random

import jsonschema
import pytest

from pluginrsi.evaluation.runner import read_tasks

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'datasets/qa_transfer_v1'
SPEC = importlib.util.spec_from_file_location('prepare_qa_transfer', ROOT / 'scripts/prepare_qa_transfer.py')
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


@pytest.fixture
def cohort():
    return prepare.read_jsonl(DATA / 'data.jsonl')


@pytest.fixture
def schema():
    return json.loads((DATA / 'schema.json').read_text())


def test_generated_cohort_and_runner_manifests(schema):
    rows = prepare.validate_directory(DATA, schema)
    assert len(rows) == 400
    for split, count in {'heldin': 150, 'heldout': 250}.items():
        manifest = read_tasks(DATA / f'{split}.jsonl')
        assert len(manifest) == count
        assert all(set(row) == {'id'} for row in manifest)


def test_source_split_and_label_balance(cohort):
    rows = [r for r in cohort if r['task'] == 'scicite']
    assert {r['split'] for r in rows} == {'heldin'}
    assert {r['source']['split'] for r in rows} == {'train'}
    assert len({r['source']['group_id'] for r in rows}) == 50
    assert [sum(r['target']['answer_key'] == label for r in rows) for label in prepare.LABELS] == [17, 17, 16]
    assert {p.name for p in DATA.glob('*.jsonl')} == {'data.jsonl', 'heldin.jsonl', 'heldout.jsonl'}
    finance = [r for r in cohort if r['task'] == 'financeqa']
    assert prepare.Counter(r['attributes']['question_type'] for r in finance) == prepare.FINANCE_QUOTAS
    assert all(r['input']['context'] == '' for r in finance if r['attributes']['question_type'] == 'conceptual')
    frontier = [r for r in cohort if r['task'] == 'frontierscience_olympiad']
    assert prepare.Counter(r['subject'] for r in frontier) == prepare.FRONTIER_QUOTAS


@pytest.mark.parametrize('corruption', ['duplicate', 'group_leak', 'bad_answer', 'extra_input', 'wrong_count'])
def test_validation_rejects_corrupted_cohort(cohort, schema, corruption):
    rows = copy.deepcopy(cohort)
    if corruption == 'duplicate':
        rows[1]['id'] = rows[0]['id']
    elif corruption == 'group_leak':
        left = next(r for r in rows if r['split'] == 'heldin')
        right = next(r for r in rows if r['split'] == 'heldout')
        right['source']['group_id'] = left['source']['group_id']
    elif corruption == 'bad_answer':
        next(r for r in rows if r['answer_mode'] == 'choice')['target']['answer_key'] = 'Z'
    elif corruption == 'extra_input':
        rows[0]['input']['answer'] = 'leaked answer'
    else:
        rows.pop()
    with pytest.raises((ValueError, jsonschema.ValidationError)):
        prepare.validate_records(rows, schema)


def test_group_sampling_excludes_papers_and_is_order_independent():
    rows = [{'id': str(i), 'label': str(i % 2), 'source': {'group_id': str(i // 2)}} for i in range(20)]
    sample = lambda items: prepare.stratified_sample(items, 4, lambda r: r['label'], random.Random(42),
        quotas={'0': 2, '1': 2}, excluded_groups={'0', '1'}, unique_groups=True)
    left = sample(rows)
    assert left == sample(list(reversed(rows)))
    groups = [r['source']['group_id'] for r in left]
    assert len(set(groups)) == 4
    assert not set(groups) & {'0', '1'}
    with pytest.raises(ValueError, match='Insufficient'):
        prepare.stratified_sample(rows[:2], 2, lambda r: r['label'], random.Random(42),
                                   quotas={'0': 1, '1': 1}, unique_groups=True)


def test_existing_cohort_not_partially_overwritten(tmp_path):
    existing = tmp_path / 'heldin.jsonl'
    existing.write_text('old\n')
    with pytest.raises(ValueError, match='Refusing to replace'):
        prepare.write_artifacts(tmp_path, {'data.jsonl': 'new data\n', 'heldin.jsonl': 'new\n'})
    assert existing.read_text() == 'old\n'
    assert not (tmp_path / 'data.jsonl').exists()


def test_near_duplicate_audit_detects_cross_split_template():
    question = 'A scientific question about energy temperature pressure mass volume entropy heat work force velocity'
    rows = [{'id': 'a', 'split': 'heldin', 'input': {'question': question}},
            {'id': 'b', 'split': 'heldout', 'input': {'question': question + ' acceleration'}}]
    assert prepare.near_duplicates(rows)[0]['cross_split']


def test_no_reasoning_traces_in_model_input(cohort):
    assert all(set(row['input']) == {'question', 'context', 'choices'} for row in cohort)
    assert all('chain_of_thought' not in row and 'solution' not in row for row in cohort)


def test_exact_deduplication_preserves_math_operators():
    rows = [{'id': str(i), 'input': {'question': question}} for i, question in enumerate([
        'Find x + y', 'Find x - y', '  Find  x + y  '])]
    assert [r['id'] for r in prepare.deduplicate(rows)] == ['0', '1']
