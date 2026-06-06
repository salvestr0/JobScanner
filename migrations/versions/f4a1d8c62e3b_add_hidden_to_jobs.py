"""Add hidden column to jobs table

Revision ID: f4a1d8c62e3b
Revises: b7d3f2e91a4c
Create Date: 2026-06-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'f4a1d8c62e3b'
down_revision = 'b7d3f2e91a4c'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hidden', sa.Boolean(), nullable=True, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('hidden')
