import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from pluginrsi.evaluation.olympiad_grading import compare, grade_olympiad, precision_values
from pluginrsi.evaluation.qa_grading import grade_answer
from pluginrsi.runtime import InfraError


def sample(method='exact_choice'):
    return {'input': {'question': 'Q', 'context': '', 'choices': [{'key': 'A', 'text': 'one'}, {'key': 'B', 'text': 'two'}]},
            'target': {'answer_key': 'A', 'answers': ['one']}, 'grading': {'method': method, 'parameters': {}}}


@pytest.mark.parametrize('prediction,score', [('A', 1), (' A\n', 1), ('B', 0), ('a', 0), ('A or B', 0), ('Answer: A', 0), ('', 0)])
def test_choice_grades_only_final_key(prediction, score):
    assert asyncio.run(grade_answer(sample(), prediction))['score'] == score


def test_labels_use_accuracy_not_per_sample_f1():
    row = sample('exact_label')
    row['input']['choices'] = [{'key': 'method', 'text': 'method'}, {'key': 'result', 'text': 'result'}]
    row['target'] = {'answer_key': 'method', 'answers': ['method']}
    assert asyncio.run(grade_answer(row, 'method'))['score'] == 1
    assert asyncio.run(grade_answer(row, 'result'))['score'] == 0


def test_reference_grading_uses_judge_and_preserves_failure():
    class Model:
        async def complete(self, messages, **kwargs):
            assert json.loads(messages[1]['content'])['candidate_answer'] == 'paraphrase'
            return {'status': 'completed', 'output_text': '{"correct":true,"reason":"Equivalent"}', 'output': []}
    assert asyncio.run(grade_answer(sample('reference_judge'), 'paraphrase', judge=Model()))['score'] == 1
    assert asyncio.run(grade_answer(sample('reference_judge'), ''))['score'] == 0
    with pytest.raises(ValueError, match='separate judge'):
        asyncio.run(grade_answer(sample('reference_judge'), 'one'))
    class Broken:
        async def complete(self, *args, **kwargs):
            raise InfraError('judge unavailable')
    with pytest.raises(InfraError):
        asyncio.run(grade_answer(sample('reference_judge'), 'one', judge=Broken()))


@pytest.mark.parametrize('reference,prediction,params,correct', [
    ('\\frac{1}{2}', '0.5', {}, True), ('\\sqrt{4}', '2', {}, True),
    ('x+x', '2x', {}, True), ('x', 'x+0.0001', {}, False),
    ('x=2', '2x=4', {}, True), ('x=2', 'x=3', {}, False),
    ('2', '200', {}, False), ('0.15', '15', {}, False),
    ('1.0', '1.05', {'error': '0.1'}, True), ('1', '1.2', {'error': '0.1'}, False),
    ('1', '1.00001', {'error': '0'}, False), ('1e-10', '0.0000000001', {'error': '0'}, True),
    ('(1,2,3)', '(1,2)', {'answer_type': 'Tuple'}, False),
    ('(1,2)', '(2,1)', {'answer_type': 'Tuple'}, False),
    ('(1,2),(3,4)', '(3,4),(1,2)', {'answer_type': 'Tuple'}, True),
    ('[0,1)', '[0,1]', {'answer_type': 'Interval'}, False),
    ('3,4', '4,3', {}, True), ('3,4', '3', {}, False),
    ('3,3', '3,4', {}, False), ('\\pm 2', '-2,+2', {}, True),
    ('\\frac{1}{2}', '\\boxed{0.5}', {}, True), ('1', '\\boxed{1', {}, False),
    ('10', '10 \\mathrm{m}', {'unit': 'm'}, True), ('10', '10 \\mathrm{s}', {'unit': 'm'}, False),
    ('$\\\\frac{1}{2}$', '\\frac{2}{4}', {}, True),
    ('1,2', '1,2.05', {'error': ',0.1'}, True),
    ('1', '\\frac{', {}, False),
    ('a=\\text{invalid label},2', 'a=\\text{invalid label},2', {}, True),
])
def test_mathematical_equivalence(reference, prediction, params, correct):
    assert compare(reference, prediction, params) is correct


@pytest.mark.parametrize('value', ['-1', 'nan', 'inf'])
def test_invalid_reference_tolerance_rejected(value):
    with pytest.raises(ValueError):
        precision_values(value)


def test_real_math_subprocess_and_canonical_reference():
    assert asyncio.run(grade_olympiad(['\\frac{1}{2}', '999'], '0.5', {}, timeout=60)) == 1
    assert asyncio.run(grade_olympiad(['3', '4'], '4', {}, timeout=60)) == 0


@pytest.mark.parametrize('mode', ['compute_timeout', 'startup_timeout', 'cancel'])
def test_math_subprocess_is_reaped_on_timeout_or_cancellation(monkeypatch, mode):
    class Process:
        returncode = None
        killed = waited = False
        def __init__(self):
            self.stdout = SimpleNamespace(readline=self.ready)
        async def ready(self):
            if mode == 'startup_timeout':
                await asyncio.Event().wait()
            return b'{"ready": true}\n'
        async def communicate(self, data):
            await asyncio.Event().wait()
        def kill(self):
            self.killed = True
            self.returncode = -9
        async def wait(self):
            self.waited = True
    process = Process()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    async def run():
        task = asyncio.create_task(grade_olympiad(['1'], '1', {}, timeout=0.01, startup_timeout=0.01))
        if mode == 'cancel':
            await asyncio.sleep(0)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if mode == 'cancel' else InfraError):
            await task
    asyncio.run(run())
    assert process.killed and process.waited
