"""Persist simulated order stop and target prices."""
from alembic import op
import sqlalchemy as sa

revision = "0002_paper_brackets"
down_revision = "0001_paper_orders"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("paper_orders", sa.Column("stop_loss", sa.Numeric(20, 8), nullable=True))
    op.add_column("paper_orders", sa.Column("take_profit", sa.Numeric(20, 8), nullable=True))

def downgrade():
    op.drop_column("paper_orders", "take_profit")
    op.drop_column("paper_orders", "stop_loss")
