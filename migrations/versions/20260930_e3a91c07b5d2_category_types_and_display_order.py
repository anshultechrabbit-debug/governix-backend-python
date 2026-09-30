"""category types, unique category names, and arranged display order

Revision ID: e3a91c07b5d2
Revises: d81f3c6a2e94
Create Date: 2026-09-30 16:00:00.000000

* categories.category_type: what kind of documents the category holds (policy,
  circular, sop ...). The seeded categories get their type from their slug; any
  other existing category becomes "other".
* uq_categories_org_name: category names are unique per organisation, ignoring
  case. Existing case-insensitive duplicates are renamed "<name> (2)" first.
* policies.display_order: the order a person arranged policies in a category.
  Presentational only; NULL until arranged. (The order of a policy's versions
  is its effective-date timeline, not a display setting.)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3a91c07b5d2"
down_revision: str | Sequence[str] | None = "d81f3c6a2e94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEEDED_TYPES = {
    "regulatory-documents": "regulatory_document",
    "policies": "policy",
    "circulars": "circular",
    "guidelines": "guideline",
    "sops": "sop",
    "manuals": "manual",
    "process-documents": "process_document",
    "notices": "notice",
    "forms": "form",
    "faqs": "faq",
}


def upgrade() -> None:
    op.add_column("categories", sa.Column("category_type", sa.String(30), nullable=False, server_default="other"))
    categories = sa.table("categories", sa.column("slug", sa.String), sa.column("category_type", sa.String))
    for slug, category_type in SEEDED_TYPES.items():
        op.execute(categories.update().where(categories.c.slug == slug).values(category_type=category_type))

    op.execute("""
        UPDATE categories AS c
        SET name = left(c.name, 90) || ' (' || d.rn || ')'
        FROM (
            SELECT id, row_number() OVER (PARTITION BY organization_id, lower(name) ORDER BY created_at, id) AS rn
            FROM categories
        ) AS d
        WHERE c.id = d.id AND d.rn > 1
    """)
    op.create_index(
        "uq_categories_org_name", "categories", ["organization_id", sa.text("lower(name)")], unique=True
    )

    op.add_column("policies", sa.Column("display_order", sa.Integer()))
    op.create_index("ix_policies_category_display_order", "policies", ["category_id", "display_order"])


def downgrade() -> None:
    op.drop_index("ix_policies_category_display_order", table_name="policies")
    op.drop_column("policies", "display_order")
    op.drop_index("uq_categories_org_name", table_name="categories")
    op.drop_column("categories", "category_type")
