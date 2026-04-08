import uuid
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.customer import Customer


class CustomerRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, search: Optional[str] = None, limit: int = 10, offset: int = 0) -> list[Customer]:
        stmt = select(Customer)
        if search:
            stmt = stmt.where(
                Customer.name.ilike(f"%{search}%")
                | Customer.company.ilike(f"%{search}%")
                | Customer.email.ilike(f"%{search}%")
            )
        stmt = stmt.order_by(Customer.created_at.desc()).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def get_by_id(self, customer_id: uuid.UUID) -> Optional[Customer]:
        return self.db.get(Customer, customer_id)

    def create(self, customer: Customer) -> Customer:
        self.db.add(customer)
        self.db.commit()
        self.db.refresh(customer)
        return customer

    def update(self, customer: Customer) -> Customer:
        self.db.commit()
        self.db.refresh(customer)
        return customer

    def delete(self, customer: Customer) -> None:
        self.db.delete(customer)
        self.db.commit()

    def count(self, search: Optional[str] = None) -> int:
        from sqlalchemy import func
        stmt = select(func.count()).select_from(Customer)
        if search:
            stmt = stmt.where(
                Customer.name.ilike(f"%{search}%")
                | Customer.company.ilike(f"%{search}%")
                | Customer.email.ilike(f"%{search}%")
            )
        return self.db.execute(stmt).scalar()
