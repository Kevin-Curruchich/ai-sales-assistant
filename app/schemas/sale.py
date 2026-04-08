import uuid
from datetime import date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, field_validator


# --- Request schemas ---

class SaleItemCreate(BaseModel):
    productId: uuid.UUID
    quantity: int
    unitPrice: Optional[Decimal] = None
    discountPercent: Optional[Decimal] = None
    discountAmount: Optional[Decimal] = None
    pricingExceptionReason: Optional[str] = None

    @field_validator("quantity")
    @classmethod
    def quantity_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity must be greater than 0")
        return v

    @field_validator("unitPrice")
    @classmethod
    def unit_price_must_be_non_negative(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and v < 0:
            raise ValueError("unitPrice must be >= 0")
        return v

    @field_validator("discountPercent")
    @classmethod
    def discount_percent_must_be_valid(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and (v < 0 or v > 100):
            raise ValueError("discountPercent must be between 0 and 100")
        return v

    @field_validator("discountAmount")
    @classmethod
    def discount_amount_must_be_non_negative(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and v < 0:
            raise ValueError("discountAmount must be >= 0")
        return v


class SaleCreate(BaseModel):
    customerId: uuid.UUID
    date: date
    items: list[SaleItemCreate]


class SaleUpdate(BaseModel):
    customerId: Optional[uuid.UUID] = None
    date: Optional[date] = None
    items: Optional[list[SaleItemCreate]] = None


# --- Preview schemas (pre-sale FIFO audit, no DB write) ---

class LotAllocationPreview(BaseModel):
    purchase_item_id: uuid.UUID
    purchase_id: uuid.UUID
    purchase_date: date
    unit_cost: Decimal
    quantity_available: int  # remaining in lot before this preview
    quantity_taken: int


class SaleItemPreview(BaseModel):
    product_id: uuid.UUID
    product_name: str
    product_sku: str
    requested_quantity: int
    allocations: list[LotAllocationPreview]
    cost_basis_unit: Decimal
    suggested_unit_price: Decimal
    final_unit_price: Decimal
    discount_percent: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None
    is_price_overridden: bool = False
    pricing_exception_reason: Optional[str] = None
    subtotal: Decimal
    gross_profit_unit: Decimal
    gross_profit_total: Decimal
    warnings: list[str] = []


class SalePreviewTotals(BaseModel):
    total_revenue: Decimal
    total_cost: Decimal
    total_gross_profit: Decimal


class SalePreviewResponse(BaseModel):
    customer_id: uuid.UUID
    customer_name: str
    date: date
    items: list[SaleItemPreview]
    totals: SalePreviewTotals


# --- Response schemas ---

class SaleItemLotAllocationResponse(BaseModel):
    id: uuid.UUID
    purchase_item_id: uuid.UUID
    quantity_allocated: int
    unit_cost_snapshot: Decimal
    lot_purchase_date: date

    model_config = {"from_attributes": True}


class SaleItemResponse(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    cost_basis_unit: Optional[Decimal] = None
    gross_profit_unit: Optional[Decimal] = None
    gross_profit_total: Optional[Decimal] = None
    discount_percent: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None
    is_price_overridden: bool = False
    pricing_exception_reason: Optional[str] = None
    # Lot allocation audit trail
    allocations: list[SaleItemLotAllocationResponse] = []
    # Enriched product info
    product_name: str
    product_sku: str
    product_earning_mode: str
    product_earning_percent: Optional[Decimal] = None
    product_earning_fee_amount: Optional[Decimal] = None
    product_status: str

    model_config = {"from_attributes": True}


class SaleResponse(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    user_id: uuid.UUID
    date: date
    total: Decimal
    items: list[SaleItemResponse] = []
    created_at: str
    updated_at: str
    created_at_formatted: Optional[str] = None
    updated_at_formatted: Optional[str] = None
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


class PaginationMeta(BaseModel):
    total: int


class PaginatedFollowUpResponse(BaseModel):
    data: list[FollowUpResponse]
    meta: PaginationMeta


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


class CalendarDateEvents(BaseModel):
    """Events for a single date"""
    date: date
    events: list[CalendarEvent] = []


class CalendarSummary(BaseModel):
    """Summary of calendar events in the date range"""
    upcoming: int = 0
    overdue: int = 0


class CalendarResponse(BaseModel):
    """Complete calendar response with events by date and summary"""
    dates: list[CalendarDateEvents]
    summary: CalendarSummary


class ProfitReportRow(BaseModel):
    key: str
    label: str
    quantity: int
    revenue: Decimal
    gross_profit: Decimal


class ProfitReportResponse(BaseModel):
    data: list[ProfitReportRow]
