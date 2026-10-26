import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Sin variable fijada (o tras un reset, que la deja en '') queda NULL: el
# NOT NULL hace fallar el insert en vez de guardarlo sin comercio.
CURRENT_BUSINESS_ID = "nullif(current_setting('app.business_id', true), '')::uuid"


def business_id_column() -> Mapped[uuid.UUID]:
    """La columna `business_id` de cada tabla del comercio y de `users`."""
    return mapped_column(
        Uuid,
        ForeignKey("businesses.id"),
        nullable=False,
        server_default=text(CURRENT_BUSINESS_ID),
    )


class Business(Base):
    """Un comercio. Sin RLS: se lee antes de saber a que comercio pertenece
    la sesion."""

    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="America/Guatemala",
        server_default=text("'America/Guatemala'"),
    )
    # NULL = no se sincroniza con Google Calendar.
    google_calendar_id: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Business(id={self.id}, name='{self.name}')>"
