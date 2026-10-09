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
    facts = [f for f in extract_numeric_facts(text) if f.kind != "date"]
    if not facts:
        return False
    first = facts[0]
    try:
        value = Decimal(first.value.partition(" ")[0])
    except InvalidOperation:
        return False
    approximate = approximate_before(text, first.start)
    return (states_value(first.raw, value, calculation.result, approximate=approximate)
            or (first.kind == "percent" and states_value(first.raw, value, calculation.result * 100,
                                                          approximate=approximate)))


def final_calculation(content: dict, evidence: dict, question: str) -> "Calculation | None":
    """The calculation that answers the question, recomputed, with a claim citing its evidence that states
    its result: the last one the model listed in "calculations" (only when every listed one checks out: a
    failed final step never falls back on an earlier one), or else the last one a claim writes out itself
    ("Rs 1,20,000 × 50% − Rs 25,000 = Rs 35,000"). None when neither is there."""
    claims = [c for c in content.get("claims") or [] if isinstance(c, dict)]
    if not claims:
        return None
    raw = content.get("calculations")
    checked = checked_calculations(raw, evidence, question)
    candidates = []
    if checked and isinstance(raw, list) and len(checked) == len(raw):
        candidates.append(checked[-1])
    if written := calculations_in(claims, evidence, question):
        candidates.append(written[-1])
    for final in candidates:
        if any(set(final.evidence_ids).issubset(c.get("evidence_ids") or [])
               and states_result(str(c.get("text") or ""), final) for c in claims):
            return final
    return None


# What a calculation question asks for, in its own words: "what is the maximum permissible EMI?", "calculate the
# total interest", "how much total interest will I pay". Never a figure; a few words at most.
_DETERMINER = r"(?:the|my|our|their|his|her|its|your|this|that)"
_ENDS_QUANTITY = (r"(?=\s*(?:\?|$|[,;:.]|\b(?:for|if|under|as\s+per|per|when|in|on|with|after|before|given|using|"
                  r"at|from|over|by|that|which|i|we|you|they|he|she)\b))")
_ASKED_QUANTITY = (
    re.compile(rf"\bwhat\s+(?:is|was|would\s+be|will\s+be|should\s+be)\s+{_DETERMINER}\s+"
               rf"(?P<x>[a-z][a-z-]*(?:\s+[a-z][a-z-]*){{0,6}}?){_ENDS_QUANTITY}", re.I),
    re.compile(rf"\b(?:calculate|compute|determine|work\s+out|find)\s+{_DETERMINER}\s+"
               rf"(?P<x>[a-z][a-z-]*(?:\s+[a-z][a-z-]*){{0,6}}?){_ENDS_QUANTITY}", re.I),
    re.compile(r"\bhow\s+much\s+(?P<x>[a-z][a-z-]*(?:\s+[a-z][a-z-]*){0,4}?)\s+"
               r"(?=(?:will|would|can|could|do|does|did|is|are|should|must|shall|may|might)\b)", re.I),
)
_CURRENCY = re.compile(r"₹|\b(?:rs|inr)\b\.?", re.I)
# A quantity measured in time, a count or a proportion ("remaining tenure", "number of EMIs", "FOIR ratio"): its
# unit (months, %, a fraction) is not the question's currency, so its sentence is left to the model.
_NOT_MONEY = re.compile(r"\b(?:tenure|term|period|months?|years?|weeks?|days?|number|count|ratio|percentage|percent|"
                        r"rate|share|proportion|times)\b", re.I)
# A division in a result sentence's working only by a unit (per cent, months a year): a share or ratio ("35,000 ÷
# 60,000 × 100") has a unit of its own, which the question's words do not give.
_UNIT_DIVISION = re.compile(r"÷\s*(?!(?:100|12|52|365|1,000|1,00,000|1,00,00,000)\b)")


def asked_quantity(question: str) -> str | None:
    """The quantity a calculation question asks for, as it words it ("maximum permissible EMI")."""
    for pattern in _ASKED_QUANTITY:
        if match := pattern.search(question):
            return " ".join(match.group("x").split())
    return None


