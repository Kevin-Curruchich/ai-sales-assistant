import uuid
from datetime import date, datetime
from typing import Optional
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.models.payment_method import PAYMENT_METHOD_COLUMN, PaymentMethod


class Sale(Base):
    __tablename__ = "sales"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    is_payment_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Nullable a proposito: en los datos reales hay ventas pagadas sin fecha
    # registrada, y alguna con fecha anterior a la venta.  Sin CHECK.
    payment_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    payment_method: Mapped[Optional[PaymentMethod]] = mapped_column(
        PAYMENT_METHOD_COLUMN, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    customer: Mapped["Customer"] = relationship("Customer", back_populates="sales")
    user: Mapped["User"] = relationship("User", back_populates="sales")
    items: Mapped[list["SaleItem"]] = relationship(
        "SaleItem", back_populates="sale", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Sale(id={self.id}, customer_id={self.customer_id}, user_id={self.user_id}, "
            f"total={self.total}, is_payment_pending={self.is_payment_pending})>"
        )
