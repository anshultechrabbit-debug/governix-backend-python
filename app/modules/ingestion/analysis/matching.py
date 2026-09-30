"""Existing-policy matching, duplicate and version analysis: the upload DECISION.

Pure functions over plain data so every rule is unit-testable. The score is a
weighted sum of independent signals; each signal is reported back so a
reviewer can see exactly why the system suggested what it did.
"""

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date

from app.modules.ingestion.model import Decision
from app.modules.ingestion.text import hamming_distance

WEIGHTS = {
    "policy_number": 0.40,
    "policy_number_mismatch": -0.35,
    "document_number": 0.15,
    "title": 0.25,
    "issuer": 0.05,
    "category": 0.05,
    "content": 0.10,
    "structure": 0.10,
}


@dataclass
class VersionFacts:
    id: uuid.UUID
    label: str
    revision: int
    effective_from: date
    effective_to: date | None
    content_hash: str | None
    document_id: uuid.UUID


@dataclass
class CandidateFacts:
    policy_id: uuid.UUID
    name: str
    normalized_name: str
    policy_number: str | None
    document_number: str | None
    issuer: str | None
    issuing_department: str | None
    category_id: uuid.UUID
    title_similarity: float  # pg_trgm similarity of normalized names, 0..1
    versions: list[VersionFacts] = field(default_factory=list)
    latest_simhash: int | None = None
    latest_section_keys: frozenset[str] = frozenset()

    @property
    def latest(self) -> VersionFacts | None:
        return max(self.versions, key=lambda v: v.effective_from) if self.versions else None


@dataclass
class UploadFacts:
    normalized_title: str
    policy_number: str | None
    document_number: str | None
    issuer: str | None
    department: str | None
    category_id: uuid.UUID | None
    effective_date: date | None
    version_label: str | None
    revision: int | None
    content_hash: str | None
    simhash: int | None
    section_keys: frozenset[str]


@dataclass
class Signal:
    signal: str
    matched: bool
    weight: float
    detail: str


@dataclass
class ScoredCandidate:
    candidate: CandidateFacts
    score: float
    signals: list[Signal]

    def summary(self) -> dict:
        latest = self.candidate.latest
        return {
            "policy_id": str(self.candidate.policy_id),
            "name": self.candidate.name,
            "policy_number": self.candidate.policy_number,
            "score": self.score,
            "latest_version": _version_json(latest) if latest else None,
            "signals": [asdict(s) for s in self.signals],
        }


@dataclass
class DecisionResult:
    decision: Decision
    confidence: float
    best: ScoredCandidate | None
    conflict: dict | None = None
    notes: list[str] = field(default_factory=list)


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def content_similarity(a: int | None, b: int | None) -> float | None:
    if a is None or b is None:
        return None
    return max(0.0, 1.0 - hamming_distance(a, b) / 32)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float | None:
    if not a or not b:
        return None
    return len(a & b) / len(a | b)


def score_candidate(upload: UploadFacts, candidate: CandidateFacts) -> ScoredCandidate:
    signals: list[Signal] = []
    score = 0.0

    def add(name: str, matched: bool, weight: float, detail: str) -> None:
        nonlocal score
        score += weight
        signals.append(Signal(name, matched, round(weight, 3), detail))

    if upload.policy_number and candidate.policy_number:
        if _norm(upload.policy_number) == _norm(candidate.policy_number):
            add("policy_number", True, WEIGHTS["policy_number"], f"Policy number {candidate.policy_number} matches")
        else:
            add("policy_number", False, WEIGHTS["policy_number_mismatch"],
                f"Policy number differs ({upload.policy_number} vs {candidate.policy_number})")
    if upload.document_number and candidate.document_number and _norm(upload.document_number) == _norm(candidate.document_number):
        add("document_number", True, WEIGHTS["document_number"], "Document number matches")

    title_sim = 1.0 if upload.normalized_title and upload.normalized_title == candidate.normalized_name else candidate.title_similarity
    add("title", title_sim >= 0.6, WEIGHTS["title"] * title_sim, f"Title similarity {title_sim:.0%}")

    issuer_values = {_norm(candidate.issuer), _norm(candidate.issuing_department)} - {""}
    upload_issuer = {_norm(upload.issuer), _norm(upload.department)} - {""}
    if issuer_values and upload_issuer:
        matched = bool(issuer_values & upload_issuer)
        add("issuer", matched, WEIGHTS["issuer"] if matched else 0.0,
            "Issuing department matches" if matched else "Issuing department differs")
    if upload.category_id:
        matched = upload.category_id == candidate.category_id
        add("category", matched, WEIGHTS["category"] if matched else 0.0,
            "Same category" if matched else "Different category")

    if (sim := content_similarity(upload.simhash, candidate.latest_simhash)) is not None:
        add("content", sim >= 0.6, WEIGHTS["content"] * sim, f"Content similarity to latest version {sim:.0%}")
    if (sim := jaccard(upload.section_keys, candidate.latest_section_keys)) is not None:
        add("structure", sim >= 0.5, WEIGHTS["structure"] * sim, f"Section structure overlap {sim:.0%}")

    return ScoredCandidate(candidate, round(max(0.0, min(score, 0.99)), 3), signals)


