"""add job region fields

Revision ID: c91e7b4a6d20
Revises: a3f9c2e7b481
Create Date: 2026-06-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c91e7b4a6d20'
down_revision = 'a3f9c2e7b481'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('job_region', sa.String(length=16), nullable=True, server_default='sg'))

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('region', sa.String(length=16), nullable=True, server_default='sg'))

    op.execute("UPDATE user_settings SET job_region = 'sg' WHERE job_region IS NULL")
    op.execute("UPDATE jobs SET region = 'sg' WHERE region IS NULL")


def downgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('region')

    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        batch_op.drop_column('job_region')
