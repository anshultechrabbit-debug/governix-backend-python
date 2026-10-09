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

# Numerical case questions, independent of policy vocabulary or document-specific aliases.
_QUANTITY_ASK = re.compile(
    r"\b(?:how\s+much|calculate|compute|determine|what\s+(?:is|would\s+be|will\s+be)\s+"
    r"(?:the\s+)?(?:maximum|minimum|total|remaining|net))\b", re.I)


def asks_for_calculation(question: str) -> bool:
    """A quantity requested for supplied inputs, rather than a request to quote a policy limit."""
    inputs = [f for f in extract_numeric_facts(question) if f.kind in ("amount", "percent", "duration", "quantity")]
    return bool(_QUANTITY_ASK.search(question) and (len(inputs) >= 2 or
                (inputs and re.search(r"\bformula\b", question, re.I))))


def leads_with_result(text: str, calculation: "Calculation") -> bool:
    """The direct answer must state the output, not merely repeat an operand in Calculation.values."""
    facts = extract_numeric_facts(text)
    if not facts:
        return False
    try:
        return near(Decimal(facts[0].value.partition(" ")[0]), calculation.result)
    except InvalidOperation:
        return False


def has_calculated_answer(content: dict, evidence: dict, question: str) -> bool:
    """The final calculation must be recomputed and stated by a claim citing its evidence.

    Claim order and an optional summary are presentation, not proof of arithmetic.
    """
    raw = content.get("calculations")
    checked = checked_calculations(raw, evidence, question)
    claims = content.get("claims") or []
    # Never silently use a preceding step when the requested final step failed verification.
    if not checked or len(checked) != len(raw) or not claims:
        return False
    final = checked[-1]
    return any(set(final.evidence_ids).issubset(claim.get("evidence_ids") or [])
               and states_result(str(claim.get("text") or ""), final) for claim in claims)


def states_result(text: str, calculation: "Calculation") -> bool:
    """A direct result or a correctly worked equation, never an operand merely mentioned in working."""
    if leads_with_result(text, calculation):
        return True
    for match in _WORKED.finditer(text):
        _number, stated, _percent = _figure(match.group(2))
        if near(stated, calculation.result) and written_arithmetic(match.group(0), set(calculation.values)):
            return True
    return False


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
        if fact.kind == "percent":
            figures.add(value / 100)  # 50% and 0.5 express the same grounded rate
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
        # A later step may use an earlier exact result only with all its supporting citations.
        # Do not share results between unrelated passages or accept a model's unverified intermediate.
        for previous in checked:
            if set(previous.evidence_ids).issubset(ids):
                grounded.add(previous.result)
        try:
            tree = ast.parse(_DIGIT_COMMA.sub("", expression.replace("×", "*").replace("÷", "/")
                                              .replace("−", "-")), mode="eval")
            if not isinstance(tree.body, ast.BinOp):
                continue  # copying an input is not a calculation
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


# Arithmetic a claim writes out itself, as people write it: "Rs. 1,07,767 × 180 months − Rs. 1 crore = Rs.
# 93,98,060", "(₹53,883 × 180) − (₹1,06,358 × 60) ≈ ₹33.17 lakh", "50% of income: 50% × Rs. 60,000 − Rs.
# 10,000 = Rs. 20,000", "₹35,000 / ₹60,000 × 100 = 58.33%". A figure keeps its currency mark, unit and scale
# ("lakh", "crore", "k"); "N%" is N / 100; operators may be words ("minus", "times", "divided by").
_FIGURE = (r"(?:₹|(?:rs|inr)\b\.?)?\s*\d[\d,]*(?:\.\d+)?"
           r"(?:\s*(?:%|per\s*cent\b|lakhs?\b|lacs?\b|crores?\b|cr\b|thousand\b|k\b|months?\b|years?\b|yrs?\b|"
           r"weeks?\b|days?\b))?")
