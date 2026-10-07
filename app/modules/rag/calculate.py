"""Arithmetic an answer may state: recomputed here, never taken on trust.

"How much total interest on Rs 1 crore over 15 years?" is answered by arithmetic on figures the documents
and the question state (an EMI of Rs. 1,07,767 for 180 months, less the Rs. 1 crore borrowed), not by a
figure any document prints. The model writes each calculation as a plain expression over those figures;
it is evaluated here, every figure in it must be one the cited evidence or the question states (or a unit
conversion), and only then may a claim state its result. The worked calculation is shown to the reader.
"""

import ast
import operator
import re
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation

from app.modules.citations.numerics import extract_numeric_facts

# Unit conversions a calculation may use besides the stated figures: months, weeks and days in a year,
# per cent, a thousand, a lakh, a crore.
CONSTANTS = frozenset(Decimal(c) for c in ("12", "52", "365", "100", "1000", "100000", "10000000"))
MAX_EXPRESSION_CHARS = 200
MAX_FIGURES = 12
MAX_CALCULATIONS = 6
# A claim may round a result ("about Rs 93.98 lakh" for Rs. 93,98,060).
TOLERANCE = Decimal("0.005")

_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_SYMBOLS = {ast.Add: "+", ast.Sub: "−", ast.Mult: "×", ast.Div: "÷"}
_PRECEDENCE = {ast.Add: 1, ast.Sub: 1, ast.Mult: 2, ast.Div: 2}
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d)")


@dataclass(frozen=True)
class Calculation:
    result: Decimal
    values: frozenset[Decimal]  # the result, every intermediate result and every figure used
    evidence_ids: tuple[str, ...]
    shown: str  # "1,07,767 × (15 × 12) − 1,00,00,000 = 93,98,060"


class _Rejected(Exception):
    pass


def figures_in(text: str) -> set[Decimal]:
    """Every figure a text states, as written ("1,07,767") and as meant ("Rs 1 crore" = 10000000),
    with years also in months."""
    figures = set()
    for match in _NUMBER.findall(text):
        try:
            figures.add(Decimal(match.replace(",", "")))
        except InvalidOperation:
            continue
    for fact in extract_numeric_facts(text):
        number, _, unit = fact.value.partition(" ")
        try:
            value = Decimal(number)
        except InvalidOperation:
            continue
        figures.add(value)
        if fact.kind == "duration" and unit == "year":
            figures.add(value * 12)
    return figures


def checked_calculations(raw: object, evidence: dict, question: str) -> list[Calculation]:
    """The model's calculations that evaluate, over figures the cited evidence or the question states.
    `evidence` maps evidence ids to objects with a `text`. Anything else is dropped, never repaired."""
    if not isinstance(raw, list):
        return []
    asked = figures_in(question)
    checked = []
    for item in raw[:MAX_CALCULATIONS]:
        if not isinstance(item, dict):
            continue
        expression = str(item.get("expression") or "")
        ids = tuple(dict.fromkeys(e for e in item.get("evidence_ids") or [] if isinstance(e, str) and e in evidence))
        if not ids or not expression or len(expression) > MAX_EXPRESSION_CHARS:
            continue
        grounded = asked | CONSTANTS
        for evidence_id in ids:
            grounded |= figures_in(evidence[evidence_id].text)
        try:
            tree = ast.parse(_DIGIT_COMMA.sub("", expression.replace("×", "*").replace("÷", "/")
                                              .replace("−", "-")), mode="eval")
            values: set[Decimal] = set()
            used: list[Decimal] = []
            result = _evaluate(tree.body, grounded, values, used)
        except (SyntaxError, _Rejected, InvalidOperation, DivisionByZero, ZeroDivisionError, RecursionError):
            continue
        if not used or len(used) > MAX_FIGURES:
            continue
        checked.append(Calculation(result=result, values=frozenset(values | set(used) | {result}),
                                   evidence_ids=ids, shown=f"{_show(tree.body)} = {indian(result)}"))
    return checked


def _evaluate(node: ast.AST, grounded: set[Decimal], values: set[Decimal], used: list[Decimal]) -> Decimal:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        value = Decimal(str(node.value))
        if value not in grounded:
            raise _Rejected(f"{value} is not a figure of the evidence or the question")
        used.append(value)
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_evaluate(node.operand, grounded, values, used)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        left = _evaluate(node.left, grounded, values, used)
        right = _evaluate(node.right, grounded, values, used)
        result = _OPERATORS[type(node.op)](left, right)
        values.add(result)
        return result
    raise _Rejected("only + - * / and parentheses")


def _show(node: ast.AST, parent: int = 0, right: bool = False) -> str:
    """The expression as a reader writes it: Indian digit grouping, × ÷ −, only the brackets it needs."""
    if isinstance(node, ast.Constant):
        return indian(Decimal(str(node.value)))
    if isinstance(node, ast.UnaryOp):
        return f"−{_show(node.operand, 3)}"
    level = _PRECEDENCE[type(node.op)]
    text = f"{_show(node.left, level)} {_SYMBOLS[type(node.op)]} {_show(node.right, level, right=True)}"
    # "a − (b − c)", "a ÷ (b × c)": a right-hand operand of equal precedence keeps its brackets.
    return f"({text})" if level < parent or (right and level == parent) else text


def indian(value: Decimal) -> str:
    """12345678.5 -> "1,23,45,678.50"; whole numbers without decimals."""
    value = value.quantize(Decimal("0.01")) if value != value.to_integral_value() else value.to_integral_value()
    sign = "-" if value < 0 else ""
    whole, _, fraction = f"{abs(value):f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    grouped = ",".join(filter(None, [head, *groups, tail]))
    return f"{sign}{grouped}" + (f".{fraction}" if fraction else "")


def near(value: Decimal, target: Decimal) -> bool:
    """`value` is `target`, perhaps rounded."""
    return abs(value - target) <= max(Decimal("0.01"), abs(target) * TOLERANCE)


def matches(value: Decimal, calculation: Calculation) -> bool:
    """A figure a claim states is the calculation's result, or one of its steps, perhaps rounded."""
    return any(near(value, v) for v in calculation.values)
