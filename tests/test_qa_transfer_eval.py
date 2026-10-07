import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pluginrsi.contracts import SolveResult
from pluginrsi.evaluation import worker
from pluginrsi.evaluation.benchmarks import qa_transfer as backend
from pluginrsi.evaluation.qa_data import freeze_qa_inputs, inspect_qa_inputs, read_qa_data
from pluginrsi.runtime import BudgetExceeded, InfraError
from pluginrsi.schemas import load_config
from pluginrsi.search.store import write_json

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'datasets/qa_transfer_v1/data.jsonl'


@pytest.fixture
def request_data(tmp_path):
    return {'work_dir': str(tmp_path), 'harness': str(tmp_path / 'harness'), 'libraries': [],
            'task': {'id': 'test-one'}, 'solver': {'model': 'fake'},
            'evaluation': {'benchmark': 'qa_transfer', 'task_timeout_seconds': 10,
                'verifier_timeout_seconds': 20,
                'benchmark_options': {'data_file': str(DATA), 'judge_config': str(ROOT / 'datasets/qa_transfer_v1/judge.yaml')}}}


@pytest.fixture
def models(monkeypatch):
    created = []
    class Model:
        calls = tokens = failed_calls = 0
        closed = False
        def __init__(self, config, emit):
            self.config = config
            self.emit = emit
            created.append(self)
        async def complete(self, *args, **kwargs):
            self.calls += 1
            self.tokens += 25
            self.emit('model/request', private_reference='SECRET_GOLD')
            return {'status': 'completed', 'output': [], 'output_text': '{"correct":true,"reason":"Match"}'}
        async def close(self):
            self.closed = True
    monkeypatch.setattr(backend, 'SolverAgent', Model)
    return created


def row_for(task):
    return copy.deepcopy(next(r for r in read_qa_data(DATA).values() if r['task'] == task))


@pytest.mark.parametrize('task', ['supergpqa_economics', 'scicite', 'financeqa'])
def test_execution_and_private_grading(request_data, monkeypatch, models, task):
    row = row_for(task)
    monkeypatch.setattr(backend, 'read_qa_data', lambda path: {'test-one': row})
    async def execute(harness, libraries, public, runtime):
        assert set(public.metadata) == {'answer_mode', 'choices', 'tools'}
        assert 'grading' not in public.metadata and 'target' not in public.metadata
        assert row['input']['question'] in public.instruction
        assert row['source']['file'] not in public.instruction
        return SolveResult('paraphrase' if task == 'financeqa' else row['target']['answer_key'])
    monkeypatch.setattr(backend, 'execute', execute)
    result = asyncio.run(backend.evaluate_qa(request_data))
    assert result['score'] == 1 and result['status'] == 'completed'
    assert all(m.closed for m in models)
    work = Path(request_data['work_dir'])
    usage = json.loads((work / 'usage.json').read_text())
    assert usage['solver_tokens'] == 0
    assert usage['judge_tokens'] == (25 if task == 'financeqa' else 0)
    assert 'SECRET_GOLD' not in (work / 'trajectory.jsonl').read_text()
    assert json.loads((work / 'worker_stage.json').read_text())['stage'] == 'done'


@pytest.mark.parametrize('outcome,expected', [('empty', 'completed'), ('wrong', 'completed'),
    ('candidate', 'candidate_error'), ('budget', 'solver_limit'), ('infra', 'infra_error'), ('judge', 'infra_error')])
def test_failure_classification(request_data, monkeypatch, models, outcome, expected):
    row = row_for('financeqa' if outcome == 'judge' else 'scicite')
    monkeypatch.setattr(backend, 'read_qa_data', lambda path: {'test-one': row})
    async def execute(*args):
        errors = {'candidate': ValueError('broken'), 'budget': BudgetExceeded(), 'infra': InfraError('unavailable')}
        if outcome in errors:
            raise errors[outcome]
        return SolveResult('' if outcome == 'empty' else 'wrong')
    monkeypatch.setattr(backend, 'execute', execute)
    if outcome == 'judge':
        async def grade(*args, **kwargs):
            raise InfraError('invalid judge output')
        monkeypatch.setattr(backend, 'grade_answer', grade)
    result = asyncio.run(backend.evaluate_qa(request_data))
    assert result['status'] == expected and result['score'] == 0
    assert all(m.closed for m in models)


def test_worker_routes_qa_without_harbor(request_data, monkeypatch):
    async def evaluate(request):
        assert request['evaluation']['benchmark'] == 'qa_transfer'
        return {'score': 1, 'status': 'completed'}
    monkeypatch.setattr(backend, 'evaluate_qa', evaluate)
    async def forbidden(*args):
        raise AssertionError('Harbor must not run')
    monkeypatch.setattr(worker, 'evaluate_harbor', forbidden)
    path = Path(request_data['work_dir']) / 'request.json'
    write_json(path, request_data)
    asyncio.run(worker.main(path))
    assert json.loads(path.with_name('result.json').read_text())['score'] == 1


