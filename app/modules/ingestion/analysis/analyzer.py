"""Builds the upload analysis from the database: detections, duplicates, matches, decision."""

import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import BIT, insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.infrastructure.ai.llm.base import LLMProvider
from app.modules.categories.model import Category
from app.modules.documents.model import Document, DocumentPage, DocumentSection
from app.modules.documents.repository import ACTIVE_STATUSES
from app.modules.ingestion.analysis.classify import CategoryProfile, classify
from app.modules.ingestion.analysis.matching import (
    CandidateFacts,
    UploadFacts,
    VersionFacts,
    decide,
)
from app.modules.ingestion.analysis.metadata import (
    DocumentMetadata,
    extract_metadata,
    fallback_title,
    normalize_title,
)
from app.modules.ingestion.analysis.naming import NAMING_PAGES, content_title, read_content
from app.modules.ingestion.model import Decision, DocumentAnalysis, ReviewStatus
from app.modules.policies.model import Policy, PolicyStatus, PolicyVersion, VersionStatus

# The general category a document goes to when nothing identifies a more specific one.
DEFAULT_CATEGORY_SLUG = "policies"

OPENING_PAGES = 3
OPENING_CHARS = 20_000
TITLE_CANDIDATES = 10
TRIGRAM_THRESHOLD = 0.3


def section_keys(session: Session, document_id: uuid.UUID) -> frozenset[str]:
    rows = session.execute(
        select(DocumentSection.number, DocumentSection.title)
        .where(DocumentSection.document_id == document_id, DocumentSection.level > 0)
        .limit(2000)
    ).all()
    return frozenset(normalize_title(f"{n or ''} {t}") for n, t in rows if t)


def _opening(session: Session, document: Document) -> tuple[list[list], str, str, str]:
    """Page 1's layout and text, the opening pages, and the (longer) text the document is named from."""
    pages = session.execute(
        select(DocumentPage.page_number, DocumentPage.text, DocumentPage.lines)
        .where(DocumentPage.document_id == document.id, DocumentPage.page_number <= max(OPENING_PAGES, NAMING_PAGES))
        .order_by(DocumentPage.page_number)
    ).all()
    first_lines = pages[0].lines if pages else []
    first_text = pages[0].text if pages else ""
    opening = "\n".join(p.text for p in pages if p.page_number <= OPENING_PAGES)[:OPENING_CHARS]
    naming = "\n".join(p.text for p in pages)
    return first_lines, first_text, opening, naming


def _duplicates(session: Session, document: Document, settings: Settings) -> tuple[Decision, Document, dict] | None:
    base = (
        Document.organization_id == document.organization_id,
        Document.id != document.id,
        Document.status.in_(ACTIVE_STATUSES),
    )
    if document.duplicate_of_id:
        existing = session.get(Document, document.duplicate_of_id)
        if existing is not None:
            return Decision.EXACT_DUPLICATE, existing, {"match": "sha256"}
    if document.content_hash:
        existing = session.scalar(
            select(Document).where(*base, Document.content_hash == document.content_hash)
            .order_by(Document.created_at).limit(1)
        )
        if existing is not None:
            return Decision.CONTENT_DUPLICATE, existing, {"match": "text", "similarity": 1.0}
    if document.simhash is not None:
        distance = func.bit_count(
            cast(Document.simhash, BIT(64)).op("#")(cast(literal(document.simhash), BIT(64)))
        )
        row = session.execute(
            select(Document, distance.label("distance"))
            .where(*base, Document.simhash.is_not(None), distance <= settings.SIMHASH_DUPLICATE_DISTANCE)
            .order_by(distance).limit(1)
        ).first()
        if row is not None:
            return Decision.CONTENT_DUPLICATE, row[0], {
                "match": "near_duplicate", "similarity": round(1 - row[1] / 64, 3),
            }
    return None


