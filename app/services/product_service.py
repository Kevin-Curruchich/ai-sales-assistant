import uuid
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models.purchase import Purchase, PurchaseItem
from app.models.product import Product
from app.repositories.product_repository import ProductRepository
from app.repositories.purchase_repository import PurchaseRepository
from app.schemas.product import ProductCreate, ProductUpdate, ProductForSaleResponse, AvailableLotInfo, LotsAvailabilityResponse


class ProductService:
    _MONEY = Decimal("0.01")
    _HUNDRED = Decimal("100")

    def count(self, search: Optional[str] = None, status_filter: Optional[str] = None) -> int:
        return self.repo.count(search=search, status=status_filter)

    def get_all_active_with_first_lot(self) -> list[ProductForSaleResponse]:
        """Get all active products with their first available FIFO lot.

        Returns products unpaginated with lot data for sales view dropdown.
        If no lots available, first_available_lot is None.
        """
        products = self.repo.get_all_active()
        result = []

        for product in products:
            lots = self.purchase_repo.get_fifo_available_lots(
                product_id=product.id,
                as_of_date=date.today(),
                lock_for_update=False,
            )

            first_lot = None
            if lots:
                lot = lots[0]
                cost_basis = Decimal(str(lot.unit_cost)).quantize(self._MONEY, rounding=ROUND_HALF_UP)
                suggested_price = self._compute_suggested_price(product, cost_basis)

                first_lot = AvailableLotInfo(
                    purchase_item_id=lot.id,
                    purchase_id=lot.purchase_id,
                    purchase_date=lot.purchase.date,
                    unit_cost=cost_basis,
                    remaining_quantity=lot.remaining_quantity,
                    suggested_unit_price=suggested_price,
                )

            result.append(
                ProductForSaleResponse(
                    id=product.id,
                    sku=product.sku,
                    name=product.name,
                    stock=product.stock,
                    earning_mode=getattr(product.earning_mode, "value", product.earning_mode),
                    earning_percent=product.earning_percent,
                    earning_fee_amount=product.earning_fee_amount,
                    status=product.status,
                    first_available_lot=first_lot,
                    has_more_lots=len(lots) > 1,
                )
            )

        return result

    def get_lots_availability(self, product_id: uuid.UUID, as_of_date: Optional[date] = None) -> LotsAvailabilityResponse:
        """Get all available FIFO lots for a product.

        Args:
            product_id: Product UUID
            as_of_date: Date to filter lots by (default: today)

        Returns:
            LotsAvailabilityResponse with all available lots

        Raises:
            HTTPException 404 if product not found
        """
        if as_of_date is None:
            as_of_date = date.today()

        product = self.repo.get_by_id(product_id)
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product with id {product_id} not found",
            )

        lots = self.purchase_repo.get_fifo_available_lots(
            product_id=product_id,
            as_of_date=as_of_date,
            lock_for_update=False,
        )

        lot_infos: list[AvailableLotInfo] = []
        for lot in lots:
            cost_basis = Decimal(str(lot.unit_cost)).quantize(self._MONEY, rounding=ROUND_HALF_UP)
            suggested_price = self._compute_suggested_price(product, cost_basis)

            lot_infos.append(
                AvailableLotInfo(
                    purchase_item_id=lot.id,
                    purchase_id=lot.purchase_id,
                    purchase_date=lot.purchase.date,
                    unit_cost=cost_basis,
                    remaining_quantity=lot.remaining_quantity,
                    suggested_unit_price=suggested_price,
                )
            )

        return LotsAvailabilityResponse(
            product_id=product.id,
            product_sku=product.sku,
            product_name=product.name,
            total_stock=product.stock,
            earning_mode=getattr(product.earning_mode, "value", product.earning_mode),
            earning_percent=product.earning_percent,
            earning_fee_amount=product.earning_fee_amount,
            lots=lot_infos,
        )

    def __init__(self, db: Session):
        self.db = db
        self.repo = ProductRepository(db)
        self.purchase_repo = PurchaseRepository(db)

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

    def _get_stock_alert(self, stock: Decimal, min_stock: int) -> tuple[str, bool]:
        if stock <= 0:
            return "out_of_stock", True
        if stock <= min_stock:
            return "low_stock", True
        return "in_stock", False

    def _compute_suggested_price(self, product: Product, cost_basis: Decimal) -> Decimal:
        mode = getattr(product.earning_mode, "value", product.earning_mode)
        if mode == "percent":
            percent = Decimal(str(product.earning_percent or 0))
            return (cost_basis * (Decimal("1") + (percent / self._HUNDRED))).quantize(
                self._MONEY,
                rounding=ROUND_HALF_UP,
            )

        fee_amount = Decimal(str(product.earning_fee_amount or 0))
        return (cost_basis + fee_amount).quantize(self._MONEY, rounding=ROUND_HALF_UP)

    def _get_highest_available_lot_cost(self, product_id: uuid.UUID) -> Optional[Decimal]:
        stmt = (
            select(func.max(PurchaseItem.unit_cost))
            .join(Purchase, Purchase.id == PurchaseItem.purchase_id)
            .where(Purchase.status == "confirmed")
            .where(PurchaseItem.product_id == product_id)
            .where(PurchaseItem.remaining_quantity > 0)
        )
        result = self.db.execute(stmt).scalar_one_or_none()
        if result is None:
            return None
        return Decimal(str(result)).quantize(self._MONEY, rounding=ROUND_HALF_UP)

    def _get_highest_available_lot_costs_bulk(self, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
        if not product_ids:
            return {}

        stmt = (
            select(
                PurchaseItem.product_id,
                func.max(PurchaseItem.unit_cost).label("max_cost"),
            )
            .join(Purchase, Purchase.id == PurchaseItem.purchase_id)
            .where(Purchase.status == "confirmed")
            .where(PurchaseItem.product_id.in_(product_ids))
            .where(PurchaseItem.remaining_quantity > 0)
            .group_by(PurchaseItem.product_id)
        )
        rows = self.db.execute(stmt).all()
        return {
            row.product_id: Decimal(str(row.max_cost)).quantize(self._MONEY, rounding=ROUND_HALF_UP)
            for row in rows
            if row.max_cost is not None
        }

    def format_product_dates(self, product: Product, highest_cost: Optional[Decimal] = None) -> dict:
        created_at, created_at_formatted = self._format_datetime(product.created_at)
        updated_at, updated_at_formatted = self._format_datetime(product.updated_at)
        stock_alert_status, should_reorder = self._get_stock_alert(product.stock, product.min_stock)
        suggested_price = self._compute_suggested_price(product, highest_cost) if highest_cost is not None else None

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
            "suggested_price": suggested_price,
        }

    def get_all_with_formatted_dates(
        self, search: Optional[str] = None, status_filter: Optional[str] = None, limit: int = 10, offset: int = 0
    ) -> list[dict]:
        items = self.get_all(search=search, status_filter=status_filter, limit=limit, offset=offset)
        highest_costs = self._get_highest_available_lot_costs_bulk([item.id for item in items])
        return [self.format_product_dates(item, highest_cost=highest_costs.get(item.id)) for item in items]

    def get_by_id_with_formatted_dates(self, product_id: uuid.UUID) -> dict:
        product = self.get_by_id(product_id)
        highest_cost = self._get_highest_available_lot_cost(product_id)
        return self.format_product_dates(product, highest_cost=highest_cost)

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
