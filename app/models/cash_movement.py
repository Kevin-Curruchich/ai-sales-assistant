import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
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
    SALDO_INICIAL = "saldo_inicial"


# Unica fuente de verdad de que tipos restan del saldo. La repite quien lea el
# libro (CashMovementRepository via SQL, CashService.ledger via Python): un
# quinto tipo agregado a un lado y no al otro haria que el ultimo saldo del
# ledger difiera del running_balance calculado en SQL.
CASH_OUTFLOW_TYPES = frozenset({CashMovementType.SALIDA, CashMovementType.RETIRO_SOCIO})


class CashMovement(Base):
    """Libro de caja.

    El saldo acumulado NO se guarda: se calcula al leer con
    SUM(...) OVER (ORDER BY occurred_at).  Guardarlo como columna fue la
    fuente de desincronizacion en la hoja de calculo de la que viene este modelo.

    Limitacion conocida: un movimiento apunta a una sola venta.  Un cobro que
    salda varias se registra como varios movimientos.
    """

    __tablename__ = "cash_movements"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # VARCHAR + CHECK, no enum nativo de Postgres, mismo patron que
    # PAYMENT_METHOD_COLUMN (app/models/payment_method.py): un enum nativo es
    # schema-scoped (db_v2.cash_movement_type_enum y db_local.cash_movement_type_enum
    # son tipos distintos, lo que ya complico una copia entre schemas -- ver
    # docs/superpowers/handoffs/2026-09-23-piezas-2-3-agente.md) y ALTER TYPE ADD
    # VALUE pelea con el DDL transaccional de Alembic, mientras que quitar un valor
    # de un enum nativo es casi imposible. Nacio como enum nativo (8df5b1f79497) y
    # se convirtio aqui, en la misma revision que agrego saldo_inicial, porque la
    # tabla seguia vacia: la conversion era gratis hoy y deja de serlo en cuanto
    # Task 8 empiece a escribir filas.
    # length=20 a proposito: el valor mas largo hoy (aporte_socio, retiro_socio,
    # saldo_inicial) tiene 13 caracteres; un tipo de movimiento nuevo con nombre
    # largo no debe obligar a redimensionar la columna ademas de tocar el CHECK.
    type: Mapped[CashMovementType] = mapped_column(
        SQLEnum(
            CashMovementType,
            name="type",
            native_enum=False,
            length=20,
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
