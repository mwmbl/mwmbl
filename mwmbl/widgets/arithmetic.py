"""Arithmetic typed into the search box, computed in place.

The query is user input from anyone on the internet, so it is never evaluated: it is parsed to an AST and walked
with an allowlist of node types. The arithmetic is exact (`Fraction`), and every value and every power is bounded
*before* it is computed, so `9**9**9` or `10**1000 * 10**1000` costs microseconds, not memory.
"""

import ast
import operator
import re
from decimal import Context, Decimal
from fractions import Fraction

MAX_QUERY_LENGTH = 100
MAX_EXPONENT = 1000
# No intermediate value may have a numerator or denominator wider than this (about 10^1233).
MAX_BITS = 4096
MAX_RESULT = 10**100
# Up to this size an integer is shown in full; beyond it, in scientific notation.
MAX_PLAIN = 10**15
MIN_PLAIN = Fraction(1, 10**6)
SIGNIFICANT_DIGITS = 12

# Rules out nearly every ordinary query without running a parser.
ARITHMETIC_PATTERN = re.compile(r"[\d\s.+\-*/^%()]+")
OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
DISPLAY_CONTEXT = Context(prec=SIGNIFICANT_DIGITS)


class Unsupported(Exception):
    """The expression is not arithmetic we are willing to compute."""


def calculate(query: str) -> str | None:
    """The result of the arithmetic in `query`, formatted for display, or None if it is not (safe) arithmetic."""
    if len(query) > MAX_QUERY_LENGTH or not ARITHMETIC_PATTERN.fullmatch(query):
        return None
    source = query.strip().replace("^", "**")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError:
        return None
    # A bare number or `-5` is not a calculation.
    if not any(isinstance(node, ast.BinOp) for node in ast.walk(tree)):
        return None
    try:
        value = _evaluate(tree.body, source)
    except (Unsupported, ZeroDivisionError):
        return None
    if abs(value) > MAX_RESULT:
        return None
    return format_fraction(value)


def _check_size(value: Fraction) -> Fraction:
    if max(abs(value.numerator).bit_length(), value.denominator.bit_length()) > MAX_BITS:
        raise Unsupported("too large")
    return value


def _evaluate(node: ast.expr, source: str) -> Fraction:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        # The literal's own text, because the float it parsed to is already inexact (0.1).
        literal = ast.get_source_segment(source, node)
        return _check_size(Fraction(literal))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        operand = _evaluate(node.operand, source)
        return -operand if isinstance(node.op, ast.USub) else operand
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, source)
        right = _evaluate(node.right, source)
        if isinstance(node.op, ast.Pow):
            return _power(left, right)
        if isinstance(node.op, ast.Mod):
            return _check_size(left % right)
        return _check_size(OPERATORS[type(node.op)](left, right))
    raise Unsupported(type(node).__name__)


def _power(base: Fraction, exponent: Fraction) -> Fraction:
    if abs(exponent) > MAX_EXPONENT:
        raise Unsupported("exponent too large")
    if exponent.denominator != 1:
        # A root: not exact, so go through a float, which cannot be made huge by a bounded base and exponent.
        if base < 0 or (base == 0 and exponent < 0):
            raise Unsupported("no real result")
        base_bits = max(abs(base.numerator).bit_length(), base.denominator.bit_length())
        if base_bits * abs(exponent) > MAX_BITS:
            raise Unsupported("too large")
        try:
            return _check_size(Fraction(float(base) ** float(exponent)))
        except OverflowError:
            raise Unsupported("too large")
    power = int(exponent)
    base_bits = max(abs(base.numerator).bit_length(), base.denominator.bit_length())
    if base_bits * abs(power) > MAX_BITS:
        raise Unsupported("too large")
    return _check_size(base**power)


def format_fraction(value: Fraction) -> str:
    """Plain digits for everyday magnitudes, scientific notation beyond them; 12 significant digits at most."""
    if value.denominator == 1 and abs(value) < MAX_PLAIN:
        return str(value.numerator)
    rounded = DISPLAY_CONTEXT.divide(Decimal(value.numerator), Decimal(value.denominator)).normalize(DISPLAY_CONTEXT)
    if value != 0 and not MIN_PLAIN <= abs(value) < MAX_PLAIN:
        return format(rounded, "e")
    return format(rounded, "f")
