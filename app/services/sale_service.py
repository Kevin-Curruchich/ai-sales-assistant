import uuid
from typing import Optional
from datetime import date, datetime, timedelta
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from app.models.sale import Sale
from app.models.sale_item import SaleItem
from app.models.customer_product_cycle import CustomerProductCycle
from app.repositories.sale_repository import SaleRepository
from app.repositories.product_repository import ProductRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.customer_product_cycle_repository import CustomerProductCycleRepository
from app.schemas.sale import (
    SaleCreate, SaleItemResponse, SaleResponse, SaleUpdate,
    FollowUpResponse, FollowUpItemResponse, FollowUpMetrics,
    CalendarEvent, CalendarDateEvents, CalendarSummary, CalendarResponse,
)
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SaleService:
    def count(
        self,
        customer_id: Optional[uuid.UUID] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> int:
        return self.sale_repo.count(
            customer_id=customer_id, start_date=start_date, end_date=end_date
        )
    def _to_sale_response(self, sale: Sale) -> "SaleResponse":
        try:
            user = sale.user
            customer = sale.customer
            created_at = sale.created_at
            updated_at = sale.updated_at

            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if isinstance(updated_at, str):
                updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))

            created_at_value = created_at.strftime("%Y-%m-%d") if created_at else ""
            updated_at_value = updated_at.strftime("%Y-%m-%d") if updated_at else ""
            created_at_formatted = created_at.strftime("%d/%m/%Y") if created_at else None
            updated_at_formatted = updated_at.strftime("%d/%m/%Y") if updated_at else None

            items = []
            for item in sale.items:
                product = item.product
                items.append(SaleItemResponse(
                    id=item.id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    subtotal=item.subtotal,
                    product_name=product.name if product else "",
                    product_sku=product.sku if product else "",
                    product_price=product.price if product else 0.0,
                    product_status=product.status if product else "",
                ))
            return SaleResponse(
                id=sale.id,
                customer_id=sale.customer_id,
                user_id=sale.user_id,
                date=sale.date,
                total=sale.total,
                items=items,
                created_at=created_at_value,
                updated_at=updated_at_value,
                created_at_formatted=created_at_formatted,
                updated_at_formatted=updated_at_formatted,
                user_name=user.display_name if user else "",
                user_email=user.email if user else "",
                customer_name=customer.name if customer else "",
                customer_company=customer.company if customer else "",
                customer_email=customer.email if customer else "",
            )
        except AttributeError as e:
            logger.error(f"Error processing sale {sale.id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error processing sale {sale.id}: {e}",
            )

    def get_all_enriched(
        self,
        customer_id: Optional[uuid.UUID] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list["SaleResponse"]:
        sales = self.get_all(customer_id, start_date, end_date, limit=limit, offset=offset)
        return [self._to_sale_response(s) for s in sales]

    def get_by_id_enriched(self, sale_id: uuid.UUID) -> "SaleResponse":
        sale = self.get_by_id(sale_id)
        return self._to_sale_response(sale)

    def create_enriched(self, data: SaleCreate, user_id: uuid.UUID) -> "SaleResponse":
        sale = self.create(data, user_id=user_id)
        sale = self.get_by_id(sale.id)
        return self._to_sale_response(sale)

    def update_enriched(self, sale_id: uuid.UUID, data: SaleUpdate) -> "SaleResponse":
        sale = self.update(sale_id, data)
        sale = self.get_by_id(sale.id)
        return self._to_sale_response(sale)
    def __init__(self, db: Session):
        self.db = db
        self.sale_repo = SaleRepository(db)
        self.product_repo = ProductRepository(db)
        self.customer_repo = CustomerRepository(db)
        self.cycle_repo = CustomerProductCycleRepository(db)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

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

    def create(self, data: SaleCreate, user_id: uuid.UUID) -> Sale:
        # Validate customer exists
        customer = self.customer_repo.get_by_id(data.customerId)
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer with id {data.customerId} not found",
            )

        # Build sale items, validate products, deduct stock
        sale_items: list[SaleItem] = []
        total = 0.0

        for item_data in data.items:
            product = self.product_repo.get_by_id(item_data.productId)
            if not product:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product with id {item_data.productId} not found",
                )
            if product.stock < item_data.quantity:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient stock for product '{product.name}'. "
                        f"Available: {product.stock}, Requested: {item_data.quantity}"
                    ),
                )

            subtotal = item_data.quantity * item_data.unitPrice
            total += subtotal

            sale_item = SaleItem(
                product_id=item_data.productId,
                quantity=item_data.quantity,
                unit_price=item_data.unitPrice,
                subtotal=subtotal,
            )
            sale_items.append(sale_item)

            # Deduct stock
            product.stock -= item_data.quantity

        sale = Sale(
            customer_id=data.customerId,
            user_id=user_id,
            date=data.date,
            total=total,
            items=sale_items,
        )
        sale = self.sale_repo.create(sale)

        # Update replenishment cycles for each product in the sale
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
            # Restore stock from old items
            for old_item in sale.items:
                product = self.product_repo.get_by_id(old_item.product_id)
                if product:
                    product.stock += old_item.quantity

            # Clear old items
            sale.items.clear()

            # Add new items
            total = 0.0
            for item_data in data.items:
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

                subtotal = item_data.quantity * item_data.unitPrice
                total += subtotal
                sale_item = SaleItem(
                    product_id=item_data.productId,
                    quantity=item_data.quantity,
                    unit_price=item_data.unitPrice,
                    subtotal=subtotal,
                )
                sale.items.append(sale_item)
                product.stock -= item_data.quantity

            sale.total = total

        result = self.sale_repo.update(sale)

        # Re-sync cycles for updated items
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
        """Create or update the CustomerProductCycle for this customer+product pair.
        
        Calculates the average purchase interval from the sale history and
        projects the next purchase date.
        """
        cycle = self.cycle_repo.get_by_customer_and_product(customer_id, product_id)

        # Gather all dates this customer bought this product
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

        # Calculate average interval
        avg_interval: Optional[int] = None
        estimated_next: Optional[date] = None

        if total_purchases >= 2:
            intervals = [
                (purchase_dates[i + 1] - purchase_dates[i]).days
                for i in range(len(purchase_dates) - 1)
            ]
            avg = sum(intervals) / len(intervals)
            avg_interval = max(int(avg), 1)
            estimated_next = sale_date + timedelta(days=avg_interval)
        else:
            # First purchase — default to 30 days
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
    # Follow-up logic (per customer, with product-level detail)
    # ------------------------------------------------------------------

    def get_follow_ups(self, filter_type: str = "all", limit: int = 10, offset: int = 0) -> tuple[list[FollowUpResponse], int]:
        today = date.today()
        cycles = self.cycle_repo.get_all_with_estimation()

        # Group cycles by customer
        customer_cycles: dict[uuid.UUID, list[CustomerProductCycle]] = {}
        for c in cycles:
            customer_cycles.setdefault(c.customer_id, []).append(c)

        follow_ups: list[FollowUpResponse] = []

        for customer_id, c_cycles in customer_cycles.items():
            customer = c_cycles[0].customer
            items: list[FollowUpItemResponse] = []
            worst_days: Optional[int] = None  # smallest (most urgent)

            for c in c_cycles:
                days_until = (c.estimated_next_purchase - today).days if c.estimated_next_purchase else None
                product = c.product

                items.append(FollowUpItemResponse(
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
                ))

                if days_until is not None:
                    if worst_days is None or days_until < worst_days:
                        worst_days = days_until

            if worst_days is None:
                continue

            # Determine customer-level status from the most urgent product
            if worst_days < 0:
                fu_status = "overdue"
            elif worst_days <= 7:
                fu_status = "urgent"
            elif worst_days <= 14:
                fu_status = "upcoming"
            else:
                fu_status = "normal"

            # Apply filter
            if filter_type == "overdue" and fu_status != "overdue":
                continue
            elif filter_type == "7_days" and worst_days > 7:
                continue
            elif filter_type == "14_days" and worst_days > 14:
                continue
            elif filter_type == "30_days" and worst_days > 30:
                continue

            # Sort items by days_until ascending
            items.sort(key=lambda i: i.days_until if i.days_until is not None else 9999)

            follow_ups.append(FollowUpResponse(
                customer_id=customer_id,
                customer=customer.name,
                email=customer.email,
                status=fu_status,
                items=items,
            ))

        # Sort customers by most urgent first
        follow_ups.sort(key=lambda f: min(
            (i.days_until for i in f.items if i.days_until is not None), default=9999
        ))
        
        # Apply pagination
        total = len(follow_ups)
        paginated = follow_ups[offset:offset + limit]
        return paginated, total

    def get_follow_up_metrics(self) -> FollowUpMetrics:
        today = date.today()
        cycles = self.cycle_repo.get_all_with_estimation()

        # Group by customer — count each customer once using the most urgent product
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

        return FollowUpMetrics(
            overdue=overdue, next7Days=next_7, next14Days=next_14, next30Days=next_30
        )

    # ------------------------------------------------------------------
    # Calendar logic (one event per customer+product)
    # ------------------------------------------------------------------

    def get_calendar_events(
        self, start_date: date, end_date: date
    ) -> CalendarResponse:
        today = date.today()
        cycles = self.cycle_repo.get_all_with_estimation()

        # Collect all events within the date range
        all_events: list[CalendarEvent] = []
        for c in cycles:
            next_purchase = c.estimated_next_purchase
            if not next_purchase:
                continue
            if next_purchase < start_date or next_purchase > end_date:
                continue

            event_type = "overdue" if next_purchase < today else "upcoming"
            all_events.append(CalendarEvent(
                date=next_purchase,
                customerId=c.customer_id,
                customer=c.customer.name,
                productId=c.product_id,
                productName=c.product.name,
                type=event_type,
            ))

        # Group events by date
        events_by_date: dict[date, list[CalendarEvent]] = {}
        for event in all_events:
            if event.date not in events_by_date:
                events_by_date[event.date] = []
            events_by_date[event.date].append(event)

        # Generate all dates in the range
        date_events_list: list[CalendarDateEvents] = []
        current_date = start_date
        while current_date <= end_date:
            date_events = CalendarDateEvents(
                date=current_date,
                events=events_by_date.get(current_date, [])
            )
            date_events_list.append(date_events)
            current_date += timedelta(days=1)

        # Calculate summary totals
        upcoming_count = sum(1 for e in all_events if e.type == "upcoming")
        overdue_count = sum(1 for e in all_events if e.type == "overdue")

        summary = CalendarSummary(
            upcoming=upcoming_count,
            overdue=overdue_count
        )

        return CalendarResponse(
            dates=date_events_list,
            summary=summary
        )

    # ------------------------------------------------------------------
    # Dashboard helpers
    # ------------------------------------------------------------------

    def get_sales_this_month(self) -> float:
        return self.sale_repo.get_sales_this_month()

    def get_recent_sales(self, limit: int = 5) -> list[Sale]:
        return self.sale_repo.get_recent_sales(limit=limit)
