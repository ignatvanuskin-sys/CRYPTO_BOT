"""add performance indexes for leaderboard and lifecycle

Revision ID: 009
Revises: 008
"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None

def upgrade():
    # competitions(status, ends_at) for lifecycle
    try:
        op.create_index("ix_competitions_status_ends_at", "competitions", ["status", "ends_at"])
    except Exception:
        pass
    # paper_positions(competition_id, status) for finalize
    try:
        op.create_index("ix_paper_positions_competition_status", "paper_positions", ["competition_id", "status"])
    except Exception:
        pass
    # paper_positions(status) standalone for tp_sl_engine
    try:
        op.create_index("ix_paper_positions_status", "paper_positions", ["status"])
    except Exception:
        pass
    # market_snapshots updated_at already in 004, but ensure model sync
    # instruments(status)
    try:
        op.create_index("ix_instruments_status", "instruments", ["status"])
    except Exception:
        pass

def downgrade():
    for name, table in [
        ("ix_competitions_status_ends_at", "competitions"),
        ("ix_paper_positions_competition_status", "paper_positions"),
        ("ix_paper_positions_status", "paper_positions"),
        ("ix_instruments_status", "instruments"),
    ]:
        try:
            op.drop_index(name, table_name=table)
        except Exception:
            pass
