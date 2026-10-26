import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.business import business_id_column

OPEN = "open"
CLOSED = "closed"

REASON_SALE = "sale"
REASON_POSTPONED = "postponed"
REASON_DISMISSED = "dismissed"

SYNC_PENDING = "pending"
SYNC_SYNCED = "synced"
SYNC_FAILED = "failed"
SYNC_SKIPPED = "skipped"


class FollowUp(Base):
    """Un seguimiento de un cliente + producto: se abre con la proyeccion y se
    cierra con una venta o a mano (posponer / descartar).

    La base es la fuente de verdad; Google Calendar es un espejo que
    `app/services/calendar_sync.py` mantiene a partir de `sync_status`.
    """

    __tablename__ = "follow_ups"
    __table_args__ = (
        # A lo sumo un seguimiento abierto por par, en la base y no solo en Python.
        Index(
            "uq_follow_ups_open_pair",
            "customer_id",
            "product_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        CheckConstraint("status IN ('open', 'closed')", name="status"),
        CheckConstraint(
            "close_reason IS NULL OR close_reason IN ('sale', 'postponed', 'dismissed')",
            name="close_reason",
        ),
        CheckConstraint(
            "sync_status IN ('pending', 'synced', 'failed', 'skipped')",
            name="sync_status",
        ),
        CheckConstraint(
            "(status = 'open') = (close_reason IS NULL AND closed_at IS NULL)",
            name="closed_fields",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=OPEN)
    close_reason: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    sale_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("sales.id", ondelete="SET NULL"), nullable=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    sync_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=SYNC_PENDING
    )
    sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    business_id: Mapped[uuid.UUID] = business_id_column()

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped["Customer"] = relationship("Customer")
    product: Mapped["Product"] = relationship("Product")

    def __repr__(self) -> str:
        return (
            f"<FollowUp(id={self.id}, customer_id={self.customer_id}, "
            f"product_id={self.product_id}, due_date={self.due_date}, status={self.status})>"
        )
