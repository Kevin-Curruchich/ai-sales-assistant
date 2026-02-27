import uuid
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.product import Product


class ProductRepository:
    def count(self, search: Optional[str] = None, status: Optional[str] = None) -> int:
        from sqlalchemy import func
        stmt = select(func.count()).select_from(Product)
        if search:
            stmt = stmt.where(
                Product.name.ilike(f"%{search}%")
                | Product.sku.ilike(f"%{search}%")
            )
        if status:
            stmt = stmt.where(Product.status == status)
        return self.db.execute(stmt).scalar()
    def __init__(self, db: Session):
        self.db = db

    def get_all(
        self, search: Optional[str] = None, status: Optional[str] = None, limit: int = 10, offset: int = 0
    ) -> list[Product]:
        stmt = select(Product)
        if search:
            stmt = stmt.where(
                Product.name.ilike(f"%{search}%")
                | Product.sku.ilike(f"%{search}%")
            )
        if status:
            stmt = stmt.where(Product.status == status)
        stmt = stmt.order_by(Product.name).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def get_by_id(self, product_id: uuid.UUID) -> Optional[Product]:
        return self.db.get(Product, product_id)

    def get_by_sku(self, sku: str) -> Optional[Product]:
        stmt = select(Product).where(Product.sku == sku)
        return self.db.execute(stmt).scalar_one_or_none()

    def create(self, product: Product) -> Product:
        self.db.add(product)
        self.db.commit()
        self.db.refresh(product)
        return product

    def update(self, product: Product) -> Product:
        self.db.commit()
        self.db.refresh(product)
        return product

    def delete(self, product: Product) -> None:
        self.db.delete(product)
        self.db.commit()