def restated_result(content: dict, evidence: dict, question: str) -> tuple[dict, "Calculation"] | None:
    """The model chose the calculation, but its sentences never state the recomputed result, or misstate it
    ("0.60 * 200000 - 65000" in "calculations", and "The maximum permissible EMI is Rs 35,000" in a claim): the
    result stated in the question's words with the working ("The maximum permissible EMI is Rs 55,000: 0.60 ×
    2,00,000 − 65,000 = 55,000."), ahead of the model's other claims, less those giving the quantity another
    figure. Only when every listed calculation checks out and the question names the quantity; a share or
    ratio (divided by another figure) is left alone. The method is still for the reasoning check to judge."""
    raw = content.get("calculations")
    checked = checked_calculations(raw, evidence, question)
    if not checked or not isinstance(raw, list) or len(checked) != len(raw):
        return None
    final = checked[-1]
    quantity = asked_quantity(question)
    if not quantity or _NOT_MONEY.search(quantity) or _UNIT_DIVISION.search(final.shown) or final.result < 0:
        return None
    currency = "Rs " if _CURRENCY.search(question) else ""
    claim = {"text": f"The {quantity} is {currency}{indian(final.result)}: {final.shown}.",
             "evidence_ids": list(final.evidence_ids)}
    named = quantity.lower()
    others = [c for c in content.get("claims") or [] if isinstance(c, dict) and not (
        named in str(c.get("text") or "").lower()
        and (figures := figures_in(str(c.get("text") or ""))) and final.result not in figures)]
    return {**content, "claims": [claim, *others]}, final


def has_calculated_answer(content: dict, evidence: dict, question: str) -> bool:
    """The final calculation must be recomputed and stated by a claim citing its evidence.

    Claim order and an optional summary are presentation, not proof of arithmetic.
    """
    return final_calculation(content, evidence, question) is not None


def states_result(text: str, calculation: "Calculation") -> bool:
    """A direct result or a correctly worked equation, never an operand merely mentioned in working."""
    if leads_with_result(text, calculation):
        return True
    for match in _WORKED.finditer(text):
        _number, stated, _percent = _figure(match.group(2))
        approximate = approximate_before(text, match.start(2))
        if (states_value(match.group(2), stated, calculation.result, approximate=approximate)
                and written_arithmetic(match.group(0), set(calculation.values))):
            return True
    if calculation.inputs and calculation.result not in calculation.inputs:
        # "After taking the Rs 65,000 of obligations off 60% of Rs 2,00,000, the maximum EMI is Rs 55,000": the
        # result stated after its working, in words. A figure the calculation starts from is never its result.
        return any(_states(fact, text, calculation.result) for fact in extract_numeric_facts(text)
                   if fact.kind != "date")
    return False


def _states(fact, text: str, result: Decimal) -> bool:
    try:
        value = Decimal(fact.value.partition(" ")[0])
    except InvalidOperation:
        return False
    approximate = approximate_before(text, fact.start)
    return (states_value(fact.raw, value, result, approximate=approximate)
            or (fact.kind == "percent" and states_value(fact.raw, value, result * 100, approximate=approximate)))


@dataclass(frozen=True)
class Calculation:
    result: Decimal
    values: frozenset[Decimal]  # the result, every intermediate result and every figure used
    evidence_ids: tuple[str, ...]
    shown: str  # "1,07,767 × (15 × 12) − 1,00,00,000 = 93,98,060"
    inputs: frozenset[Decimal] = frozenset()  # the figures it starts from (1,07,767, 15, 12, 1,00,00,000)


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


