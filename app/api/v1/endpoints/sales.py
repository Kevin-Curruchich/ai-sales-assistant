import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, status

from app.api.dependencies import CurrentUser, SaleServiceDep, get_current_user
from app.schemas.sale import (
    ProfitReportResponse,
    SaleCreate,
    SalePaymentStatusUpdate,
    SalePreviewResponse,
    SaleResponse,
    SaleUpdate,
)

router = APIRouter(
    prefix="/sales",
    tags=["Sales"],
    dependencies=[Depends(get_current_user)],
)


@router.get("")
def list_sales(
    service: SaleServiceDep,
    customer_id: Optional[uuid.UUID] = None,
    product_id: Optional[uuid.UUID] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    is_payment_pending: Optional[bool] = None,
    limit: int = 10,
    offset: int = 0,
):
    items = service.get_all_enriched(
        customer_id=customer_id,
        product_id=product_id,
        start_date=start_date,
        end_date=end_date,
        is_payment_pending=is_payment_pending,
        limit=limit,
        offset=offset,
    )
    filters = dict(
        customer_id=customer_id,
        product_id=product_id,
        start_date=start_date,
        end_date=end_date,
        is_payment_pending=is_payment_pending,
    )
    return {
        "data": items,
        "meta": {
            "total": service.count(**filters),
            # Monto de todas las ventas del filtro, no solo de esta pagina.
            "total_amount": service.sum_total(**filters),
        },
    }


@router.get("/reports/profit", response_model=ProfitReportResponse)
def get_profit_report(
    service: SaleServiceDep,
    group_by: str = "product",
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 100,
):
    return service.get_profit_report(
        group_by=group_by,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )


@router.post(
    "/preview", response_model=SalePreviewResponse, status_code=status.HTTP_200_OK
)
def preview_sale(data: SaleCreate, service: SaleServiceDep):
    """Dry-run FIFO lot allocation and pricing without writing to the database.

    Returns the exact lot split, cost basis, suggested and final price for
    every item so the frontend can show an audit breakdown before confirming.
    """
    return service.preview_sale(data)


@router.get("/{sale_id}", response_model=SaleResponse)
def get_sale(sale_id: uuid.UUID, service: SaleServiceDep):
    return service.get_by_id_enriched(sale_id)


@router.post("", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
def create_sale(data: SaleCreate, service: SaleServiceDep, current_user: CurrentUser):
    return service.create_enriched(data, user_id=current_user.id)


@router.put("/{sale_id}", response_model=SaleResponse)
def update_sale(sale_id: uuid.UUID, data: SaleUpdate, service: SaleServiceDep):
    return service.update_enriched(sale_id, data)


@router.patch("/{sale_id}/payment-status", response_model=SaleResponse)
def update_sale_payment_status(
    sale_id: uuid.UUID,
    data: SalePaymentStatusUpdate,
    service: SaleServiceDep,
):
    return service.update_payment_status_enriched(sale_id, data)
