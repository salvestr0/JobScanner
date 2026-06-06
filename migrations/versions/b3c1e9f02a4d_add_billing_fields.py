"""add billing fields

Revision ID: b3c1e9f02a4d
Revises: f7ef5236638b
Create Date: 2026-06-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b3c1e9f02a4d'
down_revision = 'f7ef5236638b'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('stripe_customer_id',  sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('subscription_status', sa.String(length=32),  nullable=True))
    op.add_column('users', sa.Column('trial_ends_at',       sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column('users', 'trial_ends_at')
    op.drop_column('users', 'subscription_status')
    op.drop_column('users', 'stripe_customer_id')
