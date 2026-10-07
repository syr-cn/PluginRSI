"""Bounded numeric expressions for the QA tool environment; no Python eval."""

import ast
import asyncio
import json
import math
import operator
import sys

MAX_INPUT_CHARS = 2048
MAX_NODES = 128
MAX_MAGNITUDE = 1e100
MAX_EXPONENT = 1000
MAX_TIMEOUT_SECONDS = 5
MAX_OUTPUT_CHARS = 4096
MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
CONSTANTS = {'pi': math.pi, 'e': math.e, 'tau': math.tau}
FUNCTIONS = {name: getattr(math, name) for name in ('sqrt', 'log', 'log10', 'exp', 'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'floor', 'ceil')}
FUNCTIONS.update(abs=abs, round=round, min=min, max=max)
BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}


def calculate(expression):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > MAX_INPUT_CHARS:
        raise ValueError('Provide a nonempty numeric expression of at most 2048 characters')
    tree = ast.parse(expression, mode='eval')
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise ValueError('Expression is too complex')

    def bounded(value):
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > MAX_MAGNITUDE:
            raise ValueError('Result must be a finite real number with magnitude at most 1e100')
        return value

    def visit(node):
        if isinstance(node, ast.Constant):
            return bounded(node.value)
        if isinstance(node, ast.Name) and node.id in CONSTANTS:
            return CONSTANTS[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > MAX_EXPONENT or (left != 0 and right * math.log10(abs(left)) > math.log10(MAX_MAGNITUDE)):
                    raise ValueError('Power exceeds calculator limits')
                return bounded(left ** right)
            if type(node.op) in BINARY:
                return bounded(BINARY[type(node.op)](left, right))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords:
            return bounded(FUNCTIONS[node.func.id](*(visit(arg) for arg in node.args)))
        raise ValueError('Only numeric operators, supported math functions and pi/e/tau are allowed')

    return visit(tree.body)


async def run_calculation(expression, timeout=MAX_TIMEOUT_SECONDS):
    from ..contracts import ToolResult
    from ..runtime import InfraError

    if not isinstance(expression, str) or len(expression) > MAX_INPUT_CHARS:
        return ToolResult('Invalid expression length or type', True)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Calculator timeout must be finite and positive')
    process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-S', __file__,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps({'expression': expression}).encode()),
                                                min(timeout, MAX_TIMEOUT_SECONDS))
        if process.returncode:
            raise InfraError('Calculator process failed')
        result = json.loads(stdout)
        return ToolResult(json.dumps(result, ensure_ascii=False)[:MAX_OUTPUT_CHARS], 'error' in result)
    except asyncio.TimeoutError:
        return ToolResult('Calculator time limit exceeded', True)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


if __name__ == '__main__':
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    cap = min([MEMORY_LIMIT_BYTES] + [value for value in (soft, hard) if value != resource.RLIM_INFINITY])
    resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    request = json.load(sys.stdin)
    try:
        result = {'result': calculate(request['expression'])}
    except (ValueError, TypeError, SyntaxError, ArithmeticError, RecursionError) as error:
        result = {'error': str(error)}
    print(json.dumps(result, allow_nan=False))
