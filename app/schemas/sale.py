import uuid
from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel


# --- Request schemas ---

class SaleItemCreate(BaseModel):
    productId: uuid.UUID
    quantity: int
    unitPrice: float


class SaleCreate(BaseModel):
    customerId: uuid.UUID
    date: date
    items: list[SaleItemCreate]


class SaleUpdate(BaseModel):
    customerId: Optional[uuid.UUID] = None
    date: Optional[date] = None
    items: Optional[list[SaleItemCreate]] = None


# --- Response schemas ---

class SaleItemResponse(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    quantity: int
    unit_price: float
    subtotal: float
    # Enriched product info
    product_name: str
    product_sku: str
    product_price: float
    product_status: str

    model_config = {"from_attributes": True}


class SaleResponse(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    user_id: uuid.UUID
    date: date
    total: float
    items: list[SaleItemResponse] = []
    created_at: datetime
    updated_at: datetime
    # Enriched user info
    user_name: str
    user_email: str
    # Enriched customer info
    customer_name: str
    customer_company: Optional[str] = None
    customer_email: Optional[str] = None

    model_config = {"from_attributes": True}


# --- Follow-up schemas (per customer+product) ---

class FollowUpItemResponse(BaseModel):
    """A single product that a customer is expected to need."""
    product_id: uuid.UUID
    product_name: str
    avg_interval_days: Optional[int] = None
    last_purchase_date: Optional[date] = None
    last_quantity: int = 0
    estimated_next_purchase: Optional[date] = None
    days_until: Optional[int] = None
    current_stock: int = 0
    min_stock: int = 0
    stock_alert: bool = False  # True when current_stock <= min_stock


class FollowUpResponse(BaseModel):
    customer_id: uuid.UUID
    customer: str
    email: Optional[str] = None
    status: str  # "overdue" | "urgent" | "upcoming" | "normal" (worst among products)
    items: list[FollowUpItemResponse] = []


class FollowUpMetrics(BaseModel):
    overdue: int
    next7Days: int
    next14Days: int
    next30Days: int


# --- Calendar schemas ---

class CalendarEvent(BaseModel):
    date: date
    customerId: uuid.UUID
    customer: str
    productId: uuid.UUID
    productName: str
    type: str  # "overdue" | "upcoming"
