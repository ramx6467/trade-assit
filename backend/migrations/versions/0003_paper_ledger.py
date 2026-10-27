"""Add per-currency simulated positions and execution accounting."""
from alembic import op
import sqlalchemy as sa

revision = "0003_paper_ledger"
down_revision = "0002_paper_brackets"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("paper_orders", sa.Column("fees", sa.Numeric(20, 8), nullable=False, server_default="0"))
    op.add_column("paper_orders", sa.Column("slippage", sa.Numeric(20, 8), nullable=False, server_default="0"))
    op.add_column("paper_orders", sa.Column("realized_pnl", sa.Numeric(20, 8), nullable=False, server_default="0"))
    op.create_table("paper_positions",
        sa.Column("symbol", sa.String(length=24), primary_key=True),
        sa.Column("quote_currency", sa.String(length=8), nullable=False),
        sa.Column("unit_name", sa.String(length=32), nullable=False),
        sa.Column("signed_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("average_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("stop_loss", sa.Numeric(20, 8), nullable=True),
        sa.Column("take_profit", sa.Numeric(20, 8), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("fees_paid", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))

def downgrade():
    op.drop_table("paper_positions")
    op.drop_column("paper_orders", "realized_pnl")
    op.drop_column("paper_orders", "slippage")
    op.drop_column("paper_orders", "fees")
