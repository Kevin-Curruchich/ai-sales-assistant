import uuid
from typing import Optional
from datetime import date
from fastapi import APIRouter, Depends, status
from app.api.dependencies import CurrentUser, PurchaseServiceDep, get_current_user
from app.schemas.purchase import (
    PurchaseCreate, PurchaseUpdate, PurchaseResponse, PaginatedPurchaseResponse,
)

router = APIRouter(
    prefix="/purchases",
    tags=["Purchases"],
    dependencies=[Depends(get_current_user)],
)


@router.get("", response_model=PaginatedPurchaseResponse)
def list_purchases(
    service: PurchaseServiceDep,
    status_filter: Optional[str] = None,
    supplier_name: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 10,
    offset: int = 0,
):
    items = service.get_all_enriched(
        status_filter=status_filter,
        supplier_name=supplier_name,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )
    total = service.count(
        status_filter=status_filter,
        supplier_name=supplier_name,
        start_date=start_date,
        end_date=end_date,
    )
    return {"data": items, "meta": {"total": total}}


@router.get("/{purchase_id}", response_model=PurchaseResponse)
def get_purchase(purchase_id: uuid.UUID, service: PurchaseServiceDep):
    return service.get_by_id_enriched(purchase_id)


@router.post("", response_model=PurchaseResponse, status_code=status.HTTP_201_CREATED)
def create_purchase(data: PurchaseCreate, service: PurchaseServiceDep, current_user: CurrentUser):
    return service.create(data, user_id=current_user.id)


@router.put("/{purchase_id}", response_model=PurchaseResponse)
def update_purchase(purchase_id: uuid.UUID, data: PurchaseUpdate, service: PurchaseServiceDep):
    return service.update(purchase_id, data)


@router.post("/{purchase_id}/confirm", response_model=PurchaseResponse)
def confirm_purchase(purchase_id: uuid.UUID, service: PurchaseServiceDep):
    return service.confirm(purchase_id)


@router.post("/{purchase_id}/cancel", response_model=PurchaseResponse)
def cancel_purchase(purchase_id: uuid.UUID, service: PurchaseServiceDep):
    return service.cancel(purchase_id)


@router.delete("/{purchase_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_purchase(purchase_id: uuid.UUID, service: PurchaseServiceDep):
    service.delete(purchase_id)
