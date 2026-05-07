import uuid
from datetime import date, datetime
from typing import Optional
from decimal import Decimal
from sqlalchemy import Date, DateTime, Float, Integer, Numeric, ForeignKey, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class CustomerProductCycle(Base):
    """Tracks the replenishment cycle for each customer + product pair.
    
    Updated automatically every time a sale is created.  The avg_interval_days
    is recalculated from the history of purchases for that pair, and
    estimated_next_purchase is projected from the most recent purchase.
    """
    __tablename__ = "customer_product_cycles"
    __table_args__ = (
        UniqueConstraint("customer_id", "product_id", name="uq_customer_product"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)

    avg_interval_days: Mapped[Optional[int]] = mapped_column(Integer)  # None until ≥2 purchases
    estimated_next_purchase: Mapped[Optional[date]] = mapped_column(Date)
    last_purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    last_quantity: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    total_purchases: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    customer: Mapped["Customer"] = relationship("Customer", back_populates="product_cycles")
    product: Mapped["Product"] = relationship("Product", back_populates="customer_cycles")

    def __repr__(self) -> str:
        return (
            f"<CustomerProductCycle(customer_id={self.customer_id}, "
            f"product_id={self.product_id}, "
            f"avg_interval={self.avg_interval_days}d)>"
        )
