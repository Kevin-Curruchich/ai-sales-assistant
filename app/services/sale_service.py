import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.customer_product_cycle import CustomerProductCycle
from app.models.purchase import PurchaseItem
from app.models.sale import Sale
from app.models.sale_item import SaleItem
from app.models.sale_item_lot_allocation import SaleItemLotAllocation
from app.repositories.customer_product_cycle_repository import CustomerProductCycleRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.purchase_repository import PurchaseRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.sale_repository import SaleRepository
from app.schemas.sale import (
    CalendarDateEvents,
    CalendarEvent,
    CalendarResponse,
    CalendarSummary,
    FollowUpItemResponse,
    FollowUpMetrics,
    FollowUpResponse,
    LotAllocationPreview,
    ProfitReportResponse,
    ProfitReportRow,
    SaleCreate,
    SaleItemCreate,
    SaleItemLotAllocationResponse,
    SaleItemPreview,
    SaleItemResponse,
    SalePreviewResponse,
    SalePreviewTotals,
    SaleResponse,
    SaleUpdate,
)

MONEY = Decimal("0.01")
HUNDRED = Decimal("100")


class SaleService:
    def __init__(self, db: Session):
        self.db = db
        self.sale_repo = SaleRepository(db)
        self.product_repo = ProductRepository(db)
        self.purchase_repo = PurchaseRepository(db)
        self.customer_repo = CustomerRepository(db)
        self.cycle_repo = CustomerProductCycleRepository(db)

    # ------------------------------------------------------------------
    # Money and pricing helpers
    # ------------------------------------------------------------------

    def _money(self, value: Decimal | float | int | None) -> Decimal:
        if value is None:
            return Decimal("0.00")
        return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)

    def _allocate_fifo_lots(
        self,
        product_id: uuid.UUID,
        quantity: Decimal,
        sale_date: date,
    ) -> tuple[list[tuple[PurchaseItem, Decimal]], Decimal]:
        lots = self.purchase_repo.get_fifo_available_lots(
            product_id=product_id,
            as_of_date=sale_date,
            lock_for_update=True,
        )

        to_consume = Decimal(str(quantity))
        allocations: list[tuple[PurchaseItem, Decimal]] = []
        total_cost = Decimal("0.00")

        for lot in lots:
            if to_consume <= 0:
                break

            take = min(lot.remaining_quantity, to_consume)
            if take <= 0:
                continue

            allocations.append((lot, take))
            total_cost = self._money(total_cost + self._money(lot.unit_cost) * take)
            to_consume -= take

        if to_consume > 0:
            available = quantity - to_consume
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Insufficient FIFO lots to price sale item. "
                    f"Available priced quantity={available}, requested={quantity}."
                ),
            )

        cost_basis = self._money(total_cost / quantity)
        return allocations, cost_basis

    def _suggested_unit_price(self, product, cost_basis: Decimal) -> Decimal:
        mode = getattr(product.earning_mode, "value", product.earning_mode)
        if mode == "percent":
            percent = Decimal(str(product.earning_percent or 0))
            return self._money(cost_basis * (Decimal("1") + (percent / HUNDRED)))

        fee_amount = Decimal(str(product.earning_fee_amount or 0))
        return self._money(cost_basis + fee_amount)

    def _resolve_sale_item_pricing(
        self,
        product,
        cost_basis: Decimal,
        item_data: SaleItemCreate,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, bool, Optional[str], Optional[Decimal], Optional[Decimal]]:
        if item_data.discountPercent is not None and item_data.discountAmount is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Provide only one discount type for product '{product.name}': "
                    "discountPercent or discountAmount"
                ),
            )

        suggested = self._suggested_unit_price(product, cost_basis)
        discount_percent = self._money(item_data.discountPercent) if item_data.discountPercent is not None else None
        discount_amount = self._money(item_data.discountAmount) if item_data.discountAmount is not None else None

        if item_data.unitPrice is not None:
            if discount_percent is not None or discount_amount is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Do not send unitPrice together with discounts for product '{product.name}'"
                    ),
                )

            unit_price = self._money(item_data.unitPrice)
            is_overridden = unit_price != suggested
            reason = item_data.pricingExceptionReason

            # Market-up overrides are allowed, but must be justified.
            if unit_price > suggested and not reason:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "pricingExceptionReason is required when overriding above suggested price for "
                        f"product '{product.name}'."
                    ),
                )
        else:
            unit_price = suggested
            is_overridden = False
            reason = item_data.pricingExceptionReason

            if discount_percent is not None:
                unit_price = self._money(suggested * (Decimal("1") - (discount_percent / HUNDRED)))
            elif discount_amount is not None:
                unit_price = self._money(suggested - discount_amount)

        if unit_price < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unitPrice must be >= 0 for product '{product.name}'",
            )

        gross_profit_unit = self._money(unit_price - cost_basis)
        return (
            unit_price,
            cost_basis,
            gross_profit_unit,
            suggested,
            is_overridden,
            reason,
            discount_percent,
            discount_amount,
        )

    def _to_sale_response(self, sale: Sale) -> SaleResponse:
        user = sale.user
        customer = sale.customer

        created_at = sale.created_at
        updated_at = sale.updated_at
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))

        items = []
        for item in sale.items:
            product = item.product
            items.append(
                SaleItemResponse(
                    id=item.id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                    unit_price=self._money(item.unit_price),
                    subtotal=self._money(item.subtotal),
                    cost_basis_unit=self._money(item.cost_basis_unit) if item.cost_basis_unit is not None else None,
                    gross_profit_unit=self._money(item.gross_profit_unit) if item.gross_profit_unit is not None else None,
                    gross_profit_total=self._money(item.gross_profit_total) if item.gross_profit_total is not None else None,
                    discount_percent=self._money(item.discount_percent) if item.discount_percent is not None else None,
                    discount_amount=self._money(item.discount_amount) if item.discount_amount is not None else None,
                    is_price_overridden=item.is_price_overridden,
                    pricing_exception_reason=item.pricing_exception_reason,
                    allocations=[
                        SaleItemLotAllocationResponse(
                            id=alloc.id,
                            purchase_item_id=alloc.purchase_item_id,
                            quantity_allocated=alloc.quantity_allocated,
                            unit_cost_snapshot=self._money(alloc.unit_cost_snapshot),
                            lot_purchase_date=alloc.lot_purchase_date,
                        )
                        for alloc in item.allocations
                    ],
                    product_name=product.name if product else "",
                    product_sku=product.sku if product else "",
                    product_earning_mode=product.earning_mode if product else "percent",
                    product_earning_percent=product.earning_percent if product else None,
                    product_earning_fee_amount=product.earning_fee_amount if product else None,
                    product_status=product.status if product else "",
                )
            )

        return SaleResponse(
            id=sale.id,
            customer_id=sale.customer_id,
            user_id=sale.user_id,
            date=sale.date,
            total=self._money(sale.total),
            items=items,
            created_at=created_at.strftime("%Y-%m-%d") if created_at else "",
            updated_at=updated_at.strftime("%Y-%m-%d") if updated_at else "",
            created_at_formatted=created_at.strftime("%d/%m/%Y") if created_at else None,
            updated_at_formatted=updated_at.strftime("%d/%m/%Y") if updated_at else None,
            user_name=user.display_name if user else "",
            user_email=user.email if user else "",
            customer_name=customer.name if customer else "",
            customer_company=customer.company if customer else "",
            customer_email=customer.email if customer else "",
        )

    def recalculate_sale_snapshots_for_products(self, product_ids: set[uuid.UUID], from_date: date) -> None:
        # FIFO snapshots are immutable once a sale is confirmed.
        return

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def count(
        self,
        customer_id: Optional[uuid.UUID] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> int:
        return self.sale_repo.count(customer_id=customer_id, start_date=start_date, end_date=end_date)

    def get_all(
        self,
        customer_id: Optional[uuid.UUID] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Sale]:
        return self.sale_repo.get_all(
            customer_id=customer_id, start_date=start_date, end_date=end_date, limit=limit, offset=offset
        )

    def get_by_id(self, sale_id: uuid.UUID) -> Sale:
        sale = self.sale_repo.get_by_id(sale_id)
        if not sale:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Sale with id {sale_id} not found",
            )
        return sale

    def get_all_enriched(
        self,
        customer_id: Optional[uuid.UUID] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[SaleResponse]:
        sales = self.get_all(customer_id, start_date, end_date, limit=limit, offset=offset)
        return [self._to_sale_response(s) for s in sales]

    def get_by_id_enriched(self, sale_id: uuid.UUID) -> SaleResponse:
        return self._to_sale_response(self.get_by_id(sale_id))

    def create_enriched(self, data: SaleCreate, user_id: uuid.UUID) -> SaleResponse:
        sale = self.create(data, user_id=user_id)
        return self._to_sale_response(self.get_by_id(sale.id))

    def preview_sale(self, data: SaleCreate) -> SalePreviewResponse:
        """Simulate FIFO lot allocation and pricing without writing to the database.

        Reads the current available lots for each product and returns the exact
        allocation split, computed cost basis, suggested price, and final price
        (after any discount/override) so the frontend can show an audit breakdown
        before the user confirms.

        No rows are inserted or updated; no row locks are acquired.
        """
        customer = self.customer_repo.get_by_id(data.customerId)
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer with id {data.customerId} not found",
            )

        preview_items: list[SaleItemPreview] = []
        total_revenue = Decimal("0.00")
        total_cost = Decimal("0.00")
        total_gross_profit = Decimal("0.00")

        for item_data in data.items:
            product = self.product_repo.get_by_id(item_data.productId)
            if not product:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product with id {item_data.productId} not found",
                )

            warnings: list[str] = []

            if product.status != "active":
                warnings.append(f"Product '{product.name}' is not active.")

            if product.stock < item_data.quantity:
                warnings.append(
                    f"Insufficient stock for product '{product.name}'. "
                    f"Available: {product.stock}, Requested: {item_data.quantity}."
                )

            # Dry-run FIFO allocation — no lock, no DB write
            lots = self.purchase_repo.get_fifo_available_lots(
                product_id=item_data.productId,
                as_of_date=data.date,
                lock_for_update=False,
            )

            to_consume = item_data.quantity
            lot_previews: list[LotAllocationPreview] = []
            total_item_cost = Decimal("0.00")

            for lot in lots:
                if to_consume <= 0:
                    break
                take = min(lot.remaining_quantity, to_consume)
                if take <= 0:
                    continue
                lot_previews.append(
                    LotAllocationPreview(
                        purchase_item_id=lot.id,
                        purchase_id=lot.purchase_id,
                        purchase_date=lot.purchase.date,
                        unit_cost=self._money(lot.unit_cost),
                        quantity_available=lot.remaining_quantity,
                        quantity_taken=take,
                    )
                )
                total_item_cost = self._money(total_item_cost + self._money(lot.unit_cost) * take)
                to_consume -= take

            if to_consume > 0:
                available_qty = item_data.quantity - to_consume
                warnings.append(
                    f"Only {available_qty} of {item_data.quantity} units have confirmed FIFO lot cost. "
                    "This sale would be blocked at creation time."
                )
                # Best-effort: use whatever cost we could compute
                if available_qty > 0:
                    cost_basis = self._money(total_item_cost / available_qty)
                else:
                    cost_basis = Decimal("0.00")
            else:
                cost_basis = self._money(total_item_cost / item_data.quantity)

            suggested = self._suggested_unit_price(product, cost_basis)

            # Resolve pricing (same rules as actual create)
            try:
                (
                    final_price,
                    cost_basis,
                    gross_profit_unit,
                    _,
                    is_overridden,
                    reason,
                    discount_percent,
                    discount_amount,
                ) = self._resolve_sale_item_pricing(
                    product=product,
                    cost_basis=cost_basis,
                    item_data=item_data,
                )
            except HTTPException as exc:
                # Pricing rules violated — surface as a warning instead of aborting
                warnings.append(f"Pricing rule error: {exc.detail}")
                final_price = suggested
                gross_profit_unit = self._money(suggested - cost_basis)
                is_overridden = False
                reason = None
                discount_percent = None
                discount_amount = None

            subtotal = self._money(final_price * item_data.quantity)
            gross_profit_total = self._money(gross_profit_unit * item_data.quantity)
            item_total_cost = self._money(cost_basis * item_data.quantity)

            total_revenue = self._money(total_revenue + subtotal)
            total_cost = self._money(total_cost + item_total_cost)
            total_gross_profit = self._money(total_gross_profit + gross_profit_total)

            preview_items.append(
                SaleItemPreview(
                    product_id=product.id,
                    product_name=product.name,
                    product_sku=product.sku,
                    requested_quantity=item_data.quantity,
                    allocations=lot_previews,
                    cost_basis_unit=cost_basis,
                    suggested_unit_price=suggested,
                    final_unit_price=final_price,
                    discount_percent=discount_percent,
                    discount_amount=discount_amount,
                    is_price_overridden=is_overridden,
                    pricing_exception_reason=reason,
                    subtotal=subtotal,
                    gross_profit_unit=gross_profit_unit,
                    gross_profit_total=gross_profit_total,
                    warnings=warnings,
                )
            )

        return SalePreviewResponse(
            customer_id=customer.id,
            customer_name=customer.name,
            date=data.date,
            items=preview_items,
            totals=SalePreviewTotals(
                total_revenue=total_revenue,
                total_cost=total_cost,
                total_gross_profit=total_gross_profit,
            ),
        )



    def update_enriched(self, sale_id: uuid.UUID, data: SaleUpdate) -> SaleResponse:
        sale = self.update(sale_id, data)
        return self._to_sale_response(self.get_by_id(sale.id))

    def create(self, data: SaleCreate, user_id: uuid.UUID) -> Sale:
        customer = self.customer_repo.get_by_id(data.customerId)
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer with id {data.customerId} not found",
            )

        sale_items: list[SaleItem] = []
        total = Decimal("0.00")

        for item_data in data.items:
            product = self.product_repo.get_by_id(item_data.productId)
            if not product:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product with id {item_data.productId} not found",
                )
            if product.status != "active":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Product '{product.name}' is not active",
                )
            if product.stock < item_data.quantity:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient stock for product '{product.name}'. "
                        f"Available: {product.stock}, Requested: {item_data.quantity}"
                    ),
                )

            allocations, cost_basis = self._allocate_fifo_lots(
                product_id=item_data.productId,
                quantity=item_data.quantity,
                sale_date=data.date,
            )

            (
                unit_price,
                cost_basis,
                gross_profit_unit,
                _,
                is_overridden,
                reason,
                discount_percent,
                discount_amount,
            ) = self._resolve_sale_item_pricing(
                product=product,
                cost_basis=cost_basis,
                item_data=item_data,
            )

            subtotal = self._money(unit_price * item_data.quantity)
            gross_profit_total = self._money(gross_profit_unit * item_data.quantity)
            total = self._money(total + subtotal)

            sale_item = SaleItem(
                product_id=item_data.productId,
                quantity=item_data.quantity,
                unit_price=unit_price,
                subtotal=subtotal,
                cost_basis_unit=cost_basis,
                gross_profit_unit=gross_profit_unit,
                gross_profit_total=gross_profit_total,
                discount_percent=discount_percent,
                discount_amount=discount_amount,
                is_price_overridden=is_overridden,
                pricing_exception_reason=reason,
                allocations=[
                    SaleItemLotAllocation(
                        purchase_item_id=lot.id,
                        quantity_allocated=lot_quantity,
                        unit_cost_snapshot=float(lot.unit_cost),
                        lot_purchase_date=lot.purchase.date,
                    )
                    for lot, lot_quantity in allocations
                ],
            )
            sale_items.append(sale_item)

            for lot, lot_quantity in allocations:
                lot.remaining_quantity -= lot_quantity

            product.stock -= item_data.quantity

        sale = Sale(
            customer_id=data.customerId,
            user_id=user_id,
            date=data.date,
            total=total,
            items=sale_items,
        )
        sale = self.sale_repo.create(sale)

        for item_data in data.items:
            self._update_cycle(data.customerId, item_data.productId, data.date, item_data.quantity)

        return sale

    def update(self, sale_id: uuid.UUID, data: SaleUpdate) -> Sale:
        sale = self.get_by_id(sale_id)
        update_data = data.model_dump(exclude_unset=True)

        if "customerId" in update_data:
            customer = self.customer_repo.get_by_id(update_data["customerId"])
            if not customer:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Customer with id {update_data['customerId']} not found",
                )
            sale.customer_id = update_data["customerId"]

        if "date" in update_data:
            sale.date = update_data["date"]

        if "items" in update_data and update_data["items"] is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Updating sale items is disabled with FIFO lot traceability. "
                    "Create a new sale instead."
                ),
            )

        result = self.sale_repo.update(sale)

        return result

    # ------------------------------------------------------------------
    # Replenishment cycle calculation
    # ------------------------------------------------------------------

    def _update_cycle(
        self, customer_id: uuid.UUID, product_id: uuid.UUID, sale_date: date, quantity: Decimal
    ) -> None:
        cycle = self.cycle_repo.get_by_customer_and_product(customer_id, product_id)

        all_sales = self.sale_repo.get_by_customer(customer_id)
        purchase_dates: list[date] = []
        for s in all_sales:
            for item in s.items:
                if item.product_id == product_id:
                    purchase_dates.append(s.date)
                    break
        if sale_date not in purchase_dates:
            purchase_dates.append(sale_date)
        purchase_dates.sort()

        total_purchases = len(purchase_dates)

        if total_purchases >= 2:
            intervals = [
                (purchase_dates[i + 1] - purchase_dates[i]).days
                for i in range(len(purchase_dates) - 1)
            ]
            avg = sum(intervals) / len(intervals)
            avg_interval = max(int(avg), 1)
            estimated_next = sale_date + timedelta(days=avg_interval)
        else:
            avg_interval = 30
            estimated_next = sale_date + timedelta(days=30)

        if cycle:
            cycle.avg_interval_days = avg_interval
            cycle.estimated_next_purchase = estimated_next
            cycle.last_purchase_date = sale_date
            cycle.last_quantity = quantity
            cycle.total_purchases = total_purchases
            self.cycle_repo.update(cycle)
        else:
            cycle = CustomerProductCycle(
                customer_id=customer_id,
                product_id=product_id,
                avg_interval_days=avg_interval,
                estimated_next_purchase=estimated_next,
                last_purchase_date=sale_date,
                last_quantity=quantity,
                total_purchases=total_purchases,
            )
            self.cycle_repo.create(cycle)

    # ------------------------------------------------------------------
    # Follow-up logic
    # ------------------------------------------------------------------

    def get_follow_ups(self, filter_type: str = "all", limit: int = 10, offset: int = 0) -> tuple[list[FollowUpResponse], int]:
        today = date.today()
        cycles = self.cycle_repo.get_all_with_estimation()

        customer_cycles: dict[uuid.UUID, list[CustomerProductCycle]] = {}
        for c in cycles:
            customer_cycles.setdefault(c.customer_id, []).append(c)

        follow_ups: list[FollowUpResponse] = []

        for customer_id, c_cycles in customer_cycles.items():
            customer = c_cycles[0].customer
            items: list[FollowUpItemResponse] = []
            worst_days: Optional[int] = None

            for c in c_cycles:
                days_until = (c.estimated_next_purchase - today).days if c.estimated_next_purchase else None
                product = c.product

                items.append(
                    FollowUpItemResponse(
                        product_id=c.product_id,
                        product_name=product.name,
                        avg_interval_days=c.avg_interval_days,
                        last_purchase_date=c.last_purchase_date,
                        last_quantity=c.last_quantity,
                        estimated_next_purchase=c.estimated_next_purchase,
                        days_until=days_until,
                        current_stock=product.stock,
                        min_stock=product.min_stock,
                        stock_alert=product.stock <= product.min_stock,
                    )
                )

                if days_until is not None and (worst_days is None or days_until < worst_days):
                    worst_days = days_until

            if worst_days is None:
                continue

            if worst_days < 0:
                fu_status = "overdue"
            elif worst_days <= 7:
                fu_status = "urgent"
            elif worst_days <= 14:
                fu_status = "upcoming"
            else:
                fu_status = "normal"

            if filter_type == "overdue" and fu_status != "overdue":
                continue
            if filter_type == "7_days" and worst_days > 7:
                continue
            if filter_type == "14_days" and worst_days > 14:
                continue
            if filter_type == "30_days" and worst_days > 30:
                continue

            items.sort(key=lambda i: i.days_until if i.days_until is not None else 9999)
            follow_ups.append(
                FollowUpResponse(
                    customer_id=customer_id,
                    customer=customer.name,
                    email=customer.email,
                    status=fu_status,
                    items=items,
                )
            )

        follow_ups.sort(
            key=lambda f: min((i.days_until for i in f.items if i.days_until is not None), default=9999)
        )

        total = len(follow_ups)
        return follow_ups[offset:offset + limit], total

    def get_follow_up_metrics(self) -> FollowUpMetrics:
        today = date.today()
        cycles = self.cycle_repo.get_all_with_estimation()

        customer_worst: dict[uuid.UUID, int] = {}
        for c in cycles:
            if not c.estimated_next_purchase:
                continue
            days = (c.estimated_next_purchase - today).days
            if c.customer_id not in customer_worst or days < customer_worst[c.customer_id]:
                customer_worst[c.customer_id] = days

        overdue = sum(1 for d in customer_worst.values() if d < 0)
        next_7 = sum(1 for d in customer_worst.values() if d <= 7)
        next_14 = sum(1 for d in customer_worst.values() if d <= 14)
        next_30 = sum(1 for d in customer_worst.values() if d <= 30)

        return FollowUpMetrics(overdue=overdue, next7Days=next_7, next14Days=next_14, next30Days=next_30)

    # ------------------------------------------------------------------
    # Calendar logic
    # ------------------------------------------------------------------

    def get_calendar_events(self, start_date: date, end_date: date) -> CalendarResponse:
        today = date.today()
        cycles = self.cycle_repo.get_all_with_estimation()

        all_events: list[CalendarEvent] = []
        for c in cycles:
            next_purchase = c.estimated_next_purchase
            if not next_purchase or next_purchase < start_date or next_purchase > end_date:
                continue

            event_type = "overdue" if next_purchase < today else "upcoming"
            all_events.append(
                CalendarEvent(
                    date=next_purchase,
                    customerId=c.customer_id,
                    customer=c.customer.name,
                    productId=c.product_id,
                    productName=c.product.name,
                    type=event_type,
                )
            )

        events_by_date: dict[date, list[CalendarEvent]] = {}
        for event in all_events:
            events_by_date.setdefault(event.date, []).append(event)

        date_events_list: list[CalendarDateEvents] = []
        current_date = start_date
        while current_date <= end_date:
            date_events_list.append(
                CalendarDateEvents(date=current_date, events=events_by_date.get(current_date, []))
            )
            current_date += timedelta(days=1)

        summary = CalendarSummary(
            upcoming=sum(1 for e in all_events if e.type == "upcoming"),
            overdue=sum(1 for e in all_events if e.type == "overdue"),
        )

        return CalendarResponse(dates=date_events_list, summary=summary)

    # ------------------------------------------------------------------
    # Profit reports
    # ------------------------------------------------------------------

    def get_profit_report(
        self,
        group_by: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100,
    ) -> ProfitReportResponse:
        rows: dict[str, ProfitReportRow] = {}
        sales = self.sale_repo.get_all(start_date=start_date, end_date=end_date, limit=10000, offset=0)

        for sale in sales:
            for item in sale.items:
                if group_by == "sale":
                    key = str(sale.id)
                    label = f"{sale.date} - {sale.customer.name if sale.customer else 'Unknown'}"
                elif group_by == "customer":
                    key = str(sale.customer_id)
                    label = sale.customer.name if sale.customer else "Unknown"
                elif group_by == "product":
                    key = str(item.product_id)
                    label = item.product.name if item.product else "Unknown"
                else:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="group_by must be one of: sale, customer, product",
                    )

                if key not in rows:
                    rows[key] = ProfitReportRow(
                        key=key,
                        label=label,
                        quantity=0,
                        revenue=Decimal("0.00"),
                        gross_profit=Decimal("0.00"),
                    )

                rows[key].quantity += item.quantity
                rows[key].revenue = self._money(rows[key].revenue + self._money(item.subtotal))
                rows[key].gross_profit = self._money(
                    rows[key].gross_profit + self._money(item.gross_profit_total)
                )

        ordered = sorted(rows.values(), key=lambda r: r.gross_profit, reverse=True)
        return ProfitReportResponse(data=ordered[:limit])

    # ------------------------------------------------------------------
    # Dashboard helpers
    # ------------------------------------------------------------------

    def get_sales_this_month(self) -> float:
        return self.sale_repo.get_sales_this_month()

    def get_recent_sales(self, limit: int = 5) -> list[Sale]:
        return self.sale_repo.get_recent_sales(limit=limit)
