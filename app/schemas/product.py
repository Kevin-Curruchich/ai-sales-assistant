import uuid
from typing import Optional
from pydantic import BaseModel


# --- Request schemas ---

class ProductCreate(BaseModel):
    sku: str
    name: str
    description: Optional[str] = None
    price: float
    stock: int = 0
    min_stock: int = 0  # Reorder point threshold
    status: str = "active"  # "active" | "inactive"


class ProductUpdate(BaseModel):
    sku: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[int] = None
    min_stock: Optional[int] = None
    status: Optional[str] = None


# --- Response schemas ---

class ProductResponse(BaseModel):
    id: uuid.UUID
    sku: str
    name: str
    description: Optional[str] = None
    price: float
    stock: int
    min_stock: int
    status: str
    created_at: str
    updated_at: str
    created_at_formatted: Optional[str] = None
    updated_at_formatted: Optional[str] = None
    stock_alert_status: str
    should_reorder: bool

    model_config = {"from_attributes": True}


class PaginationMeta(BaseModel):
    total: int


class PaginatedProductResponse(BaseModel):
    data: list[ProductResponse]
    meta: PaginationMeta
