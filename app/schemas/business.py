from typing import Optional

from pydantic import BaseModel, EmailStr


class BusinessSignUp(BaseModel):
    """Alta publica de un comercio y de su admin. No acepta `role`."""

    business_name: str
    email: EmailStr
    password: str
    display_name: Optional[str] = None
