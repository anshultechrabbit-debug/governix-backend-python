import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

# Every ON DELETE CASCADE foreign key: is its referencing column the leading column of an index?
UNINDEXED_CASCADES = text("""
    SELECT c.conrelid::regclass::text AS child, a.attname AS column_name
    FROM pg_constraint c
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
    WHERE c.contype = 'f' AND c.confdeltype = 'c'
      AND NOT EXISTS (
          SELECT 1 FROM pg_index i WHERE i.indrelid = c.conrelid AND i.indkey[0] = c.conkey[1]
      )
    ORDER BY 1, 2
""")


def test_every_cascading_foreign_key_is_indexed(db):
    """A delete looks up the rows referencing each deleted row; without an index every lookup
    scans the whole table (deleting a 2,500-page document took minutes on document_sections)."""
    missing = [f"{row.child}.{row.column_name}" for row in db.execute(UNINDEXED_CASCADES)]
    assert missing == [], f"Index these cascading foreign keys: {missing}"
