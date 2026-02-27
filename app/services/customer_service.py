import uuid
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from app.models.customer import Customer
from app.repositories.customer_repository import CustomerRepository
from app.schemas.customer import CustomerCreate, CustomerUpdate


class CustomerService:
    def __init__(self, db: Session):
        self.repo = CustomerRepository(db)

    def get_all(self, search: Optional[str] = None, limit: int = 10, offset: int = 0) -> list[Customer]:
        return self.repo.get_all(search=search, limit=limit, offset=offset)

    def get_by_id(self, customer_id: uuid.UUID) -> Customer:
        customer = self.repo.get_by_id(customer_id)
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer with id {customer_id} not found",
            )
        return customer

    def create(self, data: CustomerCreate) -> Customer:
        customer = Customer(**data.model_dump())
        return self.repo.create(customer)

    def update(self, customer_id: uuid.UUID, data: CustomerUpdate) -> Customer:
        customer = self.get_by_id(customer_id)
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(customer, key, value)
        return self.repo.update(customer)

    def delete(self, customer_id: uuid.UUID) -> None:
        customer = self.get_by_id(customer_id)
        self.repo.delete(customer)

    def count(self, search: Optional[str] = None) -> int:
        return self.repo.count(search=search)
