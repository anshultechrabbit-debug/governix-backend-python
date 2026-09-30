"""chunks text trigram index

Revision ID: 5f2c9a71b840
Revises: d7146a00302d
Create Date: 2026-09-29 16:30:00.000000

The exact lane (quoted phrases, clause numbers, "Chapter 5") and the acronym
glossary lookup both match `chunks.text` with a PostgreSQL regex (~ / ~*).
Those predicates are not indexable by the btree/GIN indexes already present on
`chunks`, so without a trigram index PostgreSQL can only satisfy them with a
sequential scan of the entire chunk table -- on the request path, for every
question containing a clause or division reference.

gin_trgm_ops makes the regex match index-backed. The index is created
CONCURRENTLY so that building it does not take a write lock on `chunks` during
the deploy; it is skipped if it already exists, so the migration is re-runnable
against a partially-applied database.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5f2c9a71b840"
down_revision: str | Sequence[str] | None = "38e380d25b13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_chunks_text_trgm"


def _concurrently(statement: str) -> None:
    """Run a CONCURRENTLY statement outside Alembic's transaction.

    CREATE/DROP INDEX CONCURRENTLY cannot execute inside a transaction block, so
    the surrounding transaction is committed first and a new one is started
    afterwards. The index build then never takes a write lock on `chunks`.
    """
    with op.get_context().autocommit_block():
        op.execute(statement)


def upgrade() -> None:
    # pg_trgm is created by the extensions migration, but guard anyway: a
    # database restored from a dump may not have run it.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    _concurrently(
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON chunks USING gin (text gin_trgm_ops)"
    )


def downgrade() -> None:
    _concurrently(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
