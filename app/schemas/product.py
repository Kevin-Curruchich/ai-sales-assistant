import uuid
from decimal import Decimal
from enum import Enum
from typing import Optional
from pydantic import BaseModel, model_validator


class EarningMode(str, Enum):
    PERCENT = "percent"
    FEE = "fee"


# --- Request schemas ---

class ProductCreate(BaseModel):
    sku: str
    name: str
    description: Optional[str] = None
    earningMode: EarningMode = EarningMode.PERCENT
    earningPercent: Optional[Decimal] = None
    earningFeeAmount: Optional[Decimal] = None
    stock: int = 0
    min_stock: int = 0  # Reorder point threshold
    status: str = "active"  # "active" | "inactive"

    @model_validator(mode="after")
    def validate_earning_policy(self):
        if self.earningPercent is not None and self.earningPercent < 0:
            raise ValueError("earningPercent must be >= 0")
        if self.earningFeeAmount is not None and self.earningFeeAmount < 0:
            raise ValueError("earningFeeAmount must be >= 0")

        if self.earningMode == EarningMode.PERCENT and self.earningPercent is None:
            raise ValueError("earningPercent is required when earningMode is 'percent'")
        if self.earningMode == EarningMode.FEE and self.earningFeeAmount is None:
            raise ValueError("earningFeeAmount is required when earningMode is 'fee'")

        return self


class ProductUpdate(BaseModel):
    sku: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    earningMode: Optional[EarningMode] = None
    earningPercent: Optional[Decimal] = None
    earningFeeAmount: Optional[Decimal] = None
    stock: Optional[int] = None
    min_stock: Optional[int] = None
    status: Optional[str] = None

    @model_validator(mode="after")
    def validate_non_negative_values(self):
        if self.earningPercent is not None and self.earningPercent < 0:
            raise ValueError("earningPercent must be >= 0")
        if self.earningFeeAmount is not None and self.earningFeeAmount < 0:
            raise ValueError("earningFeeAmount must be >= 0")

        return self


# --- Response schemas ---

class ProductResponse(BaseModel):
    id: uuid.UUID
    sku: str
    name: str
    description: Optional[str] = None
    earning_mode: EarningMode
    earning_percent: Optional[Decimal] = None
    earning_fee_amount: Optional[Decimal] = None
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
