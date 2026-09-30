"""policies versions relationships analyses

Revision ID: a484086ebc97
Revises: 4449afbc9c74
Create Date: 2026-09-28 17:19:55.391917
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a484086ebc97'
down_revision: str | Sequence[str] | None = '4449afbc9c74'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # btree_gist lets the exclusion constraint combine "=" on policy_id with "&&" on date ranges.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table('policies',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('branch_id', sa.Uuid(), nullable=True),
    sa.Column('department_id', sa.Uuid(), nullable=True),
    sa.Column('category_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=500), nullable=False),
    sa.Column('normalized_name', sa.String(length=500), nullable=False),
    sa.Column('policy_number', sa.String(length=100), nullable=True),
    sa.Column('document_number', sa.String(length=100), nullable=True),
    sa.Column('issuer', sa.String(length=200), nullable=True),
    sa.Column('issuing_department', sa.String(length=200), nullable=True),
    sa.Column('owner', sa.String(length=200), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], name=op.f('fk_policies_branch_id_branches'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], name=op.f('fk_policies_category_id_categories'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_policies_created_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], name=op.f('fk_policies_department_id_departments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_policies_organization_id_organizations'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_policies'))
    )
    op.create_index('ix_policies_normalized_name_trgm', 'policies', ['normalized_name'], unique=False, postgresql_using='gin', postgresql_ops={'normalized_name': 'gin_trgm_ops'})
    op.create_index('ix_policies_org_category', 'policies', ['organization_id', 'category_id'], unique=False)
    op.create_index('ix_policies_org_document_number', 'policies', ['organization_id', 'document_number'], unique=False)
    op.create_index('ix_policies_scope', 'policies', ['organization_id', 'branch_id', 'department_id'], unique=False)
    op.create_index('uq_policies_org_policy_number', 'policies', ['organization_id', sa.literal_column('upper(policy_number)')], unique=True, postgresql_where=sa.text('policy_number IS NOT NULL'))
    op.create_table('document_analyses',
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('detected', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('suggested_name', sa.String(length=500), nullable=True),
    sa.Column('name_confidence', sa.Double(), nullable=False),
    sa.Column('suggested_category_id', sa.Uuid(), nullable=True),
    sa.Column('category_confidence', sa.Double(), nullable=False),
    sa.Column('category_ranking', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('decision', sa.String(length=40), nullable=False),
    sa.Column('confidence', sa.Double(), nullable=False),
    sa.Column('matched_policy_id', sa.Uuid(), nullable=True),
    sa.Column('matched_version_id', sa.Uuid(), nullable=True),
    sa.Column('duplicate_of_document_id', sa.Uuid(), nullable=True),
    sa.Column('signals', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('candidates', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('amendment_targets', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('conflict', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('missing_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('review_status', sa.String(length=20), nullable=False),
    sa.Column('reviewed_by_id', sa.Uuid(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolution', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_analyses_document_id_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('document_id', name=op.f('pk_document_analyses'))
    )
    op.create_table('policy_versions',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('policy_id', sa.Uuid(), nullable=False),
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('version_number', sa.Integer(), nullable=False),
    sa.Column('version_label', sa.String(length=50), nullable=False),
    sa.Column('revision_number', sa.Integer(), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('effective_to', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=True),
    sa.Column('supersedes_version_id', sa.Uuid(), nullable=True),
    sa.Column('superseded_by_version_id', sa.Uuid(), nullable=True),
    sa.Column('change_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('ai_change_summary', sa.Text(), nullable=True),
    sa.Column('withdrawn_reason', sa.Text(), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    postgresql.ExcludeConstraint((sa.column('policy_id'), '='), (sa.text("daterange(effective_from, effective_to, '[)')"), '&&'), where=sa.text("status = 'active'"), using='gist', name='ex_policy_versions_no_overlap'),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_policy_versions_created_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_policy_versions_document_id_documents'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_policy_versions_organization_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['policy_id'], ['policies.id'], name=op.f('fk_policy_versions_policy_id_policies'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['superseded_by_version_id'], ['policy_versions.id'], name=op.f('fk_policy_versions_superseded_by_version_id_policy_versions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['supersedes_version_id'], ['policy_versions.id'], name=op.f('fk_policy_versions_supersedes_version_id_policy_versions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_policy_versions')),
    sa.UniqueConstraint('document_id', name=op.f('uq_policy_versions_document_id')),
    sa.UniqueConstraint('policy_id', 'version_number', name='uq_policy_versions_policy_id_version_number')
    )
    op.create_index('ix_policy_versions_org_effective', 'policy_versions', ['organization_id', 'effective_from', 'effective_to'], unique=False)
    op.create_index('ix_policy_versions_policy_effective', 'policy_versions', ['policy_id', 'effective_from'], unique=False)
    op.create_table('document_relationships',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('source_document_id', sa.Uuid(), nullable=False),
    sa.Column('relation_type', sa.String(length=20), nullable=False),
    sa.Column('target_policy_id', sa.Uuid(), nullable=False),
    sa.Column('target_version_id', sa.Uuid(), nullable=True),
    sa.Column('clauses', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('evidence', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('confidence', sa.Double(), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_document_relationships_created_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_document_relationships_organization_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_document_id'], ['documents.id'], name=op.f('fk_document_relationships_source_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_policy_id'], ['policies.id'], name=op.f('fk_document_relationships_target_policy_id_policies'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_version_id'], ['policy_versions.id'], name=op.f('fk_document_relationships_target_version_id_policy_versions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_relationships'))
    )
    op.create_index('ix_document_relationships_source', 'document_relationships', ['source_document_id'], unique=False)
    op.create_index('ix_document_relationships_target', 'document_relationships', ['target_policy_id', 'status'], unique=False)
    op.create_foreign_key(op.f('fk_documents_policy_id_policies'), 'documents', 'policies', ['policy_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('fk_documents_policy_version_id_policy_versions'), 'documents', 'policy_versions', ['policy_version_id'], ['id'], ondelete='SET NULL')
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(op.f('fk_documents_policy_version_id_policy_versions'), 'documents', type_='foreignkey')
    op.drop_constraint(op.f('fk_documents_policy_id_policies'), 'documents', type_='foreignkey')
    op.drop_index('ix_document_relationships_target', table_name='document_relationships')
    op.drop_index('ix_document_relationships_source', table_name='document_relationships')
    op.drop_table('document_relationships')
    op.drop_index('ix_policy_versions_policy_effective', table_name='policy_versions')
    op.drop_index('ix_policy_versions_org_effective', table_name='policy_versions')
    op.drop_table('policy_versions')
    op.drop_table('document_analyses')
    op.drop_index('uq_policies_org_policy_number', table_name='policies', postgresql_where=sa.text('policy_number IS NOT NULL'))
    op.drop_index('ix_policies_scope', table_name='policies')
    op.drop_index('ix_policies_org_document_number', table_name='policies')
    op.drop_index('ix_policies_org_category', table_name='policies')
    op.drop_index('ix_policies_normalized_name_trgm', table_name='policies', postgresql_using='gin', postgresql_ops={'normalized_name': 'gin_trgm_ops'})
    op.drop_table('policies')
    # ### end Alembic commands ###