def checked_calculations(raw: object, evidence: dict, question: str,
                         wrong: list[tuple[str, Decimal]] | None = None) -> list[Calculation]:
    """The model's calculations that evaluate, over figures the cited evidence or the question states.
    `evidence` maps evidence ids to objects with a `text`. Anything else is dropped, never repaired.

    An expression may carry its result ("0.60 * 200000 - 65000 = 55000", or a chain "0.6 * 200000 = 120000 -
    65000 = 55000"): the steps are recomputed and a stated result must be the recomputed one. One that is not
    ("... = 65000") is dropped and, with `wrong`, reported as (the right working, the figure stated)."""
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
        steps = [step.strip() for step in _STATED_AS.split(_plain(expression)) if step.strip()]
        values: set[Decimal] = set()
        used: list[Decimal] = []
        tree, result, mismatch = None, None, None
        try:
            for step in steps:
                parsed = ast.parse(step, mode="eval")
                if not isinstance(parsed.body, ast.BinOp):
                    # "= 55000": the result written after the working, which must be the recomputed one.
                    stated = _evaluate_literal(parsed.body)
                    if result is not None and not states_value(step, stated, result):
                        mismatch = stated
                    continue  # copying an input alone is not a calculation
                result = _evaluate(parsed.body, grounded, values, used)
                grounded = grounded | {result}  # the next step of a chain may use it
                tree = parsed
        except (SyntaxError, _Rejected, InvalidOperation, DivisionByZero, ZeroDivisionError, RecursionError):
            continue
        if tree is None or result is None or not used or len(used) > MAX_FIGURES:
            continue
        shown = f"{_show(tree.body)} = {indian(result)}"
        if mismatch is not None:
            if wrong is not None:
                wrong.append((shown, mismatch))
            continue
        checked.append(Calculation(result=result, values=frozenset(values | set(used) | {result}),
                                   evidence_ids=ids, shown=shown, inputs=frozenset(used)))
    return checked


def wrongly_stated(raw: object, evidence: dict, question: str) -> list[tuple[str, Decimal]]:
    """The calculations whose stated result is not the recomputed one: (the right working, the figure stated)."""
    wrong: list[tuple[str, Decimal]] = []
    checked_calculations(raw, evidence, question, wrong)
    return wrong


# "= 55000", "≈ 55000": a result written into an expression.
_STATED_AS = re.compile(r"=|≈")


def _plain(expression: str) -> str:
    """An expression as Python reads it: "0.60 × Rs 2,00,000 − 65,000" -> "0.60 * 200000 - 65000", "60%" ->
    "(60 / 100)", "0.6 x 200000" -> "0.6 * 200000"."""
    text = re.sub(r"₹|\b(?:rs|inr)\b\.?", " ", expression, flags=re.I)
    text = text.replace("×", "*").replace("÷", "/").replace("−", "-").replace("–", "-")
    text = re.sub(r"(?<=[\d)\s])[xX](?=[\s\d(])", "*", text)
    text = _DIGIT_COMMA.sub("", text)
    return re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1 / 100)", text)


