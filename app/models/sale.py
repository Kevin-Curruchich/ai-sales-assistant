import uuid
from datetime import date, datetime
from sqlalchemy import Date, DateTime, Float, ForeignKey, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


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
    total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

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
        return f"<Sale(id={self.id}, customer_id={self.customer_id}, user_id={self.user_id}, total={self.total})>"
