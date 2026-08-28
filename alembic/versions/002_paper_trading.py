"""paper trading: accounts, instruments, ledger, paper positions/orders

Revision ID: 002
Revises: 001
Create Date: 2026-08-28
"""
from alembic import op
import sqlalchemy as sa

revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None

def upgrade():
    # trading_accounts
    op.create_table('trading_accounts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False),
        sa.Column('initial_balance', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('cash_balance', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('equity', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('margin_used', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('available_margin', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('unrealized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('total_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id')
    )
    op.create_table('instruments',
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('base_asset', sa.Text(), nullable=False),
        sa.Column('quote_asset', sa.Text(), nullable=False),
        sa.Column('status', sa.Enum('active','delisted', name='instrument_status'), nullable=False),
        sa.Column('price_precision', sa.Integer(), nullable=False),
        sa.Column('quantity_precision', sa.Integer(), nullable=False),
        sa.Column('min_quantity', sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column('max_quantity', sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('symbol')
    )
    op.create_table('account_ledger',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.Enum('INITIAL_BALANCE','TRADE_OPEN','TRADE_CLOSE','FEE','ADJUSTMENT', name='ledger_type'), nullable=False),
        sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('balance_after', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('reference_type', sa.Text(), nullable=True),
        sa.Column('reference_id', sa.Text(), nullable=True),
        sa.Column('idempotency_key', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('balance_after >= 0', name='ck_ledger_balance_after_non_negative'),
        sa.ForeignKeyConstraint(['account_id'], ['trading_accounts.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key')
    )
    op.create_index('ix_ledger_account', 'account_ledger', ['account_id'])
    op.create_index('ix_ledger_idempotency', 'account_ledger', ['idempotency_key'], unique=True)

    op.create_table('paper_positions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('side', sa.Enum('LONG','SHORT', name='paper_position_side'), nullable=False),
        sa.Column('status', sa.Enum('OPEN','CLOSING','CLOSED','CANCELLED', name='paper_position_status'), nullable=False),
        sa.Column('quantity', sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column('entry_price', sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column('current_price', sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column('notional', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('take_profit', sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column('stop_loss', sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('unrealized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('fee_open', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('fee_close', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('quantity > 0', name='ck_paper_position_qty_positive'),
        sa.ForeignKeyConstraint(['account_id'], ['trading_accounts.id']),
        sa.ForeignKeyConstraint(['symbol'], ['instruments.symbol']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_paper_positions_account_status', 'paper_positions', ['account_id', 'status'])
    op.create_index('ix_paper_positions_symbol', 'paper_positions', ['symbol'])

    op.create_table('paper_orders',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('position_id', sa.Integer(), nullable=True),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('side', sa.Enum('LONG','SHORT', name='paper_order_side'), nullable=False),
        sa.Column('order_type', sa.Enum('MARKET', name='paper_order_type'), nullable=False),
        sa.Column('quantity', sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column('requested_price', sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column('executed_price', sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column('status', sa.Enum('PENDING','FILLED','REJECTED', name='paper_order_status'), nullable=False),
        sa.Column('reduce_only', sa.Boolean(), nullable=False),
        sa.Column('idempotency_key', sa.Text(), nullable=False),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['trading_accounts.id']),
        sa.ForeignKeyConstraint(['position_id'], ['paper_positions.id']),
        sa.ForeignKeyConstraint(['symbol'], ['instruments.symbol']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key')
    )
    op.create_table('audit_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('entity_type', sa.Text(), nullable=True),
        sa.Column('entity_id', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )
    # seed instruments: BTCUSDT, ETHUSDT, SOLUSDT — via Python for sqlite/postgres compat
    instruments = sa.table('instruments',
        sa.column('symbol', sa.String),
        sa.column('base_asset', sa.Text),
        sa.column('quote_asset', sa.Text),
        sa.column('status', sa.String),
        sa.column('price_precision', sa.Integer),
        sa.column('quantity_precision', sa.Integer),
        sa.column('min_quantity', sa.Numeric),
        sa.column('max_quantity', sa.Numeric),
        sa.column('created_at', sa.DateTime),
    )
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    op.bulk_insert(instruments, [
        {"symbol": "BTCUSDT", "base_asset": "BTC", "quote_asset": "USDT", "status": "active", "price_precision": 2, "quantity_precision": 6, "min_quantity": 0.000001, "max_quantity": None, "created_at": now},
        {"symbol": "ETHUSDT", "base_asset": "ETH", "quote_asset": "USDT", "status": "active", "price_precision": 2, "quantity_precision": 6, "min_quantity": 0.000001, "max_quantity": None, "created_at": now},
        {"symbol": "SOLUSDT", "base_asset": "SOL", "quote_asset": "USDT", "status": "active", "price_precision": 2, "quantity_precision": 6, "min_quantity": 0.000001, "max_quantity": None, "created_at": now},
    ])


def downgrade():
    op.drop_table('audit_logs')
    op.drop_table('paper_orders')
    op.drop_index('ix_paper_positions_symbol', table_name='paper_positions')
    op.drop_index('ix_paper_positions_account_status', table_name='paper_positions')
    op.drop_table('paper_positions')
    op.drop_index('ix_ledger_idempotency', table_name='account_ledger')
    op.drop_index('ix_ledger_account', table_name='account_ledger')
    op.drop_table('account_ledger')
    op.drop_table('instruments')
    op.drop_table('trading_accounts')
    op.execute("DROP TYPE IF EXISTS instrument_status")
    op.execute("DROP TYPE IF EXISTS ledger_type")
    op.execute("DROP TYPE IF EXISTS paper_order_side")
    op.execute("DROP TYPE IF EXISTS paper_order_type")
    op.execute("DROP TYPE IF EXISTS paper_order_status")
    op.execute("DROP TYPE IF EXISTS paper_position_side")
    op.execute("DROP TYPE IF EXISTS paper_position_status")
