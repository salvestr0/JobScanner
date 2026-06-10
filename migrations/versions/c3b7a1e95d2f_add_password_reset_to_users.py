"""Add password reset fields to users table

Revision ID: c3b7a1e95d2f
Revises: f4a1d8c62e3b
Create Date: 2026-06-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'c3b7a1e95d2f'
down_revision = 'f4a1d8c62e3b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reset_token', sa.String(64), nullable=True))
        batch_op.add_column(sa.Column('reset_token_expires', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('reset_token_expires')
        batch_op.drop_column('reset_token')
