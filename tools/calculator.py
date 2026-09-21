import ast
import math
import operator

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_NAMES = {k: v for k, v in math.__dict__.items() if not k.startswith("_")}
_NAMES.update({"abs": abs, "round": round, "min": min, "max": max})


def _eval(node):
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, complex)):
            raise ValueError("Only numeric constants allowed")
        return node.value
    if isinstance(node, ast.BinOp):
        fn = _OPS.get(type(node.op))
        if not fn:
            raise ValueError(f"Operator not allowed: {type(node.op).__name__}")
        return fn(_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp):
        fn = _OPS.get(type(node.op))
        if not fn:
            raise ValueError(f"Operator not allowed: {type(node.op).__name__}")
        return fn(_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _NAMES:
            raise ValueError(f"Function not allowed: {getattr(node.func, 'id', '?')}")
        return _NAMES[node.func.id](*[_eval(a) for a in node.args])
    if isinstance(node, ast.Name):
        if node.id not in _NAMES:
            raise ValueError(f"Name not allowed: {node.id}")
        return _NAMES[node.id]
    raise ValueError(f"Unsupported expression type: {type(node).__name__}")


def calculate(expression: str) -> str:
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _eval(tree.body)
        if isinstance(result, float) and result == int(result):
            result = int(result)
        return str(result)
    except Exception as e:
        return f"Error: {e}"


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": (
                "Evaluate a mathematical expression safely. Supports arithmetic, "
                "exponentiation, modulo, and math functions like sqrt, sin, cos, "
                "log, log10, factorial, ceil, floor, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Math expression to evaluate, e.g. 'sqrt(144)' or '2**32'",
                    },
                },
                "required": ["expression"],
            },
        },
    },
]
