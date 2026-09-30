"""Keyword-evidence category classification (deterministic, explainable).

Each category contributes keywords (configurable per organisation). Evidence in
the title counts most, then the first page, then the opening pages. The
confidence reflects both how dominant the winner is and how much evidence
there is; weak evidence never produces a high confidence.
"""

import re
import uuid
from dataclasses import dataclass

TITLE_WEIGHT = 5.0
FIRST_PAGE_WEIGHT = 2.0
BODY_WEIGHT = 0.5
BODY_CAP = 6
STRONG_EVIDENCE = 8.0


@dataclass(frozen=True)
class CategoryProfile:
    id: uuid.UUID
    name: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class CategoryScore:
    category_id: uuid.UUID
    name: str
    score: float
    matched: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    category_id: uuid.UUID | None
    name: str | None
    confidence: float
    ranking: list[CategoryScore]


def _pattern(keyword: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])")


def classify(
    categories: list[CategoryProfile], title: str | None, first_page: str, opening_text: str
) -> Classification:
    title_l, first_l, body_l = (title or "").lower(), first_page.lower(), opening_text.lower()
    ranking: list[CategoryScore] = []
    for category in categories:
        keywords = set(category.keywords) | {category.name.lower(), category.name.lower().rstrip("s")}
        score, matched = 0.0, []
        for keyword in keywords:
            if len(keyword) < 2:
                continue
            pattern = _pattern(keyword)
            hits_title = len(pattern.findall(title_l))
            hits_first = len(pattern.findall(first_l))
            hits_body = min(len(pattern.findall(body_l)), BODY_CAP)
            keyword_score = hits_title * TITLE_WEIGHT + min(hits_first, 3) * FIRST_PAGE_WEIGHT + hits_body * BODY_WEIGHT
            if keyword_score:
                score += keyword_score
                matched.append(keyword)
        ranking.append(CategoryScore(category.id, category.name, round(score, 2), tuple(sorted(matched))))
    ranking.sort(key=lambda s: s.score, reverse=True)

    if not ranking or ranking[0].score == 0:
        return Classification(None, None, 0.0, ranking[:3])
    best = ranking[0]
    runner_up = ranking[1].score if len(ranking) > 1 else 0.0
    dominance = best.score / (best.score + runner_up)
    strength = min(best.score / STRONG_EVIDENCE, 1.0)
    confidence = round(min(0.99, dominance * (0.5 + 0.5 * strength)), 2)
    return Classification(best.category_id, best.name, confidence, ranking[:3])
