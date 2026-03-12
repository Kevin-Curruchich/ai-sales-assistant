import uuid
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import select
from app.models.customer import Customer
from app.repositories.customer_repository import CustomerRepository
from app.schemas.customer import CustomerCreate, CustomerUpdate
from app.models.customer_product_cycle import CustomerProductCycle
from app.models.sale import Sale
from app.models.sale_item import SaleItem
from datetime import datetime
import logging
from dateutil.parser import parse

logger = logging.getLogger("customers")


class CustomerService:
    def __init__(self, db: Session):
        self.db = db  # Ensure the db session is assigned to self.db
        self.repo = CustomerRepository(db)

    def get_all(self, search: Optional[str] = None, limit: int = 10, offset: int = 0) -> list[Customer]:
        return self.repo.get_all(search=search, limit=limit, offset=offset)

    def get_all_with_formatted_dates(self, search: Optional[str] = None, limit: int = 10, offset: int = 0):
        customers = self.get_all(search=search, limit=limit, offset=offset)
        return [self.format_customer_dates(customer) for customer in customers]

    def get_by_id(self, customer_id: uuid.UUID) -> Customer:
        customer = self.repo.get_by_id(customer_id)
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer with id {customer_id} not found",
            )
        return customer

    def create(self, data: CustomerCreate) -> Customer:
        customer = Customer(**data.model_dump())
        return self.repo.create(customer)

    def update(self, customer_id: uuid.UUID, data: CustomerUpdate) -> Customer:
        customer = self.get_by_id(customer_id)
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(customer, key, value)
        return self.repo.update(customer)

    def delete(self, customer_id: uuid.UUID) -> None:
        customer = self.get_by_id(customer_id)
        self.repo.delete(customer)

    def count(self, search: Optional[str] = None) -> int:
        return self.repo.count(search=search)

    def format_customer_dates(self, customer):
        # Ensure created_at and updated_at are datetime objects BEFORE getting __dict__
        if isinstance(customer.created_at, str):
            customer.created_at = parse(customer.created_at)
        if isinstance(customer.updated_at, str):
            customer.updated_at = parse(customer.updated_at)

        # Now get the dictionary after conversion
        customer_dict = customer.__dict__.copy()
        
        customer_dict["created_at"] = customer.created_at.strftime("%Y-%m-%d")
        customer_dict["updated_at"] = customer.updated_at.strftime("%Y-%m-%d")
        customer_dict["created_at_formatted"] = customer.created_at.strftime("%d/%m/%Y")
        customer_dict["updated_at_formatted"] = customer.updated_at.strftime("%d/%m/%Y")
        return customer_dict

    def format_sale_dates(self, sale):
        sale_dict = sale.__dict__.copy()
        sale_dict["created_at"] = sale.created_at.strftime("%Y-%m-%d")
        sale_dict["updated_at"] = sale.updated_at.strftime("%Y-%m-%d")
        sale_dict["date"] = sale.date.strftime("%Y-%m-%d")
        sale_dict["date_formatted"] = sale.date.strftime("%d/%m/%Y")
        
        # Format sale items with product details
        items = []
        for item in sale.items:
            items.append({
                "id": str(item.id),
                "product_id": str(item.product_id),
                "product_name": item.product.name,
                "product_sku": item.product.sku,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "subtotal": item.subtotal,
            })
        sale_dict["items"] = items
        
        return sale_dict

    def get_last_purchases(self, customer_id: uuid.UUID, limit: int = 5):
        stmt = (
            select(Sale)
            .options(joinedload(Sale.items).joinedload(SaleItem.product))
            .where(Sale.customer_id == customer_id)
            .order_by(Sale.date.desc())
            .limit(limit)
        )
        sales = self.db.execute(stmt).unique().scalars().all()
        return [self.format_sale_dates(sale) for sale in sales]

    def predict_next_purchase(self, customer_id: uuid.UUID):
        stmt = (
            select(CustomerProductCycle)
            .options(joinedload(CustomerProductCycle.product))
            .where(CustomerProductCycle.customer_id == customer_id)
            .where(CustomerProductCycle.estimated_next_purchase.isnot(None))
            .order_by(CustomerProductCycle.estimated_next_purchase.asc())
        )
        cycles = self.db.execute(stmt).scalars().all()
        
        if not cycles:
            return None
        
        # Format the next purchases with product info
        next_purchases = []
        for cycle in cycles:
            next_purchases.append({
                "product_id": str(cycle.product_id),
                "product_name": cycle.product.name,
                "product_sku": cycle.product.sku,
                "estimated_date": cycle.estimated_next_purchase.strftime("%d/%m/%Y"),
                "avg_interval_days": cycle.avg_interval_days,
                "last_purchase_date": cycle.last_purchase_date.strftime("%d/%m/%Y"),
                "last_quantity": cycle.last_quantity,
            })
        
        return next_purchases

    def get_customer_details(self, customer_id: uuid.UUID, limit: int = 5):
        try:
            customer = self.get_by_id(customer_id)
            last_purchases = self.get_last_purchases(customer_id, limit=limit)
            next_purchases = self.predict_next_purchase(customer_id)

            if not last_purchases:
                logger.warning(f"No sales found for customer {customer_id}")

            if not next_purchases:
                logger.warning(f"No next purchase prediction for customer {customer_id}")

            customer_data = self.format_customer_dates(customer)
            customer_data["last_purchases"] = last_purchases
            customer_data["next_purchases"] = next_purchases if next_purchases else []
            return customer_data
        except Exception as e:
            logger.error(f"Error fetching customer {customer_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal Server Error")
