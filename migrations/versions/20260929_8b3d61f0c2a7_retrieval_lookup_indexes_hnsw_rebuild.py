"""retrieval lookup indexes and denser HNSW graph

Revision ID: 8b3d61f0c2a7
Revises: 5f2c9a71b840
Create Date: 2026-09-29 20:00:00.000000

Three request-path lookups were sequential scans of `chunks`:

* page lookups ("What does page 500 contain?")      -> (organization_id, page_start)
* clause / division lookups by section number        -> section_number pattern index
* division lookups by path prefix ("Chapter 8 > ...") -> section_path pattern index

The pattern operator classes serve both equality and anchored LIKE 'x%'.

The HNSW index is rebuilt with m=32, ef_construction=128 (was 16 / 64). At
ef_search=100 the old graph returned only ~0.70-0.77 of the exact top-10
neighbours on a corpus with many near-duplicate chunks (repeated boilerplate),
and raising ef_search did not help: the graph itself was too sparse. The new
index is built alongside the old one and swapped in by rename, so queries keep
an index throughout.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8b3d61f0c2a7"
down_revision: str | Sequence[str] | None = "5f2c9a71b840"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HNSW = "ix_chunks_embedding_hnsw"


def _concurrently(*statements: str) -> None:
    """CREATE/DROP INDEX CONCURRENTLY cannot run inside a transaction block."""
    with op.get_context().autocommit_block():
        for statement in statements:
            op.execute(statement)


def _rebuild_hnsw(m: int, ef_construction: int) -> None:
    _concurrently(
        # HNSW builds are memory-bound; the default 64MB spills to disk and is slow.
        "SET maintenance_work_mem = '1GB'",
        f"DROP INDEX CONCURRENTLY IF EXISTS {HNSW}_new",
        f"CREATE INDEX CONCURRENTLY {HNSW}_new ON chunks USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = {m}, ef_construction = {ef_construction})",
        f"DROP INDEX CONCURRENTLY IF EXISTS {HNSW}",
        f"ALTER INDEX {HNSW}_new RENAME TO {HNSW}",
        "RESET maintenance_work_mem",
    )


def upgrade() -> None:
    _concurrently(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_org_page ON chunks (organization_id, page_start)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_section_number "
        "ON chunks (section_number varchar_pattern_ops)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_section_path ON chunks (section_path text_pattern_ops)",
    )
    _rebuild_hnsw(32, 128)


def downgrade() -> None:
    _rebuild_hnsw(16, 64)
    _concurrently(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_chunks_section_path",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_chunks_section_number",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_chunks_org_page",
    )
