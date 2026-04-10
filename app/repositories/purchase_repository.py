import uuid
from typing import Optional
from datetime import date
from sqlalchemy import select, func
from sqlalchemy.orm import Session, joinedload, selectinload
from app.models.purchase import Purchase, PurchaseItem


class PurchaseRepository:
    def __init__(self, db: Session):
        self.db = db

    def count(
        self,
        status: Optional[str] = None,
        supplier_name: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> int:
        stmt = select(func.count()).select_from(Purchase)
        if status:
            stmt = stmt.where(Purchase.status == status)
        if supplier_name:
            stmt = stmt.where(Purchase.supplier_name.ilike(f"%{supplier_name}%"))
        if start_date:
            stmt = stmt.where(Purchase.date >= start_date)
        if end_date:
            stmt = stmt.where(Purchase.date <= end_date)
        return self.db.execute(stmt).scalar()

    def get_all(
        self,
        status: Optional[str] = None,
        supplier_name: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Purchase]:
        stmt = (
            select(Purchase)
            .options(
                joinedload(Purchase.items).joinedload(PurchaseItem.product),
                joinedload(Purchase.user),
            )
        )
        if status:
            stmt = stmt.where(Purchase.status == status)
        if supplier_name:
            stmt = stmt.where(Purchase.supplier_name.ilike(f"%{supplier_name}%"))
        if start_date:
            stmt = stmt.where(Purchase.date >= start_date)
        if end_date:
            stmt = stmt.where(Purchase.date <= end_date)
        stmt = stmt.order_by(Purchase.created_at.desc(), Purchase.date.desc()).limit(limit).offset(offset)
        return list(self.db.execute(stmt).unique().scalars().all())

    def get_by_id(self, purchase_id: uuid.UUID) -> Optional[Purchase]:
        stmt = (
            select(Purchase)
            .options(
                joinedload(Purchase.items).joinedload(PurchaseItem.product),
                joinedload(Purchase.user),
            )
            .where(Purchase.id == purchase_id)
        )
        return self.db.execute(stmt).unique().scalar_one_or_none()

    def get_by_reference_number(self, reference_number: str) -> Optional[Purchase]:
        stmt = select(Purchase).where(Purchase.reference_number == reference_number)
        return self.db.execute(stmt).scalar_one_or_none()

    def create(self, purchase: Purchase) -> Purchase:
        self.db.add(purchase)
        self.db.commit()
        self.db.refresh(purchase)
        return purchase

    def update(self, purchase: Purchase) -> Purchase:
        self.db.commit()
        self.db.refresh(purchase)
        return purchase

    def delete(self, purchase: Purchase) -> None:
        self.db.delete(purchase)
        self.db.commit()

    def get_fifo_available_lots(
        self,
        product_id: uuid.UUID,
        as_of_date: Optional[date] = None,
        lock_for_update: bool = False,
    ) -> list[PurchaseItem]:
        stmt = (
            select(PurchaseItem)
            .join(Purchase, Purchase.id == PurchaseItem.purchase_id)
            # Keep purchase eager-loaded without introducing outer joins in the locking query.
            .options(selectinload(PurchaseItem.purchase))
            .where(Purchase.status == "confirmed")
            .where(PurchaseItem.product_id == product_id)
            .where(PurchaseItem.remaining_quantity > 0)
            .order_by(Purchase.date.asc(), Purchase.created_at.asc(), PurchaseItem.created_at.asc())
        )
        if as_of_date is not None:
            stmt = stmt.where(Purchase.date <= as_of_date)
        if lock_for_update:
            stmt = stmt.with_for_update(of=PurchaseItem)
        return list(self.db.execute(stmt).scalars().all())
