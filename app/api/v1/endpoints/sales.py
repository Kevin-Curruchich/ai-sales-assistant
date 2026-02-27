import uuid
from typing import Optional
from datetime import date
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_current_user
from app.schemas.sale import SaleCreate, SaleUpdate, SaleResponse
from app.services.sale_service import SaleService

router = APIRouter(prefix="/sales", tags=["Sales"])


@router.get("")
def list_sales(
    customer_id: Optional[uuid.UUID] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 10,
    offset: int = 0,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = SaleService(db)
    items = service.get_all_enriched(
        customer_id=customer_id, start_date=start_date, end_date=end_date, limit=limit, offset=offset
    )
    total = service.count(customer_id=customer_id, start_date=start_date, end_date=end_date)
    return {"data": items, "meta": {"total": total}}


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
    return service.create(data, user_id=current_user.id)


@router.put("/{sale_id}", response_model=SaleResponse)
def update_sale(
    sale_id: uuid.UUID,
    data: SaleUpdate,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = SaleService(db)
    return service.update(sale_id, data)
