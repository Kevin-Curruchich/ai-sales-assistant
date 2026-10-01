from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db
from app.core.datetime_utils import business_end_of_day, business_midnight
from app.models.cash_movement import CASH_OUTFLOW_TYPES
from app.schemas.cash import (
    CashMovementResponse,
    CashSummaryResponse,
    PaginatedCashMovementResponse,
)
from app.services.cash_service import CashService

router = APIRouter(prefix="/cash", tags=["Cash"])


@router.get("/summary", response_model=CashSummaryResponse)
def get_cash_summary(
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = CashService(db)
    return CashSummaryResponse(
        balance=service.running_balance(),
        owner_balance=service.owner_balance(),
    )


@router.get("/movements", response_model=PaginatedCashMovementResponse)
def list_cash_movements(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    """Movimientos de caja del mas reciente al mas antiguo.

    Las fechas son dias calendario en la zona del negocio: `end_date` incluye
    ese dia completo.
    """
    service = CashService(db)
    start = business_midnight(start_date) if start_date else None
    end = business_end_of_day(end_date) if end_date else None
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
