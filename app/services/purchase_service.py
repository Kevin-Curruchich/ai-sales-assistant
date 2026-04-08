import uuid
from typing import Optional
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from app.models.purchase import Purchase, PurchaseItem
from app.repositories.purchase_repository import PurchaseRepository
from app.repositories.product_repository import ProductRepository
from app.services.sale_service import SaleService
from app.schemas.purchase import (
    PurchaseCreate, PurchaseUpdate, PurchaseItemResponse, PurchaseResponse,
)


class PurchaseService:
    def __init__(self, db: Session):
        self.repo = PurchaseRepository(db)
        self.product_repo = ProductRepository(db)
        self.sale_service = SaleService(db)
        self.db = db

    def _money(self, value: Decimal | float | int | None) -> Decimal:
        if value is None:
            return Decimal("0.00")
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _format_datetime(self, value: datetime | str | None) -> tuple[Optional[str], Optional[str]]:
        if value is None:
            return None, None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value.strftime("%Y-%m-%d"), value.strftime("%d/%m/%Y")

    def _to_purchase_response(self, purchase: Purchase) -> PurchaseResponse:
        user = purchase.user
        created_at, created_at_formatted = self._format_datetime(purchase.created_at)
        updated_at, updated_at_formatted = self._format_datetime(purchase.updated_at)

        items = []
        for item in purchase.items:
            product = item.product
            items.append(PurchaseItemResponse(
                id=item.id,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_cost=self._money(item.unit_cost),
                subtotal=self._money(item.subtotal),
                product_name=product.name if product else "",
                product_sku=product.sku if product else "",
                product_earning_mode=product.earning_mode if product else "percent",
                product_earning_percent=product.earning_percent if product else None,
                product_earning_fee_amount=product.earning_fee_amount if product else None,
                product_status=product.status if product else "",
            ))

        return PurchaseResponse(
            id=purchase.id,
            user_id=purchase.user_id,
            supplier_name=purchase.supplier_name,
            reference_number=purchase.reference_number,
            date=purchase.date,
            notes=purchase.notes,
            total=self._money(purchase.total),
            status=purchase.status,
            items=items,
            created_at=created_at or "",
            updated_at=updated_at or "",
            created_at_formatted=created_at_formatted,
            updated_at_formatted=updated_at_formatted,
            user_name=user.display_name if user else "",
            user_email=user.email if user else "",
        )

    def _validate_items(self, items_data: list) -> None:
        """Validate all products exist and are active."""
        for item in items_data:
            product = self.product_repo.get_by_id(item.productId)
            if not product:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product with id {item.productId} not found",
                )
            if product.status != "active":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Product '{product.name}' (id={item.productId}) is not active",
                )

    def _check_duplicate_reference(self, reference_number: Optional[str], exclude_id: Optional[uuid.UUID] = None) -> None:
        if not reference_number:
            return
        existing = self.repo.get_by_reference_number(reference_number)
        if existing and existing.id != exclude_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A purchase with reference number '{reference_number}' already exists",
            )

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def count(
        self,
        status_filter: Optional[str] = None,
        supplier_name: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> int:
        return self.repo.count(
            status=status_filter,
            supplier_name=supplier_name,
            start_date=start_date,
            end_date=end_date,
        )

    def get_all_enriched(
        self,
        status_filter: Optional[str] = None,
        supplier_name: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[PurchaseResponse]:
        purchases = self.repo.get_all(
            status=status_filter,
            supplier_name=supplier_name,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )
        return [self._to_purchase_response(p) for p in purchases]

    def get_by_id(self, purchase_id: uuid.UUID) -> Purchase:
        purchase = self.repo.get_by_id(purchase_id)
        if not purchase:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Purchase with id {purchase_id} not found",
            )
        return purchase

    def get_by_id_enriched(self, purchase_id: uuid.UUID) -> PurchaseResponse:
        return self._to_purchase_response(self.get_by_id(purchase_id))

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #

    def create(self, data: PurchaseCreate, user_id: uuid.UUID) -> PurchaseResponse:
        self._check_duplicate_reference(data.referenceNumber)
        self._validate_items(data.items)

        total = Decimal("0.00")

        purchase = Purchase(
            user_id=user_id,
            supplier_name=data.supplierName,
            reference_number=data.referenceNumber,
            date=data.date,
            notes=data.notes,
            total=Decimal("0.00"),
            status="draft",
        )
        self.db.add(purchase)
        self.db.flush()  # get purchase.id before adding items

        for item in data.items:
            subtotal = self._money(item.unitCost * item.quantity)
            total = self._money(total + subtotal)
            purchase_item = PurchaseItem(
                purchase_id=purchase.id,
                product_id=item.productId,
                quantity=item.quantity,
                remaining_quantity=0,
                unit_cost=self._money(item.unitCost),
                subtotal=subtotal,
            )
            self.db.add(purchase_item)

        purchase.total = total

        self.db.commit()
        self.db.refresh(purchase)
        return self._to_purchase_response(self.get_by_id(purchase.id))

    def update(self, purchase_id: uuid.UUID, data: PurchaseUpdate) -> PurchaseResponse:
        purchase = self.get_by_id(purchase_id)

        if purchase.status != "draft":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Only draft purchases can be edited. Current status: '{purchase.status}'",
            )

        if data.referenceNumber is not None:
            self._check_duplicate_reference(data.referenceNumber, exclude_id=purchase_id)
            purchase.reference_number = data.referenceNumber

        if data.supplierName is not None:
            purchase.supplier_name = data.supplierName
        if data.date is not None:
            purchase.date = data.date
        if data.notes is not None:
            purchase.notes = data.notes

        if data.items is not None:
            self._validate_items(data.items)
            # Replace all items
            for existing_item in purchase.items:
                self.db.delete(existing_item)
            self.db.flush()

            total = Decimal("0.00")
            for item in data.items:
                subtotal = self._money(item.quantity * item.unitCost)
                total = self._money(total + subtotal)
                purchase_item = PurchaseItem(
                    purchase_id=purchase.id,
                    product_id=item.productId,
                    quantity=item.quantity,
                    remaining_quantity=0,
                    unit_cost=self._money(item.unitCost),
                    subtotal=subtotal,
                )
                self.db.add(purchase_item)
            purchase.total = total

        self.db.commit()
        return self._to_purchase_response(self.get_by_id(purchase_id))

    def confirm(self, purchase_id: uuid.UUID) -> PurchaseResponse:
        """Confirm a draft purchase: increment stock and recalculate dependent sale profit snapshots."""
        purchase = self.get_by_id(purchase_id)

        if purchase.status == "confirmed":
            # Idempotent — return as-is
            return self._to_purchase_response(purchase)

        if purchase.status != "draft":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Only draft purchases can be confirmed. Current status: '{purchase.status}'",
            )

        affected_product_ids: set[uuid.UUID] = set()
        # Each confirmed purchase item becomes an available FIFO lot.
        for item in purchase.items:
            product = self.product_repo.get_by_id(item.product_id)
            if not product:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product {item.product_id} no longer exists",
                )
            product.stock += item.quantity
            item.remaining_quantity = item.quantity
            affected_product_ids.add(item.product_id)

        purchase.status = "confirmed"
        self.db.commit()
        self.sale_service.recalculate_sale_snapshots_for_products(
            product_ids=affected_product_ids,
            from_date=purchase.date,
        )
        return self._to_purchase_response(self.get_by_id(purchase_id))

    def cancel(self, purchase_id: uuid.UUID) -> PurchaseResponse:
        """Cancel a purchase. If it was confirmed, reverse the stock adjustments."""
        purchase = self.get_by_id(purchase_id)
        affected_product_ids: set[uuid.UUID] = set()

        if purchase.status == "cancelled":
            return self._to_purchase_response(purchase)

        if purchase.status == "confirmed":
            for item in purchase.items:
                consumed_quantity = item.quantity - item.remaining_quantity
                if consumed_quantity > 0:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Cannot cancel purchase {purchase.id}: {consumed_quantity} units from product "
                            f"'{item.product.name if item.product else item.product_id}' were already sold"
                        ),
                    )
                product = self.product_repo.get_by_id(item.product_id)
                if not product:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Product {item.product_id} no longer exists",
                    )
                new_stock = product.stock - item.remaining_quantity
                if new_stock < 0:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Cannot cancel: reversing stock for product '{product.name}' "
                            f"would result in negative stock "
                            f"(current={product.stock}, to remove={item.remaining_quantity})"
                        ),
                    )
                product.stock = new_stock
                item.remaining_quantity = 0
                affected_product_ids.add(item.product_id)

        purchase.status = "cancelled"
        self.db.commit()
        if affected_product_ids:
            self.sale_service.recalculate_sale_snapshots_for_products(
                product_ids=affected_product_ids,
                from_date=purchase.date,
            )
        return self._to_purchase_response(self.get_by_id(purchase_id))

    def delete(self, purchase_id: uuid.UUID) -> None:
        purchase = self.get_by_id(purchase_id)
        if purchase.status == "confirmed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Confirmed purchases cannot be deleted. Cancel it first.",
            )
        self.repo.delete(purchase)
