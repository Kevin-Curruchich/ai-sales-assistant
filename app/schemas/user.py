import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr


# --- Response schemas ---

class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: Optional[str] = None
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Request schemas ---

class UserUpdateRole(BaseModel):
    """Only admins can change roles."""
    role: str  # "admin" | "manager" | "viewer"


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    is_active: Optional[bool] = None
    role: Optional[str] = None


class UserSignUp(BaseModel):
    email: EmailStr
    password: str
    display_name: Optional[str] = None
    role: str = "viewer"  # "admin" | "sales_rep" | "viewer"
