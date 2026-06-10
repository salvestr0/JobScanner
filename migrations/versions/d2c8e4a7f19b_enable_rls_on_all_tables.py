"""Enable row level security on all public tables

Supabase exposes the public schema through PostgREST. Without RLS, anyone
holding the project's anon key can read/write these tables via the Data API.
Enabling RLS with no policies blocks all PostgREST access. The Flask app is
unaffected: it connects as the table-owning role, which bypasses RLS.

Revision ID: d2c8e4a7f19b
Revises: eb29b9ae6689
Create Date: 2026-06-10
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd2c8e4a7f19b'
down_revision = 'eb29b9ae6689'
branch_labels = None
depends_on = None

TABLES = [
    'users',
    'user_profiles',
    'user_settings',
    'jobs',
    'application_statuses',
    'seen_jobs',
    'scan_history',
    'search_modes',
    'alembic_version',
]


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return  # SQLite (local dev) has no RLS
    for table in TABLES:
        op.execute(f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY')


def downgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    for table in TABLES:
        op.execute(f'ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY')
