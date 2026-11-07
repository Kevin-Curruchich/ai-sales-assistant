import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from app.models.cash_movement import CashMovementType
from app.models.payment_method import PaymentMethod


class CashMovementCreate(BaseModel):
    """Movimiento manual. Sin venta ni compra: esas registran el suyo."""

    type: CashMovementType
    amount: Decimal = Field(gt=0)
    # Dia del negocio; la hora la pone `business_instant`.
    date: date
    payment_method: Optional[PaymentMethod] = None
    note: Optional[str] = None


class CashMovementResponse(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    type: CashMovementType
    # Siempre positivo; `type` dice si suma o resta.
    amount: Decimal
    # True para salidas y retiros del socio: el front no tiene que repetir la regla.
    is_outflow: bool
    payment_method: Optional[PaymentMethod] = None
    sale_id: Optional[uuid.UUID] = None
    purchase_id: Optional[uuid.UUID] = None
    note: Optional[str] = None
    # Saldo de caja inmediatamente despues de este movimiento.
    running_balance: Decimal
    created_at: datetime


class CashMovementsMeta(BaseModel):
    total: int


class PaginatedCashMovementResponse(BaseModel):
    data: list[CashMovementResponse]
    meta: CashMovementsMeta


class CashSummaryResponse(BaseModel):
    # Efectivo operativo disponible hoy.
    balance: Decimal
    # Lo que el negocio le debe al socio: aportes menos retiros.
    owner_balance: Decimal
