"""Bounded mathematical answer comparison adapted from OpenBMB/OlympiadBench.

The upstream inference/judge.py selects final_answer[0]. Comma-separated answers,
LaTeX parsing and absolute tolerances follow that convention. Unlike upstream's
heuristics, this comparator does not allow arbitrary 100x numeric scaling, tuple
prefix matches, or approximate symbolic identities. See third_party/OlympiadBench.
"""

import asyncio
import json
import math
import re
import sys

DEFAULT_PRECISION = 1e-8
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_STARTUP_TIMEOUT_SECONDS = 120
MAX_ANSWER_CHARS = 16384
MAX_POWER = 1000
MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024


def precision_values(value):
    values = [DEFAULT_PRECISION] if value is None else [float(p) if p.strip() else DEFAULT_PRECISION for p in str(value).split(',')]
    if any(not math.isfinite(p) or p < 0 for p in values):
        raise ValueError('Invalid reference tolerance')
    return values


def split_parts(text):
    parts, start, depth = [], 0, 0
    for index, char in enumerate(text):
        if char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
            if depth < 0:
                raise ValueError('Unbalanced expression')
        elif char == ',' and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    if depth:
        raise ValueError('Unbalanced expression')
    parts.append(text[start:].strip())
    if any(not part for part in parts):
        raise ValueError('Empty answer component')
    return parts


def normalize(text, unit=None):
    text = text.strip().replace('\\\\', '\\')
    boxes = []
    for match in re.finditer(r'\\boxed\s*\{', text):
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += (text[end] == '{') - (text[end] == '}')
            end += 1
        if depth:
            raise ValueError('Unclosed boxed answer')
        boxes.append(text[match.end():end - 1])
    if boxes:
        text = ','.join(boxes)
    for value in ('$', '\\left', '\\right', '\\(', '\\)', '\\[', '\\]', '\\,', '\\!'):
        text = text.replace(value, '')
    text = text.replace('，', ',').replace('−', '-').replace('\\dfrac', '\\frac').replace('\\tfrac', '\\frac')
    text = re.sub(r'\\(?:mathrm|text)\{([^{}]*)\}', r'\1', text).strip().rstrip('.;')
    if unit:
        clean_unit = normalize(unit)
        if clean_unit and text.endswith(clean_unit):
            text = text[:-len(clean_unit)].rstrip()
    return text


def expand_parts(text):
    parts = split_parts(text)
    return [value for part in parts for value in
            ([part.replace('\\pm', '+'), part.replace('\\pm', '-')] if '\\pm' in part else [part])]


