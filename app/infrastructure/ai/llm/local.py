import re
import time
from typing import Any

from app.infrastructure.ai.llm.base import LLMProvider, LLMResult
from app.modules.ingestion.text import split_sentences

_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?%?")
_STOP = frozenset(
    "a an and are as at be by can do does for from has have how i in is it of on or shall should "
    "that the their this to was what when where which who will with".split()
)


class LocalLLM(LLMProvider):
    """Deterministic EXTRACTIVE answerer: quotes the evidence sentences that best
    match the question, each cited. It never writes new facts, so it is safe
    for development and tests without an API key, but it does not paraphrase."""

    model_id = "local-extractive-v1"
    MAX_CLAIMS = 3

    def generate_json(self, system: str, user: str, schema: dict[str, Any], *, context=None) -> LLMResult:
        started = time.perf_counter()
        context = context or {}
        if context.get("task") == "summary":
            return self._result(_extractive_summary(context.get("sections", [])), started)
        if context.get("task") == "name":
            # No model to read the document: the title found from the layout stands.
            return self._result({"name": context.get("detected"), "about": None}, started)
        if context.get("task") == "change_summary":
            lines = context.get("lines", [])[:5]
            return self._result({"summary": ("Important changes: " + "; ".join(lines) + ".") if lines else ""}, started)
        terms = {w for w in _WORD.findall(context.get("question", "").lower()) if w not in _STOP}
        scored = []
        for order, item in enumerate(context.get("evidence", [])):
            for position, sentence in enumerate(split_sentences(item["text"])):
                words = set(_WORD.findall(sentence.lower()))
                overlap = len(terms & words)
                if overlap:
                    scored.append((overlap, -order, -position, sentence.strip(), item["id"]))
        scored.sort(reverse=True)
        claims, seen = [], set()
        for _overlap, _o, _p, sentence, evidence_id in scored:
            if sentence in seen:
                continue
            seen.add(sentence)
            claims.append({"text": sentence, "evidence_ids": [evidence_id]})
            if len(claims) == self.MAX_CLAIMS:
                break
        content = {
            "claims": claims,
            "insufficient_evidence": not claims,
            "conflicts": [
                {"description": c["description"], "evidence_ids": c["evidence_ids"]}
                for c in context.get("conflicts", [])
            ],
        }
        return self._result(content, started)

    def _result(self, content: dict[str, Any], started: float) -> LLMResult:
        return LLMResult(content=content, model=self.model_id, latency_ms=round((time.perf_counter() - started) * 1000, 1))


_RULE = re.compile(r"\b(shall|must|not exceed|minimum|maximum|required|mandatory|at least|up to)\b|\d", re.I)
_ACTION = re.compile(r"\b(must|shall submit|shall obtain|shall ensure|required to|should|apply|report)\b", re.I)
_EXCEPTION = re.compile(r"\b(except|unless|exempt|does not apply|shall not apply|excluding)\b", re.I)
_PURPOSE_TITLE = re.compile(r"\b(purpose|objective|scope|introduction)\b", re.I)


def _extractive_summary(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """Quote the document's own sentences, so a summary without an API key never invents anything."""
    quoted = [
        (section.get("number"), section.get("title") or "", sentence.strip())
        for section in sections
        for sentence in split_sentences(section.get("text") or "")
        if len(sentence.split()) >= 5
    ]

    def pick(pattern: re.Pattern[str], limit: int) -> list[dict[str, Any]]:
        return [{"text": s, "section": n} for n, _t, s in quoted if pattern.search(s)][:limit]

    purpose = next((s for _n, title, s in quoted if _PURPOSE_TITLE.search(title)), None)
    return {
        "overview": " ".join(s for _n, _t, s in quoted[:2]),
        "purpose": purpose,
        "key_rules": pick(_RULE, 8),
        "user_actions": pick(_ACTION, 5),
        "exceptions": pick(_EXCEPTION, 5),
        "applicable_to": None,
    }
