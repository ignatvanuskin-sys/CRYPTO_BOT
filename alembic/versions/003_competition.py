"""competition + executions

Revision ID: 003
Revises: 002
Create Date: 2026-08-28
"""
from alembic import op
import sqlalchemy as sa

revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('competitions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('status', sa.Enum('UPCOMING','ACTIVE','FINISHED','CANCELLED', name='competition_status'), nullable=False),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('initial_balance', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('prize_pool', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('ranking_metric', sa.Text(), nullable=False),
        sa.Column('price_source', sa.Text(), nullable=False),
        sa.Column('market_type', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('competition_participants',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('competition_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('starting_equity', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('current_equity', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('unrealized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('roi', sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=True),
        sa.Column('joined_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['competition_id'], ['competitions.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('competition_id', 'user_id', name='uq_competition_user')
    )
    op.create_index('ix_cp_competition', 'competition_participants', ['competition_id'])
    op.create_index('ix_cp_user', 'competition_participants', ['user_id'])

    # add competition_id to paper_positions (nullable for legacy) — use batch for sqlite compat
    with op.batch_alter_table('paper_positions', recreate='always') as batch_op:
        batch_op.add_column(sa.Column('competition_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_pp_competition', ['competition_id'])
    # FK only for postgres (sqlite batch already handled, and sqlite doesn't support ALTER FK)
    try:
        op.create_foreign_key('fk_pp_competition', 'paper_positions', 'competitions', ['competition_id'], ['id'])
    except Exception:
        pass  # sqlite: skip, FK enforced via app logic

    op.create_table('executions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('position_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('competition_id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('side', sa.Text(), nullable=False),
        sa.Column('price_source', sa.Text(), nullable=False),
        sa.Column('market_type', sa.Text(), nullable=False),
        sa.Column('bid_price', sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column('ask_price', sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column('execution_price', sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column('quantity', sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column('notional', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('market_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('execution_reason', sa.Enum('OPEN','MANUAL_CLOSE','TAKE_PROFIT','STOP_LOSS', name='execution_reason'), nullable=False),
        sa.ForeignKeyConstraint(['competition_id'], ['competitions.id']),
        sa.ForeignKeyConstraint(['position_id'], ['paper_positions.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_exec_position', 'executions', ['position_id'])
    op.create_index('ix_exec_user_comp', 'executions', ['user_id', 'competition_id'])

    op.create_table('competition_leaderboard_snapshots',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('competition_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('equity', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('roi', sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('unrealized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['competition_id'], ['competitions.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('competition_id', 'user_id', name='uq_snapshot_comp_user')
    )
    op.create_index('ix_snapshot_comp_rank', 'competition_leaderboard_snapshots', ['competition_id', 'rank'])

    # seed default competition: Weekly Trading Cup #1 (via Python for sqlite/postgres compat)
    competitions = sa.table('competitions',
        sa.column('name', sa.Text),
        sa.column('status', sa.String),
        sa.column('starts_at', sa.DateTime),
        sa.column('ends_at', sa.DateTime),
        sa.column('initial_balance', sa.Numeric),
        sa.column('prize_pool', sa.Numeric),
        sa.column('ranking_metric', sa.Text),
        sa.column('price_source', sa.Text),
        sa.column('market_type', sa.Text),
        sa.column('created_at', sa.DateTime),
    )
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    op.bulk_insert(competitions, [
        {"name": "Weekly Trading Cup #1", "status": "ACTIVE", "starts_at": now, "ends_at": now + timedelta(days=7), "initial_balance": 10000, "prize_pool": 500, "ranking_metric": "ROI", "price_source": "BINGX", "market_type": "USD_M_PERPETUAL", "created_at": now},
    ])


def downgrade():
    op.drop_index('ix_snapshot_comp_rank', table_name='competition_leaderboard_snapshots')
    op.drop_table('competition_leaderboard_snapshots')
    op.drop_index('ix_exec_user_comp', table_name='executions')
    op.drop_index('ix_exec_position', table_name='executions')
    op.drop_table('executions')
    op.drop_constraint('fk_pp_competition', 'paper_positions', type_='foreignkey')
    op.drop_index('ix_pp_competition', table_name='paper_positions')
    op.drop_column('paper_positions', 'competition_id')
    op.drop_index('ix_cp_user', table_name='competition_participants')
    op.drop_index('ix_cp_competition', table_name='competition_participants')
    op.drop_table('competition_participants')
    op.drop_table('competitions')
    op.execute("DROP TYPE IF EXISTS competition_status")
    op.execute("DROP TYPE IF EXISTS execution_reason")
