"""Unified binary grading for the QA transfer cohort."""

from dataclasses import asdict

from .olympiad_grading import DEFAULT_STARTUP_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS, grade_olympiad
from .qa_judge import judge_answer

METHODS = {'exact_choice', 'exact_label', 'olympiadbench', 'reference_judge'}


async def grade_answer(sample, final_answer, *, judge=None, math_timeout=DEFAULT_TIMEOUT_SECONDS,
                       math_startup_timeout=DEFAULT_STARTUP_TIMEOUT_SECONDS):
    method = sample['grading']['method']
    if method not in METHODS:
        raise ValueError(f'Unknown QA grading method: {method}')
    if not isinstance(final_answer, str):
        raise TypeError('Final answer must be a string')
    if method in ('exact_choice', 'exact_label'):
        key = sample['target']['answer_key']
        choices = {choice['key'] for choice in sample['input']['choices']}
        if not key or key not in choices:
            raise ValueError('Invalid reference choice/label key')
        correct = final_answer.strip() == key
        return {'score': int(correct), 'method': method, 'reason': 'match' if correct else 'wrong_or_invalid_key'}
    if method == 'olympiadbench':
        score = await grade_olympiad(sample['target']['answers'], final_answer,
                                    sample['grading']['parameters'], timeout=math_timeout,
                                    startup_timeout=math_startup_timeout)
        return {'score': score, 'method': method, 'reason': 'equivalent' if score else 'not_equivalent'}
    if judge is None and final_answer.strip():
        raise ValueError('Reference judging requires a separate judge model')
    result = await judge_answer(question=sample['input']['question'], context=sample['input']['context'],
                                reference_answers=sample['target']['answers'], candidate_answer=final_answer, model=judge)
    return {**asdict(result), 'method': method}
