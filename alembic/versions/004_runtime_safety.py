"""shared market snapshots, simulated users and paper prizes

Revision ID: 004
Revises: 003
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("is_simulated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "market_snapshots",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("market_type", sa.String(length=16), nullable=False),
        sa.Column("bid", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("ask", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("last", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("exchange_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("bid > 0", name="ck_market_snapshot_bid_positive"),
        sa.CheckConstraint("ask > 0", name="ck_market_snapshot_ask_positive"),
        sa.CheckConstraint("ask >= bid", name="ck_market_snapshot_ask_ge_bid"),
        sa.PrimaryKeyConstraint("symbol"),
    )
    op.create_index("ix_market_snapshots_updated_at", "market_snapshots", ["updated_at"])

    op.create_table(
        "competition_prizes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("competition_id", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["competition_id"], ["competitions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("competition_id", "rank", name="uq_competition_prize_rank"),
    )
    op.create_index(
        "ix_competition_prize_competition",
        "competition_prizes",
        ["competition_id"],
    )


def downgrade():
    op.drop_index("ix_competition_prize_competition", table_name="competition_prizes")
    op.drop_table("competition_prizes")
    op.drop_index("ix_market_snapshots_updated_at", table_name="market_snapshots")
    op.drop_table("market_snapshots")
    op.drop_column("users", "is_simulated")
