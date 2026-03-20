import uuid
from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional
from sqlalchemy import Enum as SQLEnum, String, DateTime, Integer, Numeric, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, SCHEMA


class EarningMode(str, PyEnum):
    PERCENT = "percent"
    FEE = "fee"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    sku: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    earning_mode: Mapped[EarningMode] = mapped_column(
        SQLEnum(
            EarningMode,
            name="earning_mode_enum",
            schema=SCHEMA,
            native_enum=True,
            validate_strings=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=EarningMode.PERCENT,
        server_default=text("'percent'"),
    )
    earning_percent: Mapped[Optional[float]] = mapped_column(Numeric(10, 4), nullable=True)
    earning_fee_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Reorder point
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active") # "active" | "inactive"

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    customer_cycles: Mapped[list["CustomerProductCycle"]] = relationship(
        "CustomerProductCycle", back_populates="product", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Product(id={self.id}, sku='{self.sku}', name='{self.name}')>"
