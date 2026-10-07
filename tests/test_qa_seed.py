import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pluginrsi.contracts import Task
from pluginrsi.evaluation.benchmarks.qa_transfer import QAEnvironment
from pluginrsi.evaluation.qa_calculator import calculate, run_calculation
from pluginrsi.loader import execute, library_refs, validate
from pluginrsi.runtime import EventLog, Runtime

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / 'seeds/agents/qa_transfer/v0001'
LIBRARY = ROOT / 'seeds/plugin_library_qa'


def call(name, args, iid):
    return {'type': 'function_call', 'name': name, 'arguments': json.dumps(args), 'call_id': iid}


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.histories = []
    async def complete(self, messages, tools):
        self.histories.append(copy.deepcopy(messages))
        assert {t['name'] for t in tools} == {'calculator', 'finish'}
        return {'output': next(self.responses), 'output_text': ''}


def test_screened_library_and_unchanged_memory():
    validate(SEED, [LIBRARY])
    refs = library_refs([LIBRARY])
    assert len(refs) == 6
    assert not any('prior_' in ref or '/terminal/' in ref for ref in refs)
    for p in (LIBRARY / 'memory/experience_bank/v0001').iterdir():
        if p.is_file():
            assert p.read_bytes() == (ROOT / 'seeds/plugin_library/memory/experience_bank/v0001' / p.name).read_bytes()
    for p in LIBRARY.glob('*/*/*/plugin.yaml'):
        manifest = yaml.safe_load(p.read_text())
        if manifest['name'] != 'experience_bank':
            assert manifest['provenance']['parent_refs']


@pytest.mark.parametrize('mode,answer,choices', [('choice','A',[{'key':'A','text':'one'}]),
    ('label','method',[{'key':'method','text':'method'}]), ('free_form','0.5',[])])
def test_seed_all_answer_modes(tmp_path, mode, answer, choices):
    model = Model([[call('finish', {'answer': answer}, 'f')]])
    emit = EventLog(tmp_path / 'trajectory.jsonl')
    runtime = Runtime(model, QAEnvironment(emit), emit, SEED, tmp_path)
    result = asyncio.run(execute(SEED, [LIBRARY], Task('test', 'Question', {'answer_mode':mode,'choices':choices}), runtime))
    assert result.output == answer
    events = [json.loads(line) for line in (tmp_path / 'trajectory.jsonl').read_text().splitlines()]
    invoked = {e['producer']['plugin_alias'] for e in events if e['event']=='plugin/input'}
    assert {'solver','analysis','hypotheses','completion'} <= invoked
    system = model.histories[0][0]['content']
    assert 'requested outputs' in system and 'candidate answers' in system
    assert 'Before submission' in system and 'software issue' not in system


def test_tool_memory_and_format_repair_are_real(tmp_path):
    model = Model([[call('calculator', {'expression':'30 / 200 * 100'}, 'c')],
                   [call('finish', {'answer':'Answer: A'}, 'bad')],
                   [call('finish', {'answer':'A'}, 'good')]])
    emit = EventLog(tmp_path / 'trajectory.jsonl')
    runtime=Runtime(model,QAEnvironment(emit),emit,SEED,tmp_path)
    task=Task('test','Compute the margin',{'answer_mode':'choice','choices':[{'key':'A','text':'15%'}]})
    result=asyncio.run(execute(SEED,[LIBRARY],task,runtime))
    assert result.output=='A' and len(model.histories)==3
    events = [json.loads(line) for line in (tmp_path / 'trajectory.jsonl').read_text().splitlines()]
    invoked={e['producer']['plugin_alias'] for e in events if e['event']=='plugin/input'}
    assert invoked=={'solver','analysis','hypotheses','completion','calculator','experience'}
    assert any('15' in m.get('content','') for m in model.histories[1] if m.get('role')=='user')
    assert any('Invalid answer format' in m.get('output','') for m in model.histories[2])
    stored=(tmp_path/'state/experience/entries.jsonl').read_text()
    assert '15' in stored


def test_memory_starts_empty_for_each_task(tmp_path):
    async def run():
        for index in range(2):
            work=tmp_path/str(index);work.mkdir()
            model=Model([[],[call('finish',{'answer':'answer'},'f')]])
            runtime=Runtime(model,QAEnvironment(),lambda *a,**k:None,SEED,work)
            await execute(SEED,[LIBRARY],Task(str(index),'Q',{'answer_mode':'free_form','choices':[]}),runtime)
            assert not any('Relevant task-local' in m.get('content','') for m in model.histories[1])
    asyncio.run(run())


def test_seed_cannot_claim_success_at_turn_limit(tmp_path):
    model = Model([[]] * 20)
    runtime = Runtime(model, QAEnvironment(), lambda *a, **k: None, SEED, tmp_path)
    result = asyncio.run(execute(SEED, [LIBRARY], Task('test', 'Q', {'answer_mode': 'free_form', 'choices': []}), runtime))
    assert len(model.histories) == 20
    assert result.output == '' and result.termination_reason == 'turn_limit'


@pytest.mark.parametrize('expression,expected', [('30/200*100',15), ('sqrt(81)+2**3',17), ('sin(pi/2)',1), ('round(1/3,4)',0.3333)])
def test_calculator_numeric_results(expression, expected):
    assert calculate(expression)==expected


@pytest.mark.parametrize('expression', ['__import__("os").getcwd()', 'open("data.jsonl")',
    '(1).__class__', '[x for x in (1,2)]','lambda: 1', 'True', '2**1000000',
    'exp(1000000)', '1/0', 'x+1'])
def test_calculator_rejects_code_and_unbounded_inputs(expression):
    with pytest.raises((ValueError, SyntaxError, ArithmeticError)):
        calculate(expression)


def test_real_cpu_calculator():
    result=asyncio.run(run_calculation('sqrt(81) + 2**3'))
    assert not result.is_error and json.loads(result.content)['result']==17
    invalid=asyncio.run(run_calculation('open("/etc/passwd")'))
    assert invalid.is_error


def test_calculator_child_reaped_on_cancel(monkeypatch):
    class Process:
        returncode=None
        killed=waited=False
        async def communicate(self,*args): await asyncio.Event().wait()
        def kill(self): self.killed=True;self.returncode=-9
        async def wait(self): self.waited=True
    process=Process()
    async def spawn(*args,**kwargs): return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    async def run():
        task=asyncio.create_task(run_calculation('1+1'))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    assert process.killed and process.waited
