"""Embed chunk batches, reusing cached vectors for text already embedded anywhere."""

import logging

from sqlalchemy import bindparam, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.infrastructure.ai.embeddings.base import EmbeddingUnavailableError
from app.infrastructure.queue.registry import JobContext, task
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
                    embedded = embedder.embed([inputs[c.id] for c in missing])
                    fresh = {hashes[c.id]: v for c, v in zip(missing, embedded.vectors, strict=True)}
                    session.execute(
                        insert(EmbeddingCache)
                        .values([{"model": embedded.model_id, "text_hash": h, "embedding": v} for h, v in fresh.items()])
                        .on_conflict_do_nothing()
                    )
                    cached.update(fresh)
                    labels.update(dict.fromkeys(fresh, embedded.model_id))
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


def check_embeddings_complete(session, document_id, payload) -> None:
    rt = get_runtime()
    embedded, total = session.execute(
        select(func.count(Chunk.embedding), func.count()).where(Chunk.document_id == document_id)
    ).one()
    progress.set_done(session, document_id, Stage.EMBEDDING, embedded)
    if embedded == total:
        progress.finish(session, document_id, Stage.EMBEDDING, detail={"chunks": total})
        document = session.get(Document, document_id)
        pipeline.enqueue_step(rt.queue, session, document, pipeline.VERIFY, "verify")
    session.commit()
