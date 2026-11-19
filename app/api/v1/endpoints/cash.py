from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import CashServiceDep, get_current_user
from app.core.datetime_utils import (
    business_end_of_day,
    business_instant,
    business_midnight,
)
from app.models.cash_movement import CASH_OUTFLOW_TYPES
from app.schemas.cash import (
    CashMovementCreate,
    CashMovementResponse,
    CashSummaryResponse,
    PaginatedCashMovementResponse,
)

router = APIRouter(
    prefix="/cash",
    tags=["Cash"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/summary", response_model=CashSummaryResponse)
def get_cash_summary(service: CashServiceDep):
    return CashSummaryResponse(
        balance=service.running_balance(),
        owner_balance=service.owner_balance(),
    )


@router.post(
    "/movements",
    response_model=CashMovementResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_cash_movement(body: CashMovementCreate, service: CashServiceDep):
    """Registra un movimiento manual: hoy con la hora actual, un dia pasado a la
    medianoche del negocio (igual que `registrar_movimiento_caja`)."""
    m = service.record(
        occurred_at=business_instant(service.db, body.date),
        type=body.type,
        amount=body.amount,
        payment_method=body.payment_method,
        note=body.note,
        commit=True,
    )
    return CashMovementResponse(
        id=m.id,
        occurred_at=m.occurred_at,
        type=m.type,
        amount=m.amount,
        is_outflow=m.type in CASH_OUTFLOW_TYPES,
        payment_method=m.payment_method,
        note=m.note,
        running_balance=service.running_balance(as_of=m.occurred_at),
        created_at=m.created_at,
    )


@router.get("/movements", response_model=PaginatedCashMovementResponse)
def list_cash_movements(
    service: CashServiceDep,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Movimientos de caja del mas reciente al mas antiguo.

    Las fechas son dias calendario en la zona del negocio: `end_date` incluye
    ese dia completo.
    """
    start = business_midnight(service.db, start_date) if start_date else None
    end = business_end_of_day(service.db, end_date) if end_date else None
    rows = service.recent_movements(start=start, end=end, limit=limit, offset=offset)
    return {
        "data": [
            CashMovementResponse(
                id=m.id,
                occurred_at=m.occurred_at,
                type=m.type,
                amount=m.amount,
                is_outflow=m.type in CASH_OUTFLOW_TYPES,
                payment_method=m.payment_method,
                sale_id=m.sale_id,
                purchase_id=m.purchase_id,
                note=m.note,
                running_balance=balance,
                created_at=m.created_at,
            )
            for m, balance in rows
        ],
        "meta": {"total": service.count(start=start, end=end)},
    }
