import uuid
from datetime import date
from typing import Optional
from pydantic import BaseModel, field_validator


# --- Request schemas ---

class PurchaseItemCreate(BaseModel):
    productId: uuid.UUID
    quantity: int
    unitCost: float

    @field_validator("quantity")
    @classmethod
    def quantity_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity must be greater than 0")
        return v

    @field_validator("unitCost")
    @classmethod
    def unit_cost_must_be_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("unitCost must be >= 0")
        return v


class PurchaseCreate(BaseModel):
    supplierName: Optional[str] = None
    referenceNumber: Optional[str] = None
    date: date
    notes: Optional[str] = None
    items: list[PurchaseItemCreate]


class PurchaseUpdate(BaseModel):
    supplierName: Optional[str] = None
    referenceNumber: Optional[str] = None
    date: Optional[date] = None
    notes: Optional[str] = None
    items: Optional[list[PurchaseItemCreate]] = None


# --- Response schemas ---

class PurchaseItemResponse(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    quantity: int
    unit_cost: float
    subtotal: float
    # Enriched product info
    product_name: str
    product_sku: str
    product_price: float
    product_cost_price: Optional[float] = None
    product_status: str

    model_config = {"from_attributes": True}


class PurchaseResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    supplier_name: Optional[str] = None
    reference_number: Optional[str] = None
    date: date
    notes: Optional[str] = None
    total: float
    status: str
    items: list[PurchaseItemResponse] = []
    created_at: str
    updated_at: str
    created_at_formatted: Optional[str] = None
    updated_at_formatted: Optional[str] = None
    # Enriched user info
    user_name: str
    user_email: str

    model_config = {"from_attributes": True}


class PaginationMeta(BaseModel):
    total: int


class PaginatedPurchaseResponse(BaseModel):
    data: list[PurchaseResponse]
    meta: PaginationMeta
