"""Add error_logs table for the admin error dashboard

Persists application errors (unhandled request exceptions + scan failures) so
they survive Render's ephemeral stdout and are viewable in /admin. Errors are
de-duplicated by fingerprint (see models.ErrorLog).

Revision ID: a3f9c2e7b481
Revises: d2c8e4a7f19b
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a3f9c2e7b481'
down_revision = 'd2c8e4a7f19b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'error_logs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=True),
        sa.Column('level', sa.String(length=16), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=True),
        sa.Column('error_type', sa.String(length=128), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('traceback', sa.Text(), nullable=True),
        sa.Column('path', sa.String(length=255), nullable=True),
        sa.Column('method', sa.String(length=8), nullable=True),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('user_email', sa.String(length=255), nullable=True),
        sa.Column('occurrences', sa.Integer(), nullable=True),
        sa.Column('resolved', sa.Boolean(), nullable=True),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_error_logs_fingerprint', 'error_logs', ['fingerprint'])
    op.create_index('ix_error_logs_resolved', 'error_logs', ['resolved'])
    op.create_index('ix_error_logs_last_seen', 'error_logs', ['last_seen'])
    # Match the project's RLS policy (see d2c8e4a7f19b): block PostgREST/anon access.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE public.error_logs ENABLE ROW LEVEL SECURITY')


def downgrade():
    op.drop_index('ix_error_logs_last_seen', table_name='error_logs')
    op.drop_index('ix_error_logs_resolved', table_name='error_logs')
    op.drop_index('ix_error_logs_fingerprint', table_name='error_logs')
    op.drop_table('error_logs')
