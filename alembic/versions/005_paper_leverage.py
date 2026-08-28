"""paper positions leverage

Revision ID: 005
Revises: 004
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "paper_positions",
        sa.Column("leverage", sa.Numeric(precision=10, scale=2), nullable=False, server_default="1"),
    )


def downgrade():
    op.drop_column("paper_positions", "leverage")