def test_real_loader_executes_test_workflow(request_data, models):
    work = Path(request_data['work_dir'])
    harness = work / 'harness'
    harness.mkdir()
    (harness / '__init__.py').write_text('')
    (harness / 'harness.yaml').write_text('schema_version: 1\nentrypoint: workflow:Workflow\nplugins: {}\n')
    (harness / 'workflow.py').write_text('from pluginrsi.contracts import SolveResult\nclass Workflow:\n'
        '    async def run(self, task, runtime, plugins):\n        return SolveResult("method")\n')
    row = next(r for r in read_qa_data(DATA).values() if r['task'] == 'scicite' and r['target']['answer_key'] == 'method')
    request_data['task'] = {'id': row['id']}
    path = work / 'request.json'
    write_json(path, request_data)
    asyncio.run(worker.main(path))
    assert json.loads((work / 'result.json').read_text())['score'] == 1


def test_freezing_removes_dependency_on_source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'data.jsonl').write_text(DATA.read_text())
    (source / 'judge.yaml').write_text((ROOT / 'datasets/qa_transfer_v1/judge.yaml').read_text())
    options = {'data_file': str(source / 'data.jsonl'), 'judge_config': str(source / 'judge.yaml')}
    freeze_qa_inputs(options, tmp_path / 'frozen')
    (source / 'data.jsonl').write_text('changed')
    (source / 'judge.yaml').unlink()
    assert len(inspect_qa_inputs(options, {})) == 400


def test_config_only_150_250_and_screened_seed_is_ready():
    spec = importlib.util.spec_from_file_location('qa_launcher', ROOT / 'scripts/experiment.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    settings, config = launcher.definition(ROOT / 'configs/qa_transfer.yaml')
    assert config.evaluation.feedback_tasks == config.evaluation.selection_tasks
    assert len(config.evaluation.feedback_tasks.read_text().splitlines()) == 150
    assert len(config.evaluation.heldout_tasks.read_text().splitlines()) == 250
    assert config.evaluation.parallel_heldout is True
    assert len(inspect_qa_inputs(config.evaluation.benchmark_options, {})) == 400
    assert config.seed.is_dir()
    assert config.plugin_library == ROOT / 'seeds/plugin_library_qa'
    config.solver.model = config.proposer.agent.model = 'fixture'
    assert launcher.inspect_config(settings, config)['model_or_sandbox_requests'] == 0


def test_plain_run_config_resolves_qa_paths(tmp_path):
    raw = yaml.safe_load((ROOT / 'configs/qa_transfer.yaml').read_text())
    raw.pop('experiment')
    raw['run_dir'] = 'run'
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path)
    assert config.evaluation.benchmark_options['data_file'] == str(tmp_path / 'datasets/qa_transfer_v1/data.jsonl')


def test_invalid_input_and_split_are_rejected(tmp_path):
    row = row_for('scicite')
    path = tmp_path / 'data.jsonl'
    path.write_text(json.dumps(row) + '\n' + json.dumps(row) + '\n')
    with pytest.raises(ValueError, match='unique'):
        read_qa_data(path)
    path.write_text(json.dumps(row) + '\n')
    options = {'data_file': str(path), 'judge_config': str(ROOT / 'datasets/qa_transfer_v1/judge.yaml')}
    with pytest.raises(ValueError, match='wrong split'):
        inspect_qa_inputs(options, {'heldout': [{'id': row['id']}]})
    row['input']['target'] = 'leak'
    path.write_text(json.dumps(row) + '\n')
    with pytest.raises(ValueError, match='input/target'):
        read_qa_data(path)


@pytest.mark.parametrize('via_launcher', [False, True])
def test_run_preparation_freezes_qa_inputs(tmp_path, via_launcher):
    from test_full_search import config_at
    from pluginrsi.search.loop import initialize
    config = config_at(tmp_path)
    source = tmp_path / 'qa.jsonl'
    source.write_text(DATA.read_text())
    rows = list(read_qa_data(source).values())
    heldin = [{'id': r['id']} for r in rows if r['split'] == 'heldin'][:4]
    heldout = [{'id': r['id']} for r in rows if r['split'] == 'heldout'][:1]
    config.evaluation.feedback_tasks.write_text(''.join(json.dumps(r) + '\n' for r in heldin))
    config.evaluation.heldout_tasks.write_text(''.join(json.dumps(r) + '\n' for r in heldout))
    config.evaluation.benchmark = 'qa_transfer'
    config.evaluation.benchmark_options = {'data_file': str(source), 'judge_config': str(ROOT / 'datasets/qa_transfer_v1/judge.yaml')}
    if via_launcher:
        spec = importlib.util.spec_from_file_location('qa_prepare_launcher', ROOT / 'scripts/experiment.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        raw = config.model_dump(mode='json')
        raw.pop('run_dir')
        raw['solver'].pop('api_metrics_path')
        raw['proposer']['agent'] = {'model': 'fixture'}
        raw['experiment'] = {'name': 'qa_fixture', 'output_root': str(tmp_path / 'persistent'),
            'local_root': str(tmp_path / 'local'), 'expected_feedback_tasks': 4,
            'expected_selection_tasks': 4, 'expected_heldout_tasks': 1}
        definition = tmp_path / 'experiment.yaml'
        definition.write_text(yaml.safe_dump(raw))
        output = launcher.prepare(definition, 'qa-freeze')
        frozen = load_config(output / 'config.yaml')
    else:
        initialize(config)
        frozen = load_config(config.run_dir / 'config.yaml')
    source.unlink()
    options = frozen.evaluation.benchmark_options
    assert len(read_qa_data(options['data_file'])) == 400
    assert Path(options['judge_config']).name == 'judge.yaml'
    assert Path(options['data_file']).parent.name == 'qa'
