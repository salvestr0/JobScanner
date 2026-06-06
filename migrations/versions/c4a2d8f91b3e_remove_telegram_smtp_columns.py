"""Remove Telegram and SMTP credential columns from users table

Revision ID: c4a2d8f91b3e
Revises: b3c1e9f02a4d
Create Date: 2026-06-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'c4a2d8f91b3e'
down_revision = 'b3c1e9f02a4d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('telegram_bot_token')
        batch_op.drop_column('telegram_chat_id')
        batch_op.drop_column('email_from')
        batch_op.drop_column('email_password')


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email_password',     sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('email_from',         sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('telegram_chat_id',   sa.String(64),  nullable=True))
        batch_op.add_column(sa.Column('telegram_bot_token', sa.String(255), nullable=True))
