"""Add google_id to users and make password_hash nullable

Revision ID: e1a7c4d92f5b
Revises: c4a2d8f91b3e
Create Date: 2026-06-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'e1a7c4d92f5b'
down_revision = 'c4a2d8f91b3e'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('google_id', sa.String(255), nullable=True))
        batch_op.alter_column('password_hash', existing_type=sa.String(255), nullable=True)
        batch_op.create_unique_constraint('uq_users_google_id', ['google_id'])


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('uq_users_google_id', type_='unique')
        batch_op.alter_column('password_hash', existing_type=sa.String(255), nullable=False)
        batch_op.drop_column('google_id')