_OPERATOR_WORD = r"(?:[×x*/÷+\-−–]|plus\b|minus\b|less\b|times\b|multiplied\s+by\b|divided\s+by\b)"
_WORKED_RUN = rf"\(*\s*{_FIGURE}\s*\)*(?:\s*{_OPERATOR_WORD}\s*\(*\s*{_FIGURE}\s*\)*)+"
_WORKED = re.compile(
    rf"({_WORKED_RUN})\s*(?:=|≈|equals\b|is\b|comes\s+to\b|gives\b)\s*"
    rf"(?:about\s+|approximately\s+|approx\.?\s+|roughly\s+|around\s+|~\s*)?({_FIGURE})", re.I)
_WORKED_TOKEN = re.compile(rf"({_FIGURE})|({_OPERATOR_WORD})|([()])", re.I)
_FIGURE_PARTS = re.compile(r"(?:₹|(?:rs|inr)\.?)?\s*(\d[\d,]*(?:\.\d+)?)\s*(.*)", re.I)
_SCALE = (("crore", 10**7), ("cr", 10**7), ("lakh", 10**5), ("lac", 10**5), ("thousand", 1000), ("k", 1000))
_AS_OPERATOR = {"×": "*", "x": "*", "*": "*", "times": "*", "multiplied by": "*", "/": "/", "÷": "/",
                "divided by": "/", "+": "+", "plus": "+", "-": "-", "−": "-", "–": "-", "minus": "-", "less": "-"}


def _figure(raw: str) -> tuple[Decimal, Decimal, bool]:
    """(the number as written, its value, whether it is a percentage): "Rs. 1 crore" -> (1, 10000000, False),
    "50%" -> (50, 50, True), "180 months" -> (180, 180, False)."""
    match = _FIGURE_PARTS.match(raw.strip())
    number = Decimal(match.group(1).replace(",", ""))
    unit = " ".join(match.group(2).lower().split())
    if unit.startswith(("%", "per")):
        return number, number, True
    for word, scale in _SCALE:
        if unit.startswith(word):
            return number, number * scale, False
    return number, number, False


def written_arithmetic(text: str, grounded: set[Decimal]) -> set[Decimal]:
    """The figures of each calculation a claim writes out ("A × B − C = D") whose stated result is right and
    whose figures are all grounded (the evidence's, the question's, or a unit conversion): its result and
    every figure and step in it. Empty when there is none, or it is wrong."""
    allowed = grounded | CONSTANTS
    accepted: set[Decimal] = set()
    for match in _WORKED.finditer(text):
        expression, literals, figures = [], set(), set()
        try:
            for figure, operator_word, bracket in _WORKED_TOKEN.findall(match.group(1)):
                if figure:
                    number, value, percent = _figure(figure)
                    if number not in allowed and value not in allowed and not (percent and value / 100 in allowed):
                        raise _Rejected(f"{figure} is not a figure of the evidence or the question")
                    literal = value / 100 if percent else value
                    expression.append(f"({literal})")
                    literals.add(literal)
                    figures |= {number, value}
                elif operator_word:
                    expression.append(_AS_OPERATOR[" ".join(operator_word.lower().split())])
                else:
                    expression.append(bracket)
            if len(expression) > MAX_EXPRESSION_CHARS:
                continue
            stated_number, stated, stated_percent = _figure(match.group(2))
            values: set[Decimal] = set()
            result = _evaluate(ast.parse(" ".join(expression), mode="eval").body, literals, values, [])
        except (SyntaxError, KeyError, _Rejected, InvalidOperation, DivisionByZero, ZeroDivisionError, RecursionError):
            continue
        # "35,000 / 60,000 = 58.33%" or "× 100 = 58.33%": a share written as a percentage.
        if near(stated, result) or (stated_percent and (near(stated, result * 100) or near(stated / 100, result))):
            accepted |= figures | literals | values | {result, stated, stated_number}
    return accepted


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
