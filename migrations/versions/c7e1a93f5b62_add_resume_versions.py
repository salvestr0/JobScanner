"""Add resume_versions table (move saved builder versions off ephemeral disk)

Saved resume-builder versions were written to data/users/{id}/resume_versions/*.json,
which is wiped on every Render deploy/restart — silent data loss. Persist them in
Postgres instead. Purged on account deletion via the User.resume_versions cascade.

Revision ID: c7e1a93f5b62
Revises: b2f4d6a8c1e3
Create Date: 2026-06-27
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c7e1a93f5b62'
down_revision = 'b2f4d6a8c1e3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'resume_versions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=True),
        sa.Column('source', sa.String(length=40), nullable=True),
        sa.Column('job_title', sa.String(length=120), nullable=True),
        sa.Column('company', sa.String(length=120), nullable=True),
        sa.Column('profile', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_resume_versions_user_id', 'resume_versions', ['user_id'])
    op.create_index('ix_resume_versions_created_at', 'resume_versions', ['created_at'])
    # Match the project's RLS policy (see d2c8e4a7f19b): block PostgREST/anon access.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE public.resume_versions ENABLE ROW LEVEL SECURITY')


def downgrade():
    op.drop_index('ix_resume_versions_created_at', table_name='resume_versions')
    op.drop_index('ix_resume_versions_user_id', table_name='resume_versions')
    op.drop_table('resume_versions')
