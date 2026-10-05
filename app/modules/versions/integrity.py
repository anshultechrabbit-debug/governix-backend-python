"""Checks that a policy's version history is what it claims to be.

Two problems make an answer about "the current rule" wrong without any single
passage being wrong:

* a version that is not a revision of the policy at all: another document
  (another bank's deposit policy) filed as "version 5" of the microfinance policy.
  Its passages are then presented, and summarised as "changes", under the
  policy's name;
* a version that is in force but cannot be searched, because its document failed
  to process. The newest searchable version then looks current although it is not.

Both are read from data already stored: document simhashes and first pages for the
first, version dates and document status for the second.
"""

import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.documents.model import Document, DocumentPage, DocumentStatus
from app.modules.policies.model import Policy, PolicyStatus, PolicyVersion, VersionStatus

# Of 64 simhash bits, unrelated documents differ in about half; revisions of one
# document in far fewer (9-16 measured on revised manuals). Both this distance and
# little shared vocabulary on the opening pages are required, so a heavily
# rewritten revision is not mistaken for another document.
UNRELATED_SIMHASH_BITS = 24
UNRELATED_MAX_OVERLAP = 0.2
OPENING_PAGES = 3
_WORD = re.compile(r"[a-z]{4,}")
_CACHE_SECONDS = 600
_cache: dict[uuid.UUID, tuple[float, "Unrelated | None"]] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class Unrelated:
    """A version whose document is not a revision of the version before it."""

    version_id: uuid.UUID
    version_label: str
    previous_label: str
    heading: str  # what the document calls itself, from its first page


def _hamming(a: int, b: int) -> int:
    return bin((a ^ b) & (2**64 - 1)).count("1")


def _opening(session: Session, document_id: uuid.UUID) -> str:
    pages = session.scalars(
        select(DocumentPage.text).where(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number).limit(OPENING_PAGES)
    ).all()
    return "\n".join(pages)


def _overlap(a: str, b: str) -> float:
    words_a, words_b = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    return len(words_a & words_b) / max(1, min(len(words_a), len(words_b)))


def _heading(text: str) -> str:
    """The document's own title: the longest upper-case line of its first page, else its first line."""
    lines = [" ".join(line.split()) for line in text.splitlines()[:40] if len(line.strip()) >= 6]
    shouted = [line for line in lines if line.isupper() and len(re.findall(r"[A-Z]", line)) >= 6]
    return (max(shouted, key=len) if shouted else (lines[0] if lines else ""))[:120]


def unrelated_to_previous(session: Session, version: PolicyVersion) -> Unrelated | None:
    """`version` when its document is not a revision of the policy's previous version."""
    previous = session.scalars(
        select(PolicyVersion).where(
            PolicyVersion.policy_id == version.policy_id,
            PolicyVersion.id != version.id,
            PolicyVersion.effective_from < version.effective_from,
        ).order_by(PolicyVersion.effective_from.desc()).limit(1)
    ).first()
    if previous is None:
        return None
    documents = {d.id: d for d in session.scalars(
        select(Document).where(Document.id.in_([version.document_id, previous.document_id]))
    )}
    this, before = documents.get(version.document_id), documents.get(previous.document_id)
    if this is None or before is None or this.simhash is None or before.simhash is None:
        return None
    if _hamming(this.simhash, before.simhash) < UNRELATED_SIMHASH_BITS:
        return None
    opening = _opening(session, this.id)
    if not opening or _overlap(opening, _opening(session, before.id)) >= UNRELATED_MAX_OVERLAP:
        return None
    return Unrelated(version.id, version.version_label, previous.version_label, _heading(opening))


def unrelated_versions(session: Session, version_ids) -> dict[uuid.UUID, Unrelated]:
    """Which of these versions are not revisions of their policy (cached: documents do not change)."""
    now = time.monotonic()
    found: dict[uuid.UUID, Unrelated] = {}
    for version_id in {v for v in version_ids if v}:
        with _lock:
            cached = _cache.get(version_id)
        if cached and cached[0] > now:
            result = cached[1]
        else:
            version = session.get(PolicyVersion, version_id)
            result = unrelated_to_previous(session, version) if version else None
            with _lock:
                _cache[version_id] = (now + _CACHE_SECONDS, result)
        if result:
            found[version_id] = result
    return found


@dataclass(frozen=True)
class RegisteredVersion:
    id: uuid.UUID
    label: str
    effective_from: date
    effective_to: date | None
    searchable: bool
    document_status: str


def same_policy(session: Session, policy_id: uuid.UUID) -> list[uuid.UUID]:
    """The policy and any other active record of the organization with the same name.

    A policy registered twice ("Credit, Risk and Operations Policy Manual" with one version
    on one record and its later versions on another) is still one policy: which version is
    current must be read across both records."""
    policy = session.get(Policy, policy_id)
    if policy is None:
        return [policy_id]
    return list(session.scalars(
        select(Policy.id).where(
            Policy.organization_id == policy.organization_id,
            func.lower(func.trim(Policy.name)) == policy.name.strip().lower(),
            Policy.status == PolicyStatus.ACTIVE,
        )
    )) or [policy_id]


def newer_versions(session: Session, policy_id: uuid.UUID, after: date) -> list[RegisteredVersion]:
    """The policy's active versions that start after `after`, with whether each can be searched."""
    rows = session.execute(
        select(PolicyVersion, Document.status)
        .join(Document, Document.id == PolicyVersion.document_id)
        .where(
            PolicyVersion.policy_id.in_(same_policy(session, policy_id)),
            PolicyVersion.status == VersionStatus.ACTIVE,
            PolicyVersion.effective_from > after,
        )
        .order_by(PolicyVersion.effective_from)
    ).all()
    return [
        RegisteredVersion(v.id, v.version_label, v.effective_from, v.effective_to, status == DocumentStatus.READY,
                          str(status.value if hasattr(status, "value") else status))
        for v, status in rows
    ]
