"""Persist simulated paper-order history."""
from alembic import op
import sqlalchemy as sa

revision = "0001_paper_orders"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("paper_orders",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("order_type", sa.String(length=12), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("average_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("label", sa.String(length=16), nullable=False, server_default="SIMULATED"))
    op.create_index("ix_paper_orders_symbol", "paper_orders", ["symbol"])
    op.create_index("ix_paper_orders_timestamp", "paper_orders", ["timestamp"])

def downgrade():
    op.drop_index("ix_paper_orders_timestamp", table_name="paper_orders")
    op.drop_index("ix_paper_orders_symbol", table_name="paper_orders")
    op.drop_table("paper_orders")
