import uuid
from datetime import datetime
from typing import Literal, Optional

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

    role: Literal["admin", "sales_rep", "viewer"]


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    is_active: Optional[bool] = None
    role: Optional[Literal["admin", "sales_rep", "viewer"]] = None


class SellerCreate(BaseModel):
    """El admin da de alta a un vendedor sin contrasena."""

    email: EmailStr
    display_name: Optional[str] = None
    role: Literal["sales_rep", "viewer"]


class SellerCreatedResponse(UserResponse):
    password_setup_link: str


class PasswordLinkResponse(BaseModel):
    password_setup_link: str
