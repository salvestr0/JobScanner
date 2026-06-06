"""Add closing_date to jobs table

Revision ID: a3f9c1e82b0d
Revises: e1a7c4d92f5b
Create Date: 2026-06-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'a3f9c1e82b0d'
down_revision = 'e1a7c4d92f5b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('closing_date', sa.String(32), nullable=True))


def downgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('closing_date')
