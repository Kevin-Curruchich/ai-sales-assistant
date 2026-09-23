import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    Date,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Numeric,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.payment_method import PAYMENT_METHOD_COLUMN, PaymentMethod


class CashMovementType(str, PyEnum):
    ENTRADA = "entrada"
    SALIDA = "salida"
    APORTE_SOCIO = "aporte_socio"
    RETIRO_SOCIO = "retiro_socio"


class CashMovement(Base):
    """Libro de caja.

    El saldo acumulado NO se guarda: se calcula al leer con
    SUM(...) OVER (ORDER BY movement_date).  Guardarlo como columna fue la
    fuente de desincronizacion en la hoja de calculo de la que viene este modelo.

    Limitacion conocida: un movimiento apunta a una sola venta.  Un cobro que
    salda varias se registra como varios movimientos.
    """

    __tablename__ = "cash_movements"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    movement_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    type: Mapped[CashMovementType] = mapped_column(
        SQLEnum(
            CashMovementType,
            name="cash_movement_type_enum",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payment_method: Mapped[Optional[PaymentMethod]] = mapped_column(
        PAYMENT_METHOD_COLUMN, nullable=True
    )
    sale_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("sales.id", ondelete="SET NULL"), nullable=True, index=True
    )
    purchase_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("purchases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<CashMovement(id={self.id}, type={self.type}, amount={self.amount})>"
