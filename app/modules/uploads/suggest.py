"""A name for a new policy, read from a file before it is uploaded for real.

While files are being arranged, the person should see the name the document
gives itself rather than its file name, and be told when that policy already
exists, so a new version is not filed as a second policy. The opening pages go
through the same detectors the pipeline uses, so the name shown is the name
the pipeline would choose. Nothing is stored.
"""

import hashlib
import logging
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import BinaryIO

import pymupdf
from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.infrastructure.ai.llm.base import LLMProvider
from app.modules.auth.acl import can_write_scope, visible_clause
from app.modules.auth.permissions import Permission, Principal
from app.modules.auth.scope import tenant_id
from app.modules.categories.model import Category
from app.modules.documents.model import Document
from app.modules.documents.repository import ACTIVE_STATUSES
from app.modules.ingestion.analysis.analyzer import OPENING_CHARS, OPENING_PAGES
from app.modules.ingestion.analysis.classify import CategoryProfile, classify
from app.modules.ingestion.analysis.matching import CandidateFacts, ScoredCandidate, UploadFacts, score_candidate
from app.modules.ingestion.analysis.metadata import (
    DocumentMetadata,
    extract_metadata,
    fallback_title,
    normalize_title,
)
from app.modules.ingestion.analysis.naming import NAMING_PAGES, ContentRead, content_title, describe, read_content
from app.modules.ingestion.extraction import PageExtraction, extract_page
from app.modules.ingestion.structure import LayoutStats
from app.modules.policies.model import Policy, PolicyStatus
from app.modules.policies.repository import PolicyRepository, policy_visible
from app.modules.uploads.schema import ExistingPolicyMatch, IdenticalDocument, PolicySuggestion

logger = logging.getLogger(__name__)

# Only the opening pages are read, but PyMuPDF needs the whole file.
MAX_BYTES = 100 * 1024 * 1024
# Before upload there is no content to compare, only identity: the policy
# number, the document number, or a name at least this similar.
NAME_MATCH = 0.6
CANDIDATES = 5


def suggest_policy(
    session: Session, principal: Principal, settings: Settings, file: BinaryIO,
    intended_policy_id: uuid.UUID | None = None, llm_factory: Callable[[], LLMProvider] | None = None,
) -> PolicySuggestion:
    organization_id = tenant_id(principal)
    with tempfile.NamedTemporaryFile(suffix=".pdf") as spool:
        digest, size = hashlib.sha256(), 0
        while chunk := file.read(1 << 20):
            size += len(chunk)
            if size > MAX_BYTES:
                return PolicySuggestion(readable=False)
            digest.update(chunk)
            spool.write(chunk)
        spool.flush()
        identical = _identical_document(session, principal, digest.hexdigest())
        opening = _read_opening(spool.name, settings)

    pages, naming_text, pdf_title = opening or ([], "", None)
    if not any(page.char_count >= settings.OCR_MIN_CHARS for page in pages):
        return PolicySuggestion(readable=False, identical_document=identical)

    stats = LayoutStats()
    for page in pages:
        stats.observe(page.lines)
    stats.finalize()
    opening_text = "\n".join(page.text for page in pages)[:OPENING_CHARS]
    meta = extract_metadata(pages[0].lines, opening_text, stats.body_size, pdf_title, settings.DATE_ORDER)
    read = read_content(llm_factory, naming_text, meta.title.value, pdf_title) if llm_factory else ContentRead()
    named = read.name
    categories = session.scalars(
        select(Category).where(Category.organization_id == organization_id, Category.is_active.is_(True))
    ).all()
    classification = classify(
        [CategoryProfile(c.id, c.name, tuple(c.keywords)) for c in categories],
        named or meta.title.value, pages[0].text, opening_text,
    )
    title = content_title(named, meta.title) or (
        meta.title if meta.title.value else fallback_title(opening_text, classification.name)
    )
    return PolicySuggestion(
        readable=True,
        name=title.value,
        about=describe(read, naming_text, title.value),
        name_source=title.source,
        name_confidence=title.confidence,
        category_id=classification.category_id,
        category_name=classification.name,
        version_label=meta.version_label.value,
        effective_date=meta.effective_date,
        existing_policy=_existing_policy(
            session, principal, meta, normalize_title(title.value or ""), classification.category_id, intended_policy_id,
        ),
        identical_document=identical,
    )