def _candidates(session: Session, document: Document, meta: DocumentMetadata, normalized: str) -> list[CandidateFacts]:
    similarity = func.similarity(Policy.normalized_name, normalized) if normalized else literal(0.0)
    conditions = []
    if meta.policy_number.value:
        conditions.append(func.upper(Policy.policy_number) == meta.policy_number.value.upper())
    if meta.document_number.value:
        conditions.append(func.upper(Policy.document_number) == meta.document_number.value.upper())
    if normalized:
        conditions.append(similarity >= TRIGRAM_THRESHOLD)
    if not conditions:
        return []
    rows = session.execute(
        select(Policy, similarity.label("similarity"))
        .where(
            Policy.organization_id == document.organization_id,
            Policy.status == PolicyStatus.ACTIVE,
            or_(*conditions),
        )
        .order_by(similarity.desc())
        .limit(TITLE_CANDIDATES)
    ).all()

    candidates = []
    for policy, sim in rows:
        versions = session.scalars(
            select(PolicyVersion).where(
                PolicyVersion.policy_id == policy.id, PolicyVersion.status == VersionStatus.ACTIVE
            )
        ).all()
        facts = [
            VersionFacts(v.id, v.version_label, v.revision_number, v.effective_from, v.effective_to,
                         v.content_hash, v.document_id)
            for v in versions
        ]
        candidate = CandidateFacts(
            policy_id=policy.id, name=policy.name, normalized_name=policy.normalized_name,
            policy_number=policy.policy_number, document_number=policy.document_number,
            issuer=policy.issuer, issuing_department=policy.issuing_department,
            category_id=policy.category_id, title_similarity=float(sim or 0.0), versions=facts,
        )
        if candidate.latest:
            latest_document = session.get(Document, candidate.latest.document_id)
            candidate.latest_simhash = latest_document.simhash if latest_document else None
            candidate.latest_section_keys = section_keys(session, candidate.latest.document_id)
        candidates.append(candidate)
    return candidates


def _amendment_targets(session: Session, document: Document, meta: DocumentMetadata) -> list[dict]:
    targets = []
    for ref in meta.amendments:
        normalized = normalize_title(ref.target_text)
        if not normalized:
            continue
        sim = func.similarity(Policy.normalized_name, normalized)
        row = session.execute(
            select(Policy, sim.label("similarity"))
            .where(
                Policy.organization_id == document.organization_id,
                Policy.status == PolicyStatus.ACTIVE,
                or_(sim >= 0.45, func.upper(Policy.policy_number) == ref.target_text.upper().strip()),
            )
            .order_by(sim.desc())
            .limit(1)
        ).first()
        if row is None:
            continue
        policy, similarity = row
        as_of = meta.effective_date or datetime.now(UTC).date()
        version = session.scalar(
            select(PolicyVersion).where(
                PolicyVersion.policy_id == policy.id,
                PolicyVersion.status == VersionStatus.ACTIVE,
                PolicyVersion.effective_from <= as_of,
                or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to > as_of),
            )
        )
        targets.append({
            "relation_type": ref.relation_type,
            "policy_id": str(policy.id),
            "policy_name": policy.name,
            "version_id": str(version.id) if version else None,
            "version_label": version.version_label if version else None,
            "clauses": ref.clauses,
            "evidence": ref.sentence,
            "confidence": round(float(similarity), 2),
        })
    return targets


