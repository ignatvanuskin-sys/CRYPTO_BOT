"""widen symbol columns for long BingX instruments

Revision ID: 006
Revises: 005
"""
from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("instruments", "symbol", type_=sa.String(length=40), existing_nullable=False)
    op.alter_column("paper_orders", "symbol", type_=sa.String(length=40), existing_nullable=False)
    op.alter_column("paper_positions", "symbol", type_=sa.String(length=40), existing_nullable=False)
    op.alter_column("market_snapshots", "symbol", type_=sa.String(length=40), existing_nullable=False)


def downgrade():
    op.alter_column("market_snapshots", "symbol", type_=sa.String(length=20), existing_nullable=False)
    op.alter_column("paper_positions", "symbol", type_=sa.String(length=20), existing_nullable=False)
    op.alter_column("paper_orders", "symbol", type_=sa.String(length=20), existing_nullable=False)
    op.alter_column("instruments", "symbol", type_=sa.String(length=20), existing_nullable=False)
