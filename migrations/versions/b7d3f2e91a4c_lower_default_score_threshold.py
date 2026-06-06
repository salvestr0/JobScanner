"""Lower default min_score_threshold from 40 to 30

Revision ID: b7d3f2e91a4c
Revises: a3f9c1e82b0d
Create Date: 2026-06-07 00:00:00.000000

"""
from alembic import op

revision = 'b7d3f2e91a4c'
down_revision = 'a3f9c1e82b0d'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE user_settings SET min_score_threshold = 30 WHERE min_score_threshold = 40")


def downgrade():
    op.execute("UPDATE user_settings SET min_score_threshold = 40 WHERE min_score_threshold = 30")
