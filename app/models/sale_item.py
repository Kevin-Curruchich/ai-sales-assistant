import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import Boolean, ForeignKey, DateTime, Numeric, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class SaleItem(Base):
    __tablename__ = "sale_items"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    sale_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    cost_basis_unit: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    gross_profit_unit: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    gross_profit_total: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    discount_percent: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    discount_amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    is_price_overridden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pricing_exception_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    sale: Mapped["Sale"] = relationship("Sale", back_populates="items")
    product: Mapped["Product"] = relationship("Product")
    allocations: Mapped[list["SaleItemLotAllocation"]] = relationship(
        "SaleItemLotAllocation", back_populates="sale_item", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<SaleItem(id={self.id}, product_id={self.product_id}, qty={self.quantity})>"