def analyze_document(
    session: Session, document: Document, settings: Settings, body_font_size: float,
    llm_factory: Callable[[], LLMProvider] | None = None,
) -> DocumentAnalysis:
    first_lines, first_text, opening, naming_text = _opening(session, document)
    front = session.scalar(
        select(DocumentSection.content).where(
            DocumentSection.document_id == document.id, DocumentSection.level == 0
        ).order_by(DocumentSection.order_index).limit(1)
    )
    opening_text = f"{front or ''}\n{opening}"[:OPENING_CHARS]
    pdf_title = (document.pdf_metadata or {}).get("title")
    meta = extract_metadata(first_lines, opening_text, body_font_size, pdf_title, settings.DATE_ORDER)
    named = read_content(llm_factory, naming_text, meta.title.value, pdf_title).name if llm_factory else None

    categories = session.scalars(
        select(Category).where(Category.organization_id == document.organization_id, Category.is_active.is_(True))
    ).all()
    classification = classify(
        [CategoryProfile(c.id, c.name, tuple(c.keywords)) for c in categories],
        named or meta.title.value, first_text, opening_text,
    )
    title = content_title(named, meta.title) or (
        meta.title if meta.title.value else fallback_title(opening_text, classification.name)
    )
    normalized = normalize_title(title.value or "")

    upload = UploadFacts(
        normalized_title=normalized,
        policy_number=meta.policy_number.value,
        document_number=meta.document_number.value or meta.circular_number.value,
        issuer=meta.issuer.value,
        department=meta.department.value,
        category_id=document.category_id or classification.category_id,
        effective_date=meta.effective_date,
        version_label=meta.version_label.value,
        revision=int(meta.revision_number.value) if meta.revision_number.value else None,
        content_hash=document.content_hash,
        simhash=document.simhash,
        section_keys=section_keys(session, document.id),
    )
    amendment_targets = _amendment_targets(session, document, meta)
    result, scored = decide(
        upload,
        _candidates(session, document, meta, normalized),
        high=settings.MATCH_HIGH_CONFIDENCE,
        low=settings.MATCH_LOW_CONFIDENCE,
        has_amendment_targets=bool(amendment_targets),
    )

    decision, confidence, conflict = result.decision, result.confidence, result.conflict
    duplicate_of = None
    if duplicate := _duplicates(session, document, settings):
        decision, existing, detail = duplicate
        duplicate_of = existing.id
        confidence = detail.get("similarity", 1.0)
        conflict = {"type": "DUPLICATE", **detail, "existing_document_id": str(existing.id),
                    "existing_policy_id": str(existing.policy_id) if existing.policy_id else None}

    # Nothing identified the category: file it under the organization's general "Policies"
    # category rather than hold it for review. A person can still change it.
    suggested_category_id = classification.category_id or next(
        (c.id for c in categories if c.slug == DEFAULT_CATEGORY_SLUG), None
    )

    # The effective date is optional: when the document does not state one, the
    # upload date keeps the timeline ordered, so it is not reported as missing.
    missing = []
    if not title.value:
        missing.append("name")
    if not (document.category_id or suggested_category_id):
        missing.append("category")

    best = result.best
    values = {
        "document_id": document.id,
        "organization_id": document.organization_id,
        "detected": meta.to_json(),
        "suggested_name": title.value,
        "name_confidence": title.confidence,
        "suggested_category_id": suggested_category_id,
        "category_confidence": classification.confidence,
        "category_ranking": [
            {"category_id": str(s.category_id), "name": s.name, "score": s.score, "matched": list(s.matched)}
            for s in classification.ranking
        ],
        "decision": decision,
        "confidence": round(confidence, 3),
        "matched_policy_id": best.candidate.policy_id if best and best.score >= settings.MATCH_LOW_CONFIDENCE else None,
        "matched_version_id": best.candidate.latest.id if best and best.candidate.latest and best.score >= settings.MATCH_LOW_CONFIDENCE else None,
        "duplicate_of_document_id": duplicate_of,
        "signals": [asdict(s) for s in best.signals] if best else [],
        "candidates": [s.summary() for s in scored[:5]],
        "amendment_targets": amendment_targets,
        "conflict": conflict,
        "missing_fields": missing,
        "review_status": ReviewStatus.PENDING,
        "reviewed_by_id": None,
        "reviewed_at": None,
        "resolution": None,
    }
    if "HISTORICAL_VERSION" in result.notes:
        values["detected"]["notes"] = ["HISTORICAL_VERSION"]
    session.execute(
        insert(DocumentAnalysis).values(values).on_conflict_do_update(
            index_elements=["document_id"], set_={k: v for k, v in values.items() if k != "document_id"}
        )
    )
    return session.get(DocumentAnalysis, document.id, populate_existing=True)
