"""Deterministic, evidence-safe query variants and entity hints.

No LLM expands a query: variants only normalise known banking terminology and
are disabled for identifiers, clauses, dates and explicit version requests.
"""

import re

from app.modules.search.retrieval import query_terms

_IDENTIFIER = re.compile(r"\b[A-Za-z]{1,10}[-/]\w+|\b\d+(?:\.\d+){1,4}\b|\b(?:v|version)\s*\d+\b", re.I)
_SYNONYMS = {
    "probation": "probationary employment",
    "housing": "home loan",
    "house": "home loan",
    "ltv": "loan to value",
    "salary": "income",
    "maximum": "limit",
    "interest": "interest rate",
}


def query_variants(query: str, *, limit: int = 3) -> list[str]:
    """Return bounded lexical variants without adding any factual content."""
    normal = " ".join(query.split())
    if _IDENTIFIER.search(normal) or len(query_terms(normal)) < 3:
        return [normal]
    variants = [normal]
    words = normal.split()
    for source, replacement in _SYNONYMS.items():
        changed = [replacement if word.strip(".,?!:;()[]").lower() == source else word for word in words]
        candidate = " ".join(changed)
        if candidate != normal and candidate not in variants:
            variants.append(candidate)
        if len(variants) == limit:
            break
    return variants
