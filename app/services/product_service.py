import uuid
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from app.models.product import Product
from app.repositories.product_repository import ProductRepository
from app.schemas.product import ProductCreate, ProductUpdate


class ProductService:
    def count(self, search: Optional[str] = None, status_filter: Optional[str] = None) -> int:
        return self.repo.count(search=search, status=status_filter)
    def __init__(self, db: Session):
        self.repo = ProductRepository(db)

    def get_all(
        self, search: Optional[str] = None, status_filter: Optional[str] = None, limit: int = 10, offset: int = 0
    ) -> list[Product]:
        return self.repo.get_all(search=search, status=status_filter, limit=limit, offset=offset)

    def get_by_id(self, product_id: uuid.UUID) -> Product:
        product = self.repo.get_by_id(product_id)
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product with id {product_id} not found",
            )
        return product

    def create(self, data: ProductCreate) -> Product:
        # Check for duplicate SKU
        existing = self.repo.get_by_sku(data.sku)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Product with SKU '{data.sku}' already exists",
            )
        product = Product(**data.model_dump())
        return self.repo.create(product)

    def update(self, product_id: uuid.UUID, data: ProductUpdate) -> Product:
        product = self.get_by_id(product_id)
        update_data = data.model_dump(exclude_unset=True)

        # If SKU is being changed, check for duplicates
        if "sku" in update_data and update_data["sku"] != product.sku:
            existing = self.repo.get_by_sku(update_data["sku"])
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Product with SKU '{update_data['sku']}' already exists",
                )

        for key, value in update_data.items():
            setattr(product, key, value)
        return self.repo.update(product)

    def delete(self, product_id: uuid.UUID) -> None:
        product = self.get_by_id(product_id)
        self.repo.delete(product)
