"""Reworded queries for documents whose vectors are still being written.

A large document becomes searchable as soon as it is chunked; its embeddings follow at
the pace of the provider's quota (see ingestion.pipeline). Until they are written, only
the keyword lanes reach its chunks, and those miss a question worded differently from
the document ("how long do we keep records" vs "retention period").

For those documents only, the model rewords the question in the terms a policy document
would use, and each rewording runs the keyword lanes over them. The rewordings only
steer retrieval: every passage still comes from the documents, and the answer is
checked against them as usual. Once the vectors are written, this is never called.
"""

import logging
from collections.abc import Callable

from app.infrastructure.ai.llm.base import LLMProvider

logger = logging.getLogger(__name__)

MAX_REPHRASINGS = 3

PROMPT = """You help find the answer to a question in bank policy documents with a keyword search.

Write up to three short search queries that ask the same thing as the question in other words: the formal
terms, synonyms and abbreviations a policy document would use for it.
Example: "how long do we keep customer records?" -> "record retention period", "preservation of customer
records", "KYC documents retention".

- Do not answer the question.
- Do not add numbers, names, dates or facts that are not in the question.
- Keep each query under twelve words."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}


def rephrasings(llm: LLMProvider | None, question: str) -> list[str]:
    """Up to MAX_REPHRASINGS rewordings; none when the model is unavailable or fails."""
    if llm is None:
        return []
    try:
        result = llm.generate_json(PROMPT, f"Question: {question}", SCHEMA)
    except Exception:  # noqa: BLE001 - rewordings only add recall; the search goes on without them
        logger.warning("Could not reword the question for documents still being embedded", exc_info=True)
        return []
    raw = (result.content or {}).get("queries")
    asked = " ".join(question.lower().split()).strip("?. ")
    queries = [" ".join(str(q).split())[:300] for q in raw if str(q).strip()] if isinstance(raw, list) else []
    return [q for q in dict.fromkeys(queries) if q.lower().strip("?. ") != asked][:MAX_REPHRASINGS]


def rephraser(llm_factory: Callable[[], LLMProvider] | None) -> Callable[[str], list[str]] | None:
    """A rephrase function for retrieve_with_variants, built lazily from the LLM factory."""
    if llm_factory is None:
        return None
    asked: dict[str, list[str]] = {}  # an answer may retrieve more than once for one question

    def rephrase(question: str) -> list[str]:
        if question not in asked:
            try:
                llm = llm_factory()
            except Exception:  # noqa: BLE001 - no model configured: no rewordings
                return []
            asked[question] = rephrasings(llm, question)
        return asked[question]

    return rephrase
