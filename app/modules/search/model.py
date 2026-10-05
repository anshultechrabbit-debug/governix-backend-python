import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UUIDPrimaryKeyMixin

# Fixed by the column type; EMBEDDING_DIMENSIONS must match (validated at startup).
VECTOR_DIMENSIONS = 1536


class Chunk(UUIDPrimaryKeyMixin, Base):
    """Retrieval unit. Carries its ACL scope and version identity so every search
    can filter by permission and effective date inside the index scan."""

    __tablename__ = "chunks"

    organization_id: Mapped[uuid.UUID]
    branch_id: Mapped[uuid.UUID | None]
    department_id: Mapped[uuid.UUID | None]
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("policies.id", ondelete="CASCADE"))
    version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("policy_versions.id", ondelete="CASCADE"))
    category_id: Mapped[uuid.UUID | None]
    section_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("document_sections.id", ondelete="CASCADE"))
    chunk_index: Mapped[int]
    section_number: Mapped[str | None] = mapped_column(String(50))
    section_path: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int]
    page_end: Mapped[int]
    char_start: Mapped[int] = mapped_column(default=0)  # offset within the section
    token_count: Mapped[int]
    chunk_hash: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(VECTOR_DIMENSIONS))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    tsv = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(section_path, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(text, '')), 'B')",
            persisted=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_chunks_document_index", "document_id", "chunk_index"),
        Index("ix_chunks_scope", "organization_id", "branch_id", "department_id"),
        Index("ix_chunks_version", "version_id"),
        # Deleting a policy cascades to its chunks through policy_id.
        Index("ix_chunks_policy", "policy_id"),
        Index("ix_chunks_section", "section_id"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        # The exact lane and the acronym/glossary lookup match Chunk.text with a
        # regex (~ / ~*). Without a trigram index those predicates can only be
        # satisfied by a sequential scan of the whole chunk table, on the request
        # path, for every question. gin_trgm_ops makes them index-backed.
        Index(
            "ix_chunks_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
        # Page lookups ("What does page 500 contain?") within the caller's organisation.
        Index("ix_chunks_org_page", "organization_id", "page_start"),
        # Clause / division lookups: equality and anchored prefix LIKE ("5.2.%", "Chapter 8 >%").
        Index("ix_chunks_section_number", "section_number", postgresql_ops={"section_number": "varchar_pattern_ops"}),
        Index("ix_chunks_section_path", "section_path", postgresql_ops={"section_path": "text_pattern_ops"}),
        # m=32 / ef_construction=128: with 16 / 64 the graph was too sparse on a
        # corpus with near-duplicate chunks and recall stalled at ~0.75.
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 32, "ef_construction": 128},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class EmbeddingCache(Base):
    """Embeddings keyed by (model, text hash): unchanged text across versions,
    re-uploads and re-indexing is never paid for twice."""

    __tablename__ = "embedding_cache"

    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    text_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(VECTOR_DIMENSIONS))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
