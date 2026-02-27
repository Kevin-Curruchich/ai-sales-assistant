import uuid
from typing import Optional
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_current_user
from app.schemas.customer import CustomerCreate, CustomerUpdate, CustomerResponse
from app.services.customer_service import CustomerService

router = APIRouter(prefix="/customers", tags=["Customers"])


@router.get("")
def list_customers(
    search: Optional[str] = None,
    limit: int = 10,
    offset: int = 0,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = CustomerService(db)
    items = service.get_all(search=search, limit=limit, offset=offset)
    total = service.count(search=search)
    return {"data": items, "meta": {"total": total}}


@router.get("/{customer_id}", response_model=CustomerResponse)
def get_customer(
    customer_id: uuid.UUID,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = CustomerService(db)
    return service.get_by_id(customer_id)


@router.post("", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
def create_customer(
    data: CustomerCreate,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = CustomerService(db)
    return service.create(data)


@router.put("/{customer_id}", response_model=CustomerResponse)
def update_customer(
    customer_id: uuid.UUID,
    data: CustomerUpdate,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = CustomerService(db)
    return service.update(customer_id, data)
