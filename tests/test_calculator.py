"""Tests for tools/calculator.py — safe AST evaluator."""

import math
import pytest
from tools.calculator import calculate


@pytest.mark.parametrize("expr, expected", [
    ("2+2", "4"),
    ("10 - 3", "7"),
    ("6 * 7", "42"),
    ("10 / 4", "2.5"),
    ("10 / 2", "5"),              # whole floats are shown as ints
    ("2 ** 10", "1024"),
    ("17 % 5", "2"),
    ("7 // 2", "3"),
    ("-5 + 3", "-2"),
    ("+4", "4"),
    ("(2 + 3) * 4", "20"),
    ("sqrt(144)", "12"),
    ("factorial(5)", "120"),
    ("abs(-3)", "3"),
    ("min(4, 2, 9)", "2"),
    ("max(4, 2, 9)", "9"),
    ("round(3.14159, 2)", "3.14"),
    ("  1 + 1  ", "2"),           # surrounding whitespace
])
def test_valid_expressions(expr, expected):
    assert calculate(expr) == expected


def test_constants():
    assert calculate("pi") == str(math.pi)
    assert calculate("2 * pi") == str(2 * math.pi)


@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo hi')",   # attribute call
    "__import__('os')",                     # name not whitelisted
    "open('secrets.txt')",
    "print(1)",
    "eval('1+1')",
    "exec('x=1')",
    "'a' * 5",                              # non-numeric constant
    "'abc'",
    "x + 1",                                # unknown name
    "[1, 2, 3]",                            # unsupported node
    "(1).__class__",                        # attribute access
    "lambda: 1",
    "1 if True else 2",
    "1 << 3",                               # operator not in whitelist
    "~5",
    "",                                     # syntax error
    "2 +",                                  # syntax error
    "1 / 0",                                # runtime error
])
def test_rejected_expressions_return_error(expr):
    assert calculate(expr).startswith("Error")


def test_error_never_raises():
    # calculate() must always return a string, never raise
    for bad in ("((", "1/0", "sqrt(-1)", "log(0)", "factorial(-1)"):
        assert isinstance(calculate(bad), str)
