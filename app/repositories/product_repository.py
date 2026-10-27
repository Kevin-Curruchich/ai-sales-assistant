import uuid
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.product import Product
from app.models.purchase import Purchase, PurchaseItem
from app.models.sale import Sale
from app.models.sale_item import SaleItem


class ProductRepository:
    def count(self, search: Optional[str] = None, status: Optional[str] = None) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).select_from(Product)
        if search:
            stmt = stmt.where(
                Product.name.ilike(f"%{search}%") | Product.sku.ilike(f"%{search}%")
            )
        if status:
            stmt = stmt.where(Product.status == status)
        return self.db.execute(stmt).scalar()

    def __init__(self, db: Session):
        self.db = db

    def get_all(
        self,
        search: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Product]:
        stmt = select(Product)
        if search:
            stmt = stmt.where(
                Product.name.ilike(f"%{search}%") | Product.sku.ilike(f"%{search}%")
            )
        if status:
            stmt = stmt.where(Product.status == status)
        stmt = stmt.order_by(Product.name).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def get_all_active(self) -> list[Product]:
        """Get all active products without pagination, ordered by name."""
        stmt = select(Product).where(Product.status == "active").order_by(Product.name)
        return list(self.db.execute(stmt).scalars().all())

    def get_by_id(self, product_id: uuid.UUID) -> Optional[Product]:
        return self.db.get(Product, product_id)

    def get_by_id_for_update(self, product_id: uuid.UUID) -> Optional[Product]:
        """Como get_by_id, pero bloquea la fila hasta que termine la transaccion.

        Para leer el stock y decidir algo con el (p. ej. "no puede quedar
        negativo") sin que otra request lo cambie entre la lectura y la
        escritura.
        """
        stmt = (
            select(Product)
            .where(Product.id == product_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def add_to_stock(self, product_id: uuid.UUID, delta: Decimal) -> None:
        """Suma `delta` al stock (negativo resta) en la base: SET stock = stock + delta.

        No `product.stock += delta` en Python: eso escribe un valor ya calculado
        (`SET stock = 6`) a partir de una lectura que otra request puede haber
        dejado vieja, y una de las dos sumas se pierde. Aca la suma la hace
        Postgres sobre el valor vigente, con la fila bloqueada mientras tanto.
        `synchronize_session="fetch"` trae el valor nuevo al objeto en memoria.
        """
        self.db.execute(
            update(Product)
            .where(Product.id == product_id)
            .values(stock=Product.stock + delta)
            .execution_options(synchronize_session="fetch")
        )

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

    def inventory_value_by_product(self) -> dict[uuid.UUID, Decimal]:
        """Lo que queda en lotes confirmados, a su costo de compra."""
        from sqlalchemy import func

        stmt = (
            select(
                PurchaseItem.product_id,
                func.sum(PurchaseItem.remaining_quantity * PurchaseItem.unit_cost),
            )
            .join(Purchase, Purchase.id == PurchaseItem.purchase_id)
            .where(Purchase.status == "confirmed")
            .where(PurchaseItem.remaining_quantity > 0)
            .group_by(PurchaseItem.product_id)
        )
        return dict(self.db.execute(stmt).all())

    def receivable_by_product(self) -> dict[uuid.UUID, Decimal]:
        """Subtotal de los items en ventas que siguen sin cobrar."""
        from sqlalchemy import func

        stmt = (
            select(SaleItem.product_id, func.sum(SaleItem.subtotal))
            .join(Sale, Sale.id == SaleItem.sale_id)
            .where(Sale.is_payment_pending.is_(True))
            .group_by(SaleItem.product_id)
        )
        return dict(self.db.execute(stmt).all())

    def get_by_ids(self, ids: set[uuid.UUID]) -> list[Product]:
        if not ids:
            return []
        return list(
            self.db.execute(select(Product).where(Product.id.in_(ids))).scalars().all()
        )