def _read_opening(path: str, settings: Settings) -> tuple[list[PageExtraction], str, str | None] | None:
    """The opening pages with their layout, the plain text the document is named from, and its PDF title."""
    try:
        pdf = pymupdf.open(path, filetype="pdf")
    except Exception:
        return None
    try:
        if pdf.needs_pass:
            return None
        pages = [extract_page(pdf[i], ocr_min_chars=settings.OCR_MIN_CHARS) for i in range(min(OPENING_PAGES, pdf.page_count))]
        later = [pdf[i].get_text() for i in range(len(pages), min(NAMING_PAGES, pdf.page_count))]
        return pages, "\n".join([*(page.text for page in pages), *later]), (pdf.metadata or {}).get("title")
    except Exception:  # a suggestion is an enhancement; the real upload reports the problem
        logger.warning("Could not read the opening pages for a policy suggestion", exc_info=True)
        return None
    finally:
        pdf.close()


def _identical_document(session: Session, principal: Principal, sha256: str) -> IdenticalDocument | None:
    document = session.scalar(
        select(Document).where(
            visible_clause(principal, Document.organization_id, Document.branch_id, Document.department_id, Document.policy_id),
            Document.file_sha256 == sha256,
            Document.status.in_(ACTIVE_STATUSES),
        ).order_by(Document.created_at).limit(1)
    )
    if document is None:
        return None
    policy = session.get(Policy, document.policy_id) if document.policy_id else None
    return IdenticalDocument(
        document_id=document.id, title=document.title, original_filename=document.original_filename,
        policy_id=policy.id if policy else None, policy_name=policy.name if policy else None,
    )


def _existing_policy(
    session: Session, principal: Principal, meta: DocumentMetadata, normalized: str,
    category_id: uuid.UUID | None,
    intended_policy_id: uuid.UUID | None,
) -> ExistingPolicyMatch | None:
    number = meta.document_number.value or meta.circular_number.value
    similarity = func.similarity(Policy.normalized_name, normalized) if normalized else literal(0.0)
    conditions = []
    if meta.policy_number.value:
        conditions.append(func.upper(Policy.policy_number) == meta.policy_number.value.upper())
    if number:
        conditions.append(func.upper(Policy.document_number) == number.upper())
    if normalized:
        conditions.append(similarity >= NAME_MATCH)
    if not conditions:
        return None
    if intended_policy_id:
        conditions.append(Policy.id == intended_policy_id)
    rows = session.execute(
        select(Policy, similarity.label("similarity"))
        .where(policy_visible(principal), Policy.status == PolicyStatus.ACTIVE, or_(*conditions))
        .order_by(similarity.desc())
        .limit(CANDIDATES)
    ).all()
    upload = UploadFacts(
        normalized_title=normalized, policy_number=meta.policy_number.value, document_number=number,
        issuer=meta.issuer.value, department=meta.department.value, category_id=category_id,
        effective_date=meta.effective_date, version_label=meta.version_label.value, revision=None,
        content_hash=None, simhash=None, section_keys=frozenset(),
    )
    scored = sorted(
        (score_candidate(upload, CandidateFacts(
            policy_id=policy.id, name=policy.name, normalized_name=policy.normalized_name,
            policy_number=policy.policy_number, document_number=policy.document_number, issuer=policy.issuer,
            issuing_department=policy.issuing_department, category_id=policy.category_id,
            title_similarity=float(sim or 0.0),
        )) for policy, sim in rows),
        key=lambda s: s.score, reverse=True,
    )
    same = [s for s in scored if _same_policy(s)]
    if not same:
        return None
    # The policy the person chose wins when the content fits it too.
    best = next((s for s in same if s.candidate.policy_id == intended_policy_id), same[0])
    policy = next(p for p, _ in rows if p.id == best.candidate.policy_id)
    repository = PolicyRepository(session)
    current = repository.current_versions([policy.id], datetime.now(UTC).date()).get(policy.id)
    category = session.get(Category, policy.category_id)
    return ExistingPolicyMatch(
        policy_id=policy.id, name=policy.name, category_id=policy.category_id,
        category_name=category.name if category else None,
        current_version_label=current.version_label if current else None,
        current_effective_from=current.effective_from if current else None,
        version_count=repository.version_counts([policy.id]).get(policy.id, 0),
        reasons=[signal.detail for signal in best.signals if signal.matched],
        can_add_version=principal.has(Permission.POLICIES_MANAGE)
        and can_write_scope(principal, policy.branch_id, policy.department_id),
    )


def _same_policy(scored: ScoredCandidate) -> bool:
    """The same policy number, or a matching name or document number without a clashing policy number."""
    matched = {s.signal for s in scored.signals if s.matched}
    if "policy_number" in matched:
        return True
    clashing = any(s.signal == "policy_number" for s in scored.signals)
    return not clashing and bool(matched & {"document_number", "title"})
