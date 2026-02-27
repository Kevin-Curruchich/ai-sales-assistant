import uuid
from typing import Optional
from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from app.models.customer_product_cycle import CustomerProductCycle


class CustomerProductCycleRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_customer_and_product(
        self, customer_id: uuid.UUID, product_id: uuid.UUID
    ) -> Optional[CustomerProductCycle]:
        stmt = select(CustomerProductCycle).where(
            CustomerProductCycle.customer_id == customer_id,
            CustomerProductCycle.product_id == product_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_all_by_customer(self, customer_id: uuid.UUID) -> list[CustomerProductCycle]:
        stmt = (
            select(CustomerProductCycle)
            .options(joinedload(CustomerProductCycle.product))
            .where(CustomerProductCycle.customer_id == customer_id)
            .order_by(CustomerProductCycle.estimated_next_purchase.asc().nullslast())
        )
        return list(self.db.execute(stmt).unique().scalars().all())

    def get_all_with_estimation(self) -> list[CustomerProductCycle]:
        """Get all cycles that have an estimated next purchase date, with customer and product loaded."""
        stmt = (
            select(CustomerProductCycle)
            .options(
                joinedload(CustomerProductCycle.customer),
                joinedload(CustomerProductCycle.product),
            )
            .where(CustomerProductCycle.estimated_next_purchase.isnot(None))
            .order_by(CustomerProductCycle.estimated_next_purchase.asc())
        )
        return list(self.db.execute(stmt).unique().scalars().all())

    def create(self, cycle: CustomerProductCycle) -> CustomerProductCycle:
        self.db.add(cycle)
        self.db.commit()
        self.db.refresh(cycle)
        return cycle

    def update(self, cycle: CustomerProductCycle) -> CustomerProductCycle:
        self.db.commit()
        self.db.refresh(cycle)
        return cycle
