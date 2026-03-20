import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer_product_cycle import CustomerProductCycle
from app.models.purchase import Purchase, PurchaseItem
from app.models.sale import Sale
from app.models.sale_item import SaleItem
from app.repositories.customer_product_cycle_repository import CustomerProductCycleRepository
from app.repositories.customer_repository import CustomerRepository
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
    ProfitReportResponse,
    ProfitReportRow,
    SaleCreate,
    SaleItemCreate,
    SaleItemResponse,
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
        self.customer_repo = CustomerRepository(db)
        self.cycle_repo = CustomerProductCycleRepository(db)

    # ------------------------------------------------------------------
    # Money and pricing helpers
    # ------------------------------------------------------------------

    def _money(self, value: Decimal | float | int | None) -> Decimal:
        if value is None:
            return Decimal("0.00")
        return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)

    def _latest_confirmed_cost(self, product_id: uuid.UUID, as_of_date: date) -> Optional[Decimal]:
        stmt = (
            select(PurchaseItem.unit_cost)
            .join(Purchase, Purchase.id == PurchaseItem.purchase_id)
            .where(PurchaseItem.product_id == product_id)
            .where(Purchase.status == "confirmed")
            .where(Purchase.date <= as_of_date)
            .order_by(Purchase.date.desc(), Purchase.created_at.desc(), PurchaseItem.created_at.desc())
            .limit(1)
        )
        result = self.db.execute(stmt).scalar_one_or_none()
        return self._money(result) if result is not None else None

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
        sale_date: date,
        item_data: SaleItemCreate,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, bool, Optional[str]]:
        cost_basis = self._latest_confirmed_cost(product.id, sale_date)
        if cost_basis is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Sale blocked for product '{product.name}' because there is no confirmed purchase cost "
                    f"on or before {sale_date}."
                ),
            )

        suggested = self._suggested_unit_price(product, cost_basis)

        if item_data.unitPrice is None:
            unit_price = suggested
            is_overridden = False
            reason = None
        else:
            unit_price = self._money(item_data.unitPrice)
            is_overridden = unit_price != suggested
            reason = item_data.pricingExceptionReason

            if is_overridden and not reason:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Pricing exception reason is required when overriding suggested price for "
                        f"product '{product.name}'."
                    ),
                )

        if unit_price < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unitPrice must be >= 0 for product '{product.name}'",
            )

        gross_profit_unit = self._money(unit_price - cost_basis)
        return unit_price, cost_basis, gross_profit_unit, suggested, is_overridden, reason

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
                    is_price_overridden=item.is_price_overridden,
                    pricing_exception_reason=item.pricing_exception_reason,
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

    def _recalculate_item_profit_snapshot(self, item: SaleItem, sale_date: date) -> None:
        latest_cost = self._latest_confirmed_cost(item.product_id, sale_date)
        if latest_cost is None:
            return

        item.cost_basis_unit = latest_cost
        item.gross_profit_unit = self._money(self._money(item.unit_price) - latest_cost)
        item.gross_profit_total = self._money(item.gross_profit_unit * item.quantity)

    def recalculate_sale_snapshots_for_products(self, product_ids: set[uuid.UUID], from_date: date) -> None:
        if not product_ids:
            return

        stmt = (
            select(SaleItem)
            .join(Sale, Sale.id == SaleItem.sale_id)
            .where(SaleItem.product_id.in_(product_ids))
            .where(Sale.date >= from_date)
        )
        items = list(self.db.execute(stmt).scalars().all())

        for item in items:
            sale = self.sale_repo.get_by_id(item.sale_id)
            if not sale:
                continue
            self._recalculate_item_profit_snapshot(item, sale.date)

        self.db.commit()

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

            unit_price, cost_basis, gross_profit_unit, _, is_overridden, reason = self._resolve_sale_item_pricing(
                product=product,
                sale_date=data.date,
                item_data=item_data,
            )

            subtotal = self._money(unit_price * item_data.quantity)
            gross_profit_total = self._money(gross_profit_unit * item_data.quantity)
            total = self._money(total + subtotal)

            sale_items.append(
                SaleItem(
                    product_id=item_data.productId,
                    quantity=item_data.quantity,
                    unit_price=unit_price,
                    subtotal=subtotal,
                    cost_basis_unit=cost_basis,
                    gross_profit_unit=gross_profit_unit,
                    gross_profit_total=gross_profit_total,
                    is_price_overridden=is_overridden,
                    pricing_exception_reason=reason,
                )
            )

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
            for existing_item in sale.items:
                self._recalculate_item_profit_snapshot(existing_item, sale.date)

        if "items" in update_data and update_data["items"] is not None:
            for old_item in sale.items:
                product = self.product_repo.get_by_id(old_item.product_id)
                if product:
                    product.stock += old_item.quantity

            sale.items.clear()

            total = Decimal("0.00")
            for item_data in data.items or []:
                product = self.product_repo.get_by_id(item_data.productId)
                if not product:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Product with id {item_data.productId} not found",
                    )
                if product.stock < item_data.quantity:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Insufficient stock for product '{product.name}'",
                    )

                unit_price, cost_basis, gross_profit_unit, _, is_overridden, reason = self._resolve_sale_item_pricing(
                    product=product,
                    sale_date=sale.date,
                    item_data=item_data,
                )

                subtotal = self._money(unit_price * item_data.quantity)
                gross_profit_total = self._money(gross_profit_unit * item_data.quantity)
                total = self._money(total + subtotal)

                sale.items.append(
                    SaleItem(
                        product_id=item_data.productId,
                        quantity=item_data.quantity,
                        unit_price=unit_price,
                        subtotal=subtotal,
                        cost_basis_unit=cost_basis,
                        gross_profit_unit=gross_profit_unit,
                        gross_profit_total=gross_profit_total,
                        is_price_overridden=is_overridden,
                        pricing_exception_reason=reason,
                    )
                )
                product.stock -= item_data.quantity

            sale.total = total

        result = self.sale_repo.update(sale)

        if "items" in update_data and data.items is not None:
            for item_data in data.items:
                self._update_cycle(sale.customer_id, item_data.productId, sale.date, item_data.quantity)

        return result

    # ------------------------------------------------------------------
    # Replenishment cycle calculation
    # ------------------------------------------------------------------

    def _update_cycle(
        self, customer_id: uuid.UUID, product_id: uuid.UUID, sale_date: date, quantity: int
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