def _label_key(label: str | None) -> tuple:
    try:
        return tuple(int(p) for p in (label or "").split("."))
    except ValueError:
        return ()


def analyze_version(upload: UploadFacts, candidate: CandidateFacts) -> tuple[Decision, dict | None, list[str]]:
    """Validate the detected version against the policy's history. Never trust the label alone."""
    notes: list[str] = []
    versions = candidate.versions
    if upload.content_hash and any(v.content_hash == upload.content_hash for v in versions):
        same = next(v for v in versions if v.content_hash == upload.content_hash)
        return Decision.CONTENT_DUPLICATE, {"type": "SAME_CONTENT_AS_VERSION", "existing_version": _version_json(same)}, notes

    if upload.version_label:
        same_label = [
            v for v in versions
            if v.label == upload.version_label and (upload.revision is None or v.revision == upload.revision)
        ]
        if same_label:
            return Decision.VERSION_CONFLICT, {
                "type": "VERSION_LABEL_EXISTS",
                "message": f"Version {upload.version_label} already exists with different content.",
                "existing_version": _version_json(same_label[0]),
                "uploaded_version_label": upload.version_label,
            }, notes

    if upload.effective_date:
        same_date = [v for v in versions if v.effective_from == upload.effective_date]
        if same_date:
            return Decision.VERSION_CONFLICT, {
                "type": "EFFECTIVE_DATE_EXISTS",
                "message": f"Another version is already effective from {upload.effective_date.isoformat()}.",
                "existing_version": _version_json(same_date[0]),
            }, notes

    latest = candidate.latest
    if latest and upload.effective_date and upload.version_label:
        newer_label = _label_key(upload.version_label) > _label_key(latest.label) if _label_key(latest.label) else None
        later_date = upload.effective_date > latest.effective_from
        if newer_label is not None and newer_label != later_date:
            return Decision.VERSION_CONFLICT, {
                "type": "VERSION_ORDER_MISMATCH",
                "message": (
                    f"Version {upload.version_label} is effective {upload.effective_date.isoformat()}, "
                    f"but the latest version {latest.label} is effective {latest.effective_from.isoformat()}."
                ),
                "existing_version": _version_json(latest),
                "uploaded_version_label": upload.version_label,
            }, notes
    if latest and upload.effective_date and upload.effective_date < latest.effective_from:
        notes.append("HISTORICAL_VERSION")
    if not upload.effective_date:
        notes.append("EFFECTIVE_DATE_MISSING")
    return Decision.EXISTING_POLICY_NEW_VERSION, None, notes


def decide(
    upload: UploadFacts,
    candidates: list[CandidateFacts],
    *,
    high: float,
    low: float,
    has_amendment_targets: bool,
) -> tuple[DecisionResult, list[ScoredCandidate]]:
    scored = sorted((score_candidate(upload, c) for c in candidates), key=lambda s: s.score, reverse=True)
    best = scored[0] if scored else None
    number_match = bool(best and any(s.signal == "policy_number" and s.matched for s in best.signals))

    if has_amendment_targets and not number_match:
        return DecisionResult(Decision.EXISTING_POLICY_AMENDMENT, 0.8, best), scored
    if best is None or best.score < low:
        confidence = round(1 - (best.score if best else 0.0), 2)
        return DecisionResult(Decision.NEW_POLICY, confidence, best), scored
    if best.score < high:
        return DecisionResult(Decision.POSSIBLE_MATCH_REQUIRES_REVIEW, best.score, best), scored

    decision, conflict, notes = analyze_version(upload, best.candidate)
    return DecisionResult(decision, best.score, best, conflict, notes), scored


def _version_json(version: VersionFacts) -> dict:
    return {
        "id": str(version.id),
        "label": version.label,
        "revision": version.revision,
        "effective_from": version.effective_from.isoformat(),
        "effective_to": version.effective_to.isoformat() if version.effective_to else None,
        "document_id": str(version.document_id),
    }
