import uuid
from typing import Optional
from datetime import date
from sqlalchemy import select, func
from sqlalchemy.orm import Session, joinedload
from app.models.sale import Sale
from app.models.sale_item import SaleItem
from app.models.customer import Customer
import logging


class SaleRepository:
    def count(
        self,
        customer_id: Optional[uuid.UUID] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> int:
        from sqlalchemy import func
        stmt = select(func.count()).select_from(Sale)
        if customer_id:
            stmt = stmt.where(Sale.customer_id == customer_id)
        if start_date:
            stmt = stmt.where(Sale.date >= start_date)
        if end_date:
            stmt = stmt.where(Sale.date <= end_date)
        return self.db.execute(stmt).scalar()
    
    def __init__(self, db: Session):
        self.db = db

    def get_all(
        self,
        customer_id: Optional[uuid.UUID] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Sale]:
        stmt = (
            select(Sale)
            .options(
                joinedload(Sale.items).joinedload(SaleItem.product),
                joinedload(Sale.user),
                joinedload(Sale.customer)
            )
        )
        if customer_id:
            stmt = stmt.where(Sale.customer_id == customer_id)
        if start_date:
            stmt = stmt.where(Sale.date >= start_date)
        if end_date:
            stmt = stmt.where(Sale.date <= end_date)
        stmt = stmt.order_by(Sale.date.desc()).limit(limit).offset(offset)

        results = list(self.db.execute(stmt).unique().scalars().all())
        return results

    def get_by_id(self, sale_id: uuid.UUID) -> Optional[Sale]:
        stmt = (
            select(Sale)
            .options(
                joinedload(Sale.items).joinedload(SaleItem.product),
                joinedload(Sale.user),
                joinedload(Sale.customer)
            )
            .where(Sale.id == sale_id)
        )
        return self.db.execute(stmt).unique().scalar_one_or_none()

    def get_by_customer(self, customer_id: uuid.UUID) -> list[Sale]:
        stmt = (
            select(Sale)
            .where(Sale.customer_id == customer_id)
            .order_by(Sale.date.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def create(self, sale: Sale) -> Sale:
        self.db.add(sale)
        self.db.commit()
        self.db.refresh(sale)
        return sale

    def update(self, sale: Sale) -> Sale:
        self.db.commit()
        self.db.refresh(sale)
        return sale

    def delete(self, sale: Sale) -> None:
        self.db.delete(sale)
        self.db.commit()

    def get_sales_this_month(self) -> float:
        """Return total sales amount for the current month."""
        today = date.today()
        first_day = today.replace(day=1)
        stmt = select(func.coalesce(func.sum(Sale.total), 0.0)).where(
            Sale.date >= first_day, Sale.date <= today
        )
        result = self.db.execute(stmt).scalar()
        return float(result or 0.0)

    def get_recent_sales(self, limit: int = 5) -> list[Sale]:
        stmt = (
            select(Sale)
            .options(joinedload(Sale.items))
            .order_by(Sale.date.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).unique().scalars().all())
