"""add LIQUIDATION to execution_reason enum

Revision ID: 008
Revises: 007
"""
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None

def upgrade():
    # Postgres: add new enum value; SQLite ignores
    try:
        op.execute("ALTER TYPE execution_reason ADD VALUE IF NOT EXISTS 'LIQUIDATION'")
    except Exception:
        # SQLite or already exists — ignore
        pass

def downgrade():
    # Removing enum value requires recreating type — not needed for demo
    pass
