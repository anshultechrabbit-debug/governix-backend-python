"""Embed chunk batches, reusing cached vectors for text already embedded anywhere."""

import logging
import os
import random
import time

from sqlalchemy import bindparam, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.infrastructure.ai import throttle
from app.infrastructure.ai.credit import rate_limit_wait
from app.infrastructure.ai.embeddings.base import EmbeddingUnavailableError
from app.infrastructure.queue.registry import JobContext, RetryLater, task
from app.modules.documents.model import Document
from app.modules.ingestion import pipeline, progress
from app.modules.ingestion.model import Stage
from app.modules.ingestion.text import sha256_text
from app.modules.search.model import Chunk, EmbeddingCache
from app.workers.common import PipelineError, failure_guard, load_document
from app.workers.runtime import get_runtime

logger = logging.getLogger(__name__)


def embedding_input(title: str | None, chunk: Chunk) -> str:
    """Contextual embedding: document title and section path travel with the text."""
    return f"{title or ''}\n{chunk.section_path}\n{chunk.text}".strip()


@task(pipeline.EMBED_BATCH)
def embed_batch(payload: dict, ctx: JobContext) -> None:
    rt = get_runtime()
    with rt.session_factory() as session:
        document = load_document(session, payload)
        if document is None:
            return
        with failure_guard(session, document.id, Stage.EMBEDDING, ctx):
            try:
                embedder = rt.embedder
            except EmbeddingUnavailableError as exc:
                raise PipelineError("EMBEDDINGS_UNAVAILABLE", f"Embedding provider unavailable: {exc}") from None

            chunks = session.scalars(
                select(Chunk).where(
                    Chunk.document_id == document.id,
                    Chunk.chunk_index.between(payload["start"], payload["end"]),
                    Chunk.embedding.is_(None),
                ).order_by(Chunk.chunk_index)
            ).all()
            if chunks:
                inputs = {c.id: embedding_input(document.title, c) for c in chunks}
                hashes = {c.id: sha256_text(inputs[c.id]) for c in chunks}
                cached = dict(session.execute(
                    select(EmbeddingCache.text_hash, EmbeddingCache.embedding).where(
                        EmbeddingCache.model == embedder.model_id,
                        EmbeddingCache.text_hash.in_(set(hashes.values())),
                    )
                ).all())
                missing = [c for c in chunks if hashes[c.id] not in cached]
                labels = dict.fromkeys(hashes.values(), embedder.model_id)
                if missing:
                    # A fallback labels its vectors with its own model, and they are stored under it.
                    texts = [inputs[c.id] for c in missing]
                    _wait_for_tokens(texts, embedder.model_id, ctx)
                    try:
                        embedded = embedder.embed(texts)
                    except Exception as exc:
                        # Many batches share one tokens-per-minute quota: wait for it, spread out,
                        # instead of spending attempts until the document fails.
                        wait = rate_limit_wait(exc)
                        if wait is None:
                            raise
                        raise RetryLater(wait + random.uniform(1, 15), "embedding rate limit") from exc
                    fresh = {hashes[c.id]: v for c, v in zip(missing, embedded.vectors, strict=True)}
                    session.execute(
                        insert(EmbeddingCache)
                        .values([{"model": embedded.model_id, "text_hash": h, "embedding": v} for h, v in fresh.items()])
                        .on_conflict_do_nothing()
                    )
                    cached.update(fresh)
                    labels.update(dict.fromkeys(fresh, embedded.model_id))
                # Writing vectors is the CPU-heavy part (each one is inserted into the HNSW
                # index). Only a few batches write at once, so uploads never take every core
                # from the database and answers stay fast while a large document indexes.
                _wait_for_write_slot(session, ctx)
                # One pipelined statement for the batch instead of a round trip per chunk.
                session.connection().execute(
                    update(Chunk.__table__).where(Chunk.__table__.c.id == bindparam("chunk_id"))
                    .values(embedding=bindparam("vector"), embedding_model=bindparam("label")),
                    [{"chunk_id": c.id, "vector": cached[hashes[c.id]], "label": labels[hashes[c.id]]} for c in chunks],
                )
                logger.info("Document %s: embedded %s chunks (%s from cache)",
                            document.id, len(chunks), len(chunks) - len(missing))
            session.commit()
            check_embeddings_complete(session, document.id, payload)


def _wait_for_tokens(texts: list[str], model_id: str, ctx) -> None:
    """Wait for this request's share of the account's tokens-per-minute budget."""
    rt = get_runtime()
    if rt.settings.EMBEDDING_PROVIDER.lower() != "openai":
        return  # only a remote provider has an account limit
    wait = throttle.reserve(rt.session_factory, f"embeddings:{model_id}",
                            throttle.estimate_tokens(texts), rt.settings.EMBEDDING_TOKENS_PER_MINUTE)
    while wait > 0:
        step = min(wait, 10.0)
        time.sleep(step)
        wait -= step
        ctx.heartbeat()


# Advisory-lock keys for the write slots (any value not used elsewhere).
WRITE_SLOT_LOCK_BASE = 7_340_000
WRITE_SLOT_POLL_SECONDS = 0.2


def write_slots(settings) -> int:
    if settings.EMBED_WRITE_SLOTS:
        return max(settings.EMBED_WRITE_SLOTS, 1)
    return max((os.cpu_count() or 2) // 2, 1)


def _wait_for_write_slot(session, ctx) -> None:
    """Hold one of the write slots until this transaction ends (a transaction-scoped advisory
    lock, so it works across worker threads and processes and is never left behind)."""
    slots = write_slots(get_runtime().settings)
    waited = 0.0
    while True:
        for slot in range(slots):
            if session.scalar(select(func.pg_try_advisory_xact_lock(WRITE_SLOT_LOCK_BASE + slot))):
                return
        time.sleep(WRITE_SLOT_POLL_SECONDS)
        waited += WRITE_SLOT_POLL_SECONDS
        if waited >= 10:
            ctx.heartbeat()
            waited = 0.0


def check_embeddings_complete(session, document_id, payload) -> None:
    rt = get_runtime()
    embedded, total = session.execute(
        select(func.count(Chunk.embedding), func.count()).where(Chunk.document_id == document_id)
    ).one()
    progress.set_done(session, document_id, Stage.EMBEDDING, embedded)
    if embedded == total:
        progress.finish(session, document_id, Stage.EMBEDDING, detail={"chunks": total})
        # The document is already searchable by keyword (see chunking); this checks the vectors.
        document = session.get(Document, document_id)
        pipeline.enqueue_step(rt.queue, session, document, pipeline.VERIFY_VECTORS, "verify-vectors")
    session.commit()
