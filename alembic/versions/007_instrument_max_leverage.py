"""instrument max leverage per BingX

Revision ID: 007
Revises: 006
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("instruments", sa.Column("max_leverage", sa.Integer(), nullable=False, server_default="50"))
    # BingX real tiers: BTC/ETH allow up to 150-300, SOL up to 100, many alts up to 50
    # Set BTC/ETH to 300 as requested for high-leverage coins, SOL to 100
    op.execute("UPDATE instruments SET max_leverage = 300 WHERE symbol IN ('BTCUSDT', 'ETHUSDT')")
    op.execute("UPDATE instruments SET max_leverage = 100 WHERE symbol = 'SOLUSDT'")
    # For high-leverage meme coins that BingX allows 300, keep 50 for now — can be updated via sync

def downgrade():
    op.drop_column("instruments", "max_leverage")
