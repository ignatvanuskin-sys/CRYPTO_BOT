"""initial

Revision ID: 001
Revises: 
Create Date: 2026-08-27
"""
from alembic import op
import sqlalchemy as sa

revision = '001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('username', sa.Text(), nullable=True),
        sa.Column('phone_number', sa.Text(), nullable=True),
        sa.Column('phone_verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rules_accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_banned', sa.Boolean(), nullable=False),
        sa.Column('ban_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('phone_number'),
        sa.UniqueConstraint('telegram_id')
    )
    op.create_table('weeks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('week_number', sa.Integer(), nullable=False),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.Enum('active','closing','closed', name='week_status'), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('assets',
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('base_asset', sa.Text(), nullable=False),
        sa.Column('quote_asset', sa.Text(), nullable=False),
        sa.Column('status', sa.Enum('active','delisted', name='asset_status'), nullable=False),
        sa.Column('is_quote_eligible', sa.Boolean(), nullable=False),
        sa.Column('last_24h_quote_volume', sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('symbol')
    )
    op.create_table('orders',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('week_id', sa.Integer(), nullable=False),
        sa.Column('asset_symbol', sa.String(length=32), nullable=False),
        sa.Column('side', sa.Enum('buy','sell', name='order_side'), nullable=False),
        sa.Column('notional_usd', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('qty', sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column('status', sa.Enum('pending','filled','rejected', name='order_status'), nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('executed_price', sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('price_source_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('idempotency_key', sa.Text(), nullable=False),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['asset_symbol'], ['assets.symbol']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['week_id'], ['weeks.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key')
    )
    op.create_table('transactions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('week_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.Enum('WEEKLY_GRANT','TRADE_BUY','TRADE_SELL','FORCED_CLOSE','ADJUSTMENT', name='transaction_type'), nullable=False),
        sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('balance_after', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('ref_order_id', sa.Integer(), nullable=True),
        sa.Column('idempotency_key', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('balance_after >= 0', name='ck_balance_after_non_negative'),
        sa.ForeignKeyConstraint(['ref_order_id'], ['orders.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['week_id'], ['weeks.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key')
    )
    op.create_index('ix_transactions_user_week', 'transactions', ['user_id', 'week_id'])
    op.create_index('ix_transactions_idempotency', 'transactions', ['idempotency_key'], unique=True)
    # partial unique for weekly grant
    op.execute("CREATE UNIQUE INDEX uq_weekly_grant ON transactions (user_id, week_id) WHERE type='WEEKLY_GRANT'")
    op.create_table('positions',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('week_id', sa.Integer(), nullable=False),
        sa.Column('asset_symbol', sa.String(length=32), nullable=False),
        sa.Column('qty', sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column('avg_entry_price', sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('qty >= 0', name='ck_position_qty_non_negative'),
        sa.ForeignKeyConstraint(['asset_symbol'], ['assets.symbol']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['week_id'], ['weeks.id']),
        sa.PrimaryKeyConstraint('user_id', 'week_id', 'asset_symbol')
    )
    op.create_table('leaderboard_snapshots',
        sa.Column('week_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('cash_balance', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('positions_value', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('total_equity', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['week_id'], ['weeks.id']),
        sa.PrimaryKeyConstraint('week_id', 'user_id')
    )
    op.create_table('prizes',
        sa.Column('week_id', sa.Integer(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('payout_status', sa.Enum('pending','verified','paid','rejected', name='payout_status'), nullable=False),
        sa.Column('verified_by_admin_id', sa.BigInteger(), nullable=True),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['week_id'], ['weeks.id']),
        sa.PrimaryKeyConstraint('week_id', 'rank')
    )

def downgrade():
    op.drop_table('prizes')
    op.drop_table('leaderboard_snapshots')
    op.drop_table('positions')
    op.drop_index('ix_transactions_idempotency', table_name='transactions')
    op.drop_index('ix_transactions_user_week', table_name='transactions')
    op.drop_table('transactions')
    op.drop_table('orders')
    op.drop_table('assets')
    op.drop_table('weeks')
    op.drop_table('users')
    op.execute("DROP TYPE IF EXISTS week_status")
    op.execute("DROP TYPE IF EXISTS asset_status")
    op.execute("DROP TYPE IF EXISTS transaction_type")
    op.execute("DROP TYPE IF EXISTS order_side")
    op.execute("DROP TYPE IF EXISTS order_status")
    op.execute("DROP TYPE IF EXISTS payout_status")
