import uuid
from typing import Optional
from datetime import date
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_current_user
from app.schemas.sale import (
    SaleCreate,
    SalePaymentStatusUpdate,
    SalePreviewResponse,
    SaleResponse,
    SaleUpdate,
    ProfitReportResponse,
)
from app.services.sale_service import SaleService

router = APIRouter(prefix="/sales", tags=["Sales"])


@router.get("")
def list_sales(
    customer_id: Optional[uuid.UUID] = None,
    product_id: Optional[uuid.UUID] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 10,
    offset: int = 0,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = SaleService(db)
    items = service.get_all_enriched(
        customer_id=customer_id,
        product_id=product_id,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )
    total = service.count(
        customer_id=customer_id,
        product_id=product_id,
        start_date=start_date,
        end_date=end_date,
    )
    return {"data": items, "meta": {"total": total}}


@router.get("/reports/profit", response_model=ProfitReportResponse)
def get_profit_report(
    group_by: str = "product",
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = SaleService(db)
    return service.get_profit_report(
        group_by=group_by,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )


@router.post("/preview", response_model=SalePreviewResponse, status_code=status.HTTP_200_OK)
def preview_sale(
    data: SaleCreate,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_user),
):
    """Dry-run FIFO lot allocation and pricing without writing to the database.

    Returns the exact lot split, cost basis, suggested and final price for
    every item so the frontend can show an audit breakdown before confirming.
    """
    service = SaleService(db)
    return service.preview_sale(data)


@router.get("/{sale_id}", response_model=SaleResponse)
def get_sale(
    sale_id: uuid.UUID,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = SaleService(db)
    return service.get_by_id_enriched(sale_id)


@router.post("", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
def create_sale(
    data: SaleCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    service = SaleService(db)
    return service.create_enriched(data, user_id=current_user.id)


@router.put("/{sale_id}", response_model=SaleResponse)
def update_sale(
    sale_id: uuid.UUID,
    data: SaleUpdate,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = SaleService(db)
    return service.update_enriched(sale_id, data)


@router.patch("/{sale_id}/payment-status", response_model=SaleResponse)
def update_sale_payment_status(
    sale_id: uuid.UUID,
    data: SalePaymentStatusUpdate,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = SaleService(db)
    return service.update_payment_status_enriched(sale_id, data)