def compare(reference, candidate, parameters):
    import sympy as sp
    from sympy.parsing.latex import parse_latex
    from sympy.parsing.latex.errors import LaTeXParsingError

    tolerances = precision_values(parameters.get('error'))
    unit = parameters.get('unit')
    truth = normalize(reference, unit)
    try:
        prediction = normalize(candidate, unit)
        expected, actual = expand_parts(truth), expand_parts(prediction)
    except ValueError:
        return False
    if len(expected) != len(actual):
        return False
    if len(tolerances) == 1:
        tolerances *= len(expected)
    if len(tolerances) != len(expected):
        raise ValueError('Reference tolerance count differs from answer component count')

    def parse(text):
        # Preserve scientific notation rather than parsing e as a symbolic variable.
        if re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', text.strip()):
            exponent = re.search(r'[eE]([+-]?\d+)$', text.strip())
            if exponent and abs(int(exponent[1])) > MAX_POWER:
                raise ValueError('Exponent exceeds comparison limit')
            return sp.Rational(text.strip())
        parsed = parse_latex(text, strict=True)
        for power in parsed.atoms(sp.Pow):
            if power.exp.is_number and (power.exp.is_finite is not True or abs(power.exp) > MAX_POWER):
                raise ValueError('Exponent exceeds comparison limit')
        return parsed

    def equal(left, right, precision):
        if left == right and left:
            return True
        if left.startswith(('(', '[')) and left.endswith((')', ']')):
            if not right or left[0] != right[0] or left[-1] != right[-1]:
                return False
            ls, rs = split_parts(left[1:-1]), split_parts(right[1:-1])
            return len(ls) == len(rs) and all(equal(a, b, precision) for a, b in zip(ls, rs))
        if '\\cup' in left or '\\cup' in right:
            ls, rs = left.split('\\cup'), right.split('\\cup')
            return len(ls) == len(rs) and all(equal(a.strip(), b.strip(), precision) for a, b in zip(ls, rs))
        if left.count('=') == right.count('=') == 1:
            la, lb = left.split('=')
            ra, rb = right.split('=')
            first, second = sp.simplify(parse(la) - parse(lb)), sp.simplify(parse(ra) - parse(rb))
            if first == second:
                return True
            ratio = sp.simplify(first / second)
            return ratio.is_number is True and ratio.is_finite is True and ratio.is_zero is False
        if '=' in left:
            left = left.split('=', 1)[1]
        if '=' in right:
            right = right.split('=', 1)[1]
        first, second = parse(left), parse(right)
        difference = sp.simplify(first - second)
        if difference == 0:
            return True
        if first.free_symbols or second.free_symbols:
            return False
        return difference.is_finite is True and bool(abs(difference.evalf()) <= precision)

    # Match whole components one-to-one; ordered tuple components stay ordered.
    adjacency = []
    for left, precision in zip(expected, tolerances):
        edges = []
        for index, right in enumerate(actual):
            try:
                matches = equal(left, right, precision)
            except (ValueError, TypeError, SyntaxError, NotImplementedError, LaTeXParsingError):
                matches = False
            if matches:
                edges.append(index)
        adjacency.append(edges)
    assigned = {}

    def match(index, seen):
        for target in adjacency[index]:
            if target not in seen:
                seen.add(target)
                if target not in assigned or match(assigned[target], seen):
                    assigned[target] = index
                    return True
        return False

    return all(match(index, set()) for index in range(len(expected)))


async def grade_olympiad(answers, candidate, parameters, timeout=DEFAULT_TIMEOUT_SECONDS,
                         startup_timeout=DEFAULT_STARTUP_TIMEOUT_SECONDS):
    from ..runtime import InfraError

    if not isinstance(answers, list) or not answers or not all(isinstance(a, str) and a.strip() for a in answers):
        raise ValueError('OlympiadBench requires reference answers')
    precision_values(parameters.get('error'))
    if not candidate.strip() or len(candidate) > MAX_ANSWER_CHARS:
        return 0
    request = json.dumps({'reference': answers[0], 'candidate': candidate, 'parameters': parameters}).encode()
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'pluginrsi.evaluation.olympiad_grading',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        ready = await asyncio.wait_for(process.stdout.readline(), startup_timeout)
        if ready != b'{"ready": true}\n':
            raise InfraError('Mathematical comparison initialization failed; check QA dependencies')
        stdout, stderr = await asyncio.wait_for(process.communicate(request), timeout)
        if process.returncode:
            raise InfraError('Mathematical comparison process failed; check QA dependencies and reference metadata')
        result = json.loads(stdout)
        if type(result.get('score')) is not int or result['score'] not in (0, 1):
            raise InfraError('Invalid mathematical comparison result')
        return result['score']
    except asyncio.TimeoutError as error:
        raise InfraError('Mathematical comparison timed out') from error
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


if __name__ == '__main__':
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    cap = min([MEMORY_LIMIT_BYTES] + [value for value in (soft, hard) if value != resource.RLIM_INFINITY])
    resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    from sympy.parsing.latex import parse_latex
    parse_latex('0', strict=True)
    print(json.dumps({'ready': True}), flush=True)
    data = json.load(sys.stdin)
    print(json.dumps({'score': int(compare(data['reference'], data['candidate'], data['parameters']))}))
