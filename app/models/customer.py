import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    company: Mapped[Optional[str]] = mapped_column(String(255))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    address: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    sales: Mapped[list["Sale"]] = relationship(
        "Sale", back_populates="customer", cascade="all, delete-orphan"
    )
    product_cycles: Mapped[list["CustomerProductCycle"]] = relationship(
        "CustomerProductCycle", back_populates="customer", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Customer(id={self.id}, name='{self.name}')>"
