import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING
from decimal import Decimal
from sqlalchemy import Date, ForeignKey, DateTime, Numeric, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

if TYPE_CHECKING:
    from app.models.sale_item import SaleItem


class SaleItemLotAllocation(Base):
    __tablename__ = "sale_item_lot_allocations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    sale_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sale_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity_allocated: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    unit_cost_snapshot: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    lot_purchase_date: Mapped[date] = mapped_column(Date, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    sale_item: Mapped["SaleItem"] = relationship("SaleItem", back_populates="allocations")

    def __repr__(self) -> str:
        return (
            f"<SaleItemLotAllocation(id={self.id}, sale_item_id={self.sale_item_id}, "
            f"purchase_item_id={self.purchase_item_id}, qty={self.quantity_allocated})>"
        )
