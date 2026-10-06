import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, status

from app.api.dependencies import CustomerServiceDep, get_current_user
from app.schemas.customer import CustomerCreate, CustomerResponse, CustomerUpdate

logger = logging.getLogger("customers")

# Todas las rutas de clientes requieren usuario autenticado.
router = APIRouter(
    prefix="/customers",
    tags=["Customers"],
    dependencies=[Depends(get_current_user)],
)


@router.get("")
def list_customers(
    service: CustomerServiceDep,
    search: Optional[str] = None,
    limit: int = 10,
    offset: int = 0,
):
    formatted_items = service.get_all_with_formatted_dates(
        search=search, limit=limit, offset=offset
    )
    total = service.count(search=search)
    return {"data": formatted_items, "meta": {"total": total}}


@router.get("/{customer_id}")
def get_customer(customer_id: uuid.UUID, service: CustomerServiceDep):
    return service.get_customer_details(customer_id)


@router.post("", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
def create_customer(data: CustomerCreate, service: CustomerServiceDep):
    return service.create(data)


@router.put("/{customer_id}", response_model=CustomerResponse)
def update_customer(
    customer_id: uuid.UUID, data: CustomerUpdate, service: CustomerServiceDep
):
    return service.update(customer_id, data)
