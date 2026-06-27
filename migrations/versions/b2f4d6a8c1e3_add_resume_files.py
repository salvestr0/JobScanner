"""Add resume_files table for storing raw CV uploads

Persists the original resume/CV bytes from every upload path (profile parse,
logged-in ATS check, and the anonymous public ATS checker). Logged-in rows are
purged on account deletion via the User.resume_files cascade; anonymous rows
(user_id NULL) are auto-purged by the cleanup cron after a retention window.

Revision ID: b2f4d6a8c1e3
Revises: a3f9c2e7b481
Create Date: 2026-06-27
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b2f4d6a8c1e3'
down_revision = 'a3f9c2e7b481'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'resume_files',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=True),
        sa.Column('content_type', sa.String(length=128), nullable=True),
        sa.Column('byte_size', sa.Integer(), nullable=True),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column('target_role', sa.String(length=120), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_resume_files_user_id', 'resume_files', ['user_id'])
    op.create_index('ix_resume_files_uploaded_at', 'resume_files', ['uploaded_at'])
    # Match the project's RLS policy (see d2c8e4a7f19b): block PostgREST/anon
    # Data API access to this PII table. The Flask app connects as the owning
    # role and bypasses RLS.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE public.resume_files ENABLE ROW LEVEL SECURITY')


def downgrade():
    op.drop_index('ix_resume_files_uploaded_at', table_name='resume_files')
    op.drop_index('ix_resume_files_user_id', table_name='resume_files')
    op.drop_table('resume_files')
