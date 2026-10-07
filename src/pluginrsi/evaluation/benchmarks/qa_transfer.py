"""Text QA/classification evaluation with private reference grading."""

import asyncio
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from ...contracts import SolveResult, Task
from ...loader import execute
from ...runtime import BudgetExceeded, EventLog, InfraError, InvalidToolCall, Runtime, SolverAgent
from ...schemas import QABenchmarkOptions, SolverConfig, read_yaml
from ...search.store import write_json
from ..progress import mark
from ..qa_data import read_qa_data
from ..qa_grading import grade_answer

FINISH_TOOL = {'type': 'function', 'name': 'finish', 'description': 'Submit the final answer and end this task.',
               'parameters': {'type': 'object', 'properties': {'answer': {'type': 'string'}},
                              'required': ['answer'], 'additionalProperties': False}}
LABEL_HELP = 'method: use of a cited method or resource; background: context or prior knowledge; result: comparison or discussion of results.'
DEFAULT_VERIFIER_TIMEOUT_SECONDS = 420


def public_task(sample):
    data, mode = sample['input'], sample['answer_mode']
    sections = []
    if mode == 'label':
        sections.append('Classify the citation intent. ' + LABEL_HELP)
    if data['context']:
        sections.append('Context:\n' + data['context'])
    sections.append(('Citation:\n' if mode == 'label' else 'Question:\n') + data['question'])
    if data['choices']:
        sections.append('Allowed answers:\n' + '\n'.join(f"{c['key']}: {c['text']}" for c in data['choices']))
        sections.append('Submit exactly one allowed answer key, without explanation, as your final answer.')
    elif sample['grading']['method'] == 'olympiadbench':
        sections.append('Submit only the final mathematical answer(s) in LaTeX, with no derivation. '
                        'Separate multiple required answers with commas; preserve ordered tuples. '
                        'Use the units requested by the problem.')
    else:
        sections.append('Submit the final answer addressing all required parts. Include units and necessary qualifications.')
    return Task(sample['id'], '\n\n'.join(sections),
                {'answer_mode': mode, 'choices': [dict(c) for c in data['choices']], 'tools': [deepcopy(FINISH_TOOL)]})


class QAEnvironment:
    def __init__(self, emit=None):
        self.emit = emit

    async def calculate(self, expression, timeout_seconds=5):
        from ..qa_calculator import run_calculation
        if self.emit:
            self.emit('tool/request', command=expression, tool='calculator')
        result = await run_calculation(expression, timeout_seconds)
        if self.emit:
            self.emit('tool/response', content=result.content, is_error=result.is_error)
        return result

    async def call(self, name, arguments):
        raise InvalidToolCall('Use the calculator plugin; finish is handled by the workflow')


async def evaluate_qa(request):
    work = Path(request['work_dir'])
    work.mkdir(parents=True, exist_ok=True)
    evaluation = request['evaluation']
    options = QABenchmarkOptions.model_validate(evaluation['benchmark_options'])
    emit = EventLog(work / 'trajectory.jsonl')
    solver = judge = None
    status = 'completed'
    try:
        mark(work, 'environment')
        sample = read_qa_data(options.data_file)[request['task']['id']]
        task = public_task(sample)
        candidate = Path(request['harness'])
        solver = SolverAgent(SolverConfig.model_validate(request['solver']), emit)
        runtime = Runtime(solver, QAEnvironment(emit), emit, candidate, work)
        emit('task/input', instruction=task.instruction, answer_mode=sample['answer_mode'])
        mark(work, 'solving')
        try:
            solved = await asyncio.wait_for(execute(candidate, [Path(p) for p in request['libraries']], task, runtime),
                                            timeout=evaluation['task_timeout_seconds'])
            if not isinstance(solved, SolveResult) or not isinstance(solved.output, str):
                raise TypeError('Workflow must return SolveResult with string output')
        except (BudgetExceeded, asyncio.TimeoutError):
            solved = SolveResult('', termination_reason='solver_limit')
            status = 'solver_limit'
        except InvalidToolCall:
            solved = SolveResult('', termination_reason='invalid_tool_call')
        except InfraError:
            raise
        except Exception as error:
            return {'score': 0, 'status': 'candidate_error', 'detail': type(error).__name__}
        write_json(work / 'solve_result.json', asdict(solved))
        emit('task/output', output=solved.output, termination_reason=solved.termination_reason)
        mark(work, 'verifying')
        if solved.termination_reason != 'completed' or not solved.output.strip():
            return {'score': 0, 'status': status, 'detail': 'missing_final_answer', 'task_family': sample['task']}
        if sample['grading']['method'] == 'reference_judge':
            cfg = SolverConfig.model_validate(read_yaml(options.judge_config))
            # Judge traces and accounting must never mix with proposer-visible solver evidence.
            cfg.api_metrics_path = work / 'judge_api.jsonl'
            judge = SolverAgent(cfg, EventLog(work / 'judge_trajectory.jsonl'))
        try:
            verdict = await asyncio.wait_for(
                grade_answer(sample, solved.output, judge=judge, math_timeout=options.math_timeout_seconds,
                             math_startup_timeout=options.math_startup_timeout_seconds),
                timeout=evaluation.get('verifier_timeout_seconds') or DEFAULT_VERIFIER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as error:
            raise InfraError('QA verifier timed out') from error
        write_json(work / 'verifier_result.json', verdict)
        return {'score': verdict['score'], 'status': status, 'task_family': sample['task'],
                'grading_method': verdict['method']}
    except InfraError as error:
        return {'score': 0, 'status': 'infra_error', 'detail': type(error).__name__,
                'retryable': getattr(error, 'retryable', True)}
    finally:
        usage = {'model_calls': 0, 'solver_tokens': 0, 'model_api_failures': 0,
                 'judge_calls': 0, 'judge_tokens': 0, 'judge_api_failures': 0}
        if solver is not None:
            usage.update(model_calls=solver.calls, solver_tokens=solver.tokens, model_api_failures=solver.failed_calls)
        if judge is not None:
            usage.update(judge_calls=judge.calls, judge_tokens=judge.tokens, judge_api_failures=judge.failed_calls)
        write_json(work / 'usage.json', usage)
        emit('budget/used', model_calls=usage['model_calls'], solver_tokens=usage['solver_tokens'],
             model_api_failures=usage['model_api_failures'])
        for error in await asyncio.gather(*(model.close() for model in (solver, judge) if model is not None), return_exceptions=True):
            if isinstance(error, BaseException):
                emit('environment/cleanup_error', error_type=type(error).__name__)
        mark(work, 'done')
