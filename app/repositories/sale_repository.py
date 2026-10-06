import logging
import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.datetime_utils import business_today
from app.models.customer import Customer
from app.models.sale import Sale
from app.models.sale_item import SaleItem


class SaleRepository:
    @staticmethod
    def _apply_filters(
        stmt,
        customer_id: Optional[uuid.UUID] = None,
        product_id: Optional[uuid.UUID] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        is_payment_pending: Optional[bool] = None,
    ):
        """Mismos filtros para la pagina, el conteo y la suma del listado."""
        if customer_id:
            stmt = stmt.where(Sale.customer_id == customer_id)
        if product_id:
            stmt = stmt.where(Sale.items.any(SaleItem.product_id == product_id))
        if start_date:
            stmt = stmt.where(Sale.date >= start_date)
        if end_date:
            stmt = stmt.where(Sale.date <= end_date)
        if is_payment_pending is not None:
            stmt = stmt.where(Sale.is_payment_pending == is_payment_pending)
        return stmt

    def count(self, **filters) -> int:
        stmt = self._apply_filters(select(func.count()).select_from(Sale), **filters)
        return self.db.execute(stmt).scalar()

    def sum_total(self, **filters) -> Decimal:
        """Suma de `Sale.total` de las ventas que cumplen los filtros."""
        stmt = self._apply_filters(
            select(func.coalesce(func.sum(Sale.total), 0)), **filters
        )
        return Decimal(str(self.db.execute(stmt).scalar_one()))

    def __init__(self, db: Session):
        self.db = db

    def get_all(
        self,
        customer_id: Optional[uuid.UUID] = None,
        product_id: Optional[uuid.UUID] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        is_payment_pending: Optional[bool] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Sale]:
        stmt = select(Sale).options(
            joinedload(Sale.items).joinedload(SaleItem.product),
            joinedload(Sale.user),
            joinedload(Sale.customer),
        )
        stmt = self._apply_filters(
            stmt,
            customer_id=customer_id,
            product_id=product_id,
            start_date=start_date,
            end_date=end_date,
            is_payment_pending=is_payment_pending,
        )
        stmt = (
            stmt.order_by(Sale.created_at.desc(), Sale.date.desc())
            .limit(limit)
            .offset(offset)
        )

        results = list(self.db.execute(stmt).unique().scalars().all())
        return results

    def get_by_id(self, sale_id: uuid.UUID) -> Optional[Sale]:
        stmt = (
            select(Sale)
            .options(
                joinedload(Sale.items).joinedload(SaleItem.product),
                joinedload(Sale.user),
                joinedload(Sale.customer),
            )
            .where(Sale.id == sale_id)
        )
        return self.db.execute(stmt).unique().scalar_one_or_none()

    def get_by_customer(self, customer_id: uuid.UUID) -> list[Sale]:
        stmt = (
            select(Sale)
            .where(Sale.customer_id == customer_id)
            .order_by(Sale.created_at.desc(), Sale.date.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_recent_items_for_customer_product(
        self, customer_id: uuid.UUID, product_id: uuid.UUID, limit: int = 3
    ) -> list[SaleItem]:
        """Los items mas recientes de ese par, el mas nuevo primero."""
        stmt = (
            select(SaleItem)
            .join(Sale, Sale.id == SaleItem.sale_id)
            .where(Sale.customer_id == customer_id, SaleItem.product_id == product_id)
            .order_by(Sale.date.desc(), Sale.created_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def create(self, sale: Sale) -> Sale:
        # No commitea: SaleService.create compone esto con CashService.record
        # en una sola transaccion (ver Task 4). El caller es quien decide
        # cuando cerrar el commit.
        self.db.add(sale)
        self.db.flush()
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
        today = business_today()
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
            .order_by(Sale.created_at.desc(), Sale.date.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).unique().scalars().all())
