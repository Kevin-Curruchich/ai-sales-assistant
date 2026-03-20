import uuid
from datetime import datetime
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

    def _format_datetime(self, value: datetime | str | None) -> tuple[Optional[str], Optional[str]]:
        if value is None:
            return None, None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value.strftime("%Y-%m-%d"), value.strftime("%d/%m/%Y")

    def _get_stock_alert(self, stock: int, min_stock: int) -> tuple[str, bool]:
        if stock <= 0:
            return "out_of_stock", True
        if stock <= min_stock:
            return "low_stock", True
        return "in_stock", False

    def format_product_dates(self, product: Product) -> dict:
        created_at, created_at_formatted = self._format_datetime(product.created_at)
        updated_at, updated_at_formatted = self._format_datetime(product.updated_at)
        stock_alert_status, should_reorder = self._get_stock_alert(product.stock, product.min_stock)

        return {
            "id": product.id,
            "sku": product.sku,
            "name": product.name,
            "description": product.description,
            "earning_mode": product.earning_mode,
            "earning_percent": product.earning_percent,
            "earning_fee_amount": product.earning_fee_amount,
            "stock": product.stock,
            "min_stock": product.min_stock,
            "status": product.status,
            "created_at": created_at,
            "updated_at": updated_at,
            "created_at_formatted": created_at_formatted,
            "updated_at_formatted": updated_at_formatted,
            "stock_alert_status": stock_alert_status,
            "should_reorder": should_reorder,
        }

    def get_all_with_formatted_dates(
        self, search: Optional[str] = None, status_filter: Optional[str] = None, limit: int = 10, offset: int = 0
    ) -> list[dict]:
        items = self.get_all(search=search, status_filter=status_filter, limit=limit, offset=offset)
        return [self.format_product_dates(item) for item in items]

    def get_by_id_with_formatted_dates(self, product_id: uuid.UUID) -> dict:
        return self.format_product_dates(self.get_by_id(product_id))

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
        payload = {
            "sku": data.sku,
            "name": data.name,
            "description": data.description,
            "earning_mode": data.earningMode,
            "earning_percent": data.earningPercent,
            "earning_fee_amount": data.earningFeeAmount,
            "stock": data.stock,
            "min_stock": data.min_stock,
            "status": data.status,
        }
        product = Product(**payload)
        return self.repo.create(product)

    def update(self, product_id: uuid.UUID, data: ProductUpdate) -> Product:
        product = self.get_by_id(product_id)
        update_data = data.model_dump(exclude_unset=True)
        field_map = {
            "earningMode": "earning_mode",
            "earningPercent": "earning_percent",
            "earningFeeAmount": "earning_fee_amount",
        }
        update_data = {field_map.get(k, k): v for k, v in update_data.items()}

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

        earning_mode_value = getattr(product.earning_mode, "value", product.earning_mode)

        if earning_mode_value == "percent" and product.earning_percent is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="earningPercent is required when earningMode is 'percent'",
            )
        if earning_mode_value == "fee" and product.earning_fee_amount is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="earningFeeAmount is required when earningMode is 'fee'",
            )

        return self.repo.update(product)

    def delete(self, product_id: uuid.UUID) -> None:
        product = self.get_by_id(product_id)
        self.repo.delete(product)
