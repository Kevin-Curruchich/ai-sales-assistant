from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, EmailStr, field_validator


class BusinessSignUp(BaseModel):
    """Alta publica de un comercio y de su admin. No acepta `role`."""

    business_name: str
    email: EmailStr
    password: str
    display_name: Optional[str] = None
    timezone: str = "America/Guatemala"

    @field_validator("timezone")
    @classmethod
    def _zona_iana(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"Zona horaria desconocida: {value}")
        return value
