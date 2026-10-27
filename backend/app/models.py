from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class PaperOrderRecord(Base):
    __tablename__ = "paper_orders"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    order_type: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(24))
    average_price: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    fees: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    slippage: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    label: Mapped[str] = mapped_column(String(16), default="SIMULATED")

    def as_dict(self) -> dict:
        timestamp = self.timestamp.replace(tzinfo=timezone.utc) if self.timestamp.tzinfo is None else self.timestamp
        return {"id": self.id, "symbol": self.symbol, "side": self.side, "quantity": str(self.quantity), "order_type": self.order_type, "status": self.status, "average_price": str(self.average_price), "stop_loss": str(self.stop_loss) if self.stop_loss is not None else None, "take_profit": str(self.take_profit) if self.take_profit is not None else None, "fees": str(self.fees), "slippage": str(self.slippage), "realized_pnl": str(self.realized_pnl), "timestamp": timestamp.isoformat(), "label": self.label}

class PaperPositionRecord(Base):
    __tablename__ = "paper_positions"
    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    quote_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    unit_name: Mapped[str] = mapped_column(String(32), nullable=False)
    signed_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    average_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    fees_paid: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