def _evaluate_literal(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_evaluate_literal(node.operand)
    raise _Rejected("not a number")


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


@dataclass(frozen=True)
class _Worked:
    """One calculation a claim writes out, recomputed."""
    equation: str  # as written: "Rs. 1,07,767 × 180 months − Rs. 1 crore = Rs. 93,98,060"
    grounded: bool  # every figure is the evidence's, the question's, a unit conversion or an earlier result
    right: bool  # the stated result is the recomputed one, to the precision it is written in
    result: Decimal | None
    values: frozenset[Decimal]  # every figure, step and result
    inputs: frozenset[Decimal] = frozenset()  # the figures it starts from, as written and as meant


# "650 - 699 is 11.15%", "28 - 65 years": a range written with a hyphen, not a subtraction.
_RANGE = re.compile(r"^\s*\(?\s*([\d,.]+)\s*[-–]\s*([\d,.]+)\s*\)?\s*$")


def _worked(text: str, grounded: set[Decimal]) -> list[_Worked]:
    """Every calculation written out in `text`, each step able to use the results of the ones before it
    ("50% × Rs 1,20,000 = Rs 60,000; Rs 60,000 − Rs 25,000 = Rs 35,000")."""
    allowed = grounded | CONSTANTS
    found: list[_Worked] = []
    for match in _WORKED.finditer(text):
        run = re.sub(r"(?:₹|\b(?:rs|inr)\b\.?)", "", match.group(1), flags=re.I)
        if (rng := _RANGE.match(run)) and Decimal(rng.group(1).replace(",", "") or 0) < Decimal(
                rng.group(2).replace(",", "").rstrip(".") or 0):
            continue
        expression, literals, figures, grounded_here = [], set(), set(), True
        try:
            for figure, operator_word, bracket in _WORKED_TOKEN.findall(match.group(1)):
                if figure:
                    number, value, percent = _figure(figure)
                    if number not in allowed and value not in allowed and not (percent and value / 100 in allowed):
                        grounded_here = False
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
        approximate = approximate_before(text, match.start(2))
        raw = match.group(2)
        # "35,000 / 60,000 = 58.33%" or "× 100 = 58.33%": a share written as a percentage.
        right = (states_value(raw, stated, result, approximate=approximate)
                 or (stated_percent and (states_value(raw, stated, result * 100, approximate=approximate)
                                         or near(stated / 100, result))))
        found.append(_Worked(" ".join(match.group(0).split()), grounded_here, right, result,
                             frozenset(figures | literals | values | {result, stated, stated_number}),
                             frozenset(figures | literals)))
        if grounded_here and right:
            allowed |= {result, stated}  # a later step may use this one's result
    return found


def written_arithmetic(text: str, grounded: set[Decimal]) -> set[Decimal]:
    """The figures of each calculation a claim writes out ("A × B − C = D") whose stated result is right and
    whose figures are all grounded (the evidence's, the question's, or a unit conversion): its result and
    every figure and step in it. Empty when there is none, or it is wrong."""
    return set().union(*(w.values for w in _worked(text, grounded) if w.grounded and w.right))


def wrong_arithmetic(text: str, grounded: set[Decimal]) -> list[str]:
    """The calculations a claim writes out over grounded figures whose stated result is not the recomputed
    one ("63,815 × 60 = 63,81,480", "53,883 × 180 = 97,00,940"): a false statement however true each
    figure in it is."""
    return [w.equation for w in _worked(text, grounded) if w.grounded and not w.right]


def calculations_in(claims: list, evidence: dict, question: str) -> list[Calculation]:
    """The calculations the claims write out themselves, right and grounded in the question and the evidence
    each claim cites: the answer's arithmetic when the model put it in its sentences, not in
    "calculations". In claim order, so the last is the final step."""
    asked = figures_in(question)
    found = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        ids = tuple(dict.fromkeys(e for e in claim.get("evidence_ids") or [] if isinstance(e, str) and e in evidence))
        if not ids:
            continue
        grounded = asked | set().union(*(figures_in(evidence[e].text) for e in ids))
        for worked in _worked(str(claim.get("text") or ""), grounded):
            if worked.grounded and worked.right and worked.result is not None:
                found.append(Calculation(result=worked.result, values=worked.values, evidence_ids=ids,
                                         shown=worked.equation,
                                         inputs=worked.inputs))
    return found


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


# "about Rs 93.98 lakh", "approximately 54%", "~Rs 2,425": a figure its writer marks as rounded.
_APPROXIMATE = re.compile(r"(?:\babout|\bapproximately|\bapprox\.?|\broughly|\baround|\bnearly|~)\s*(?:₹|rs\.?|inr)?\s*$",
                          re.I)


def step_of(raw: str) -> Decimal:
    """The smallest difference a figure as written can show: "Rs. 96,98,940" -> 1, "93.98 lakh" -> 1000,
    "58.33%" -> 0.01."""
    match = _FIGURE_PARTS.match(raw.strip())
    if not match:
        return Decimal(1)
    digits = match.group(1).replace(",", "")
    step = Decimal(1).scaleb(-len(digits.partition(".")[2]))
    unit = " ".join(match.group(2).lower().split())
    for word, scale in _SCALE:
        if unit.startswith(word):
            return step * scale
    return step


def states_value(raw: str, value: Decimal, target: Decimal, *, approximate: bool = False) -> bool:
    """A figure written as `raw` (worth `value`) states `target`: to the precision it is written in, give or
    take its last digit's rounding ("Rs. 52,971" for 52,971.2, "93.98 lakh" for 93,98,060), or within 0.5%
    when its writer marks it as approximate. "Rs. 97,00,940" does not state 96,98,940."""
    if approximate:
        return near(value, target)
    return abs(value - target) <= step_of(raw) + Decimal("0.005")


def approximate_before(text: str, start: int) -> bool:
    return bool(_APPROXIMATE.search(text[max(0, start - 20):start]))


def matches(value: Decimal, calculation: Calculation, *, raw: str | None = None, approximate: bool = False) -> bool:
    """A figure a claim states is the calculation's result, or one of its steps: rounded no further than it is
    written ("93.98 lakh"), or within 0.5% when marked approximate. Without `raw`, within 0.5%."""
    if raw is None:
        return any(near(value, v) for v in calculation.values)
    return any(states_value(raw, value, v, approximate=approximate) for v in calculation.values)
