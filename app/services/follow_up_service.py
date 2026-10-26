import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.datetime_utils import business_today
from app.models.follow_up import (
    CLOSED,
    OPEN,
    REASON_DISMISSED,
    REASON_POSTPONED,
    REASON_SALE,
    SYNC_PENDING,
    FollowUp,
)
from app.repositories.follow_up_repository import FollowUpRepository
from app.services import calendar_sync


class FollowUpService:
    """Abre y cierra seguimientos. Toda escritura deja la fila `pending` para
    que el espejo de Google la recoja."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = FollowUpRepository(db)

    def record_purchase(
        self,
        customer_id: uuid.UUID,
        product_id: uuid.UUID,
        sale_id: uuid.UUID,
        next_due: Optional[date],
    ) -> None:
        """Una compra nueva del par: cierra el abierto y abre el siguiente.

        No hace commit: corre dentro de la transaccion de la venta. Quien
        decide si la venta es informacion nueva es `SaleService.create`.
        """
        current = self.repo.get_open(customer_id, product_id)
        if current is not None:
            self._close(current, REASON_SALE, sale_id=sale_id)
        if next_due is not None:
            self._open(customer_id, product_id, next_due)

    def open_if_missing(
        self, customer_id: uuid.UUID, product_id: uuid.UUID, due_date: date
    ) -> None:
        """Abre un seguimiento solo si el par no tiene uno abierto.

        No hace commit: corre dentro de la transaccion de la venta.
        """
        if self.repo.get_open(customer_id, product_id) is None:
            self._open(customer_id, product_id, due_date)

    def postpone(
        self, follow_up_id: uuid.UUID, new_date: date, user_id: uuid.UUID
    ) -> FollowUp:
        current = self._get_open_or_raise(follow_up_id)
        if new_date <= business_today(self.db):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="La nueva fecha del seguimiento debe ser posterior a hoy",
            )
        self._close(current, REASON_POSTPONED, closed_by=user_id)
        new = self._open(current.customer_id, current.product_id, new_date)
        self.db.commit()
        self.db.refresh(new)
        calendar_sync.request_calendar_sync()
        return new

    def dismiss(self, follow_up_id: uuid.UUID, user_id: uuid.UUID) -> None:
        current = self._get_open_or_raise(follow_up_id)
        self._close(current, REASON_DISMISSED, closed_by=user_id)
        self.db.commit()
        calendar_sync.request_calendar_sync()

    def _get_open_or_raise(self, follow_up_id: uuid.UUID) -> FollowUp:
        # FOR UPDATE: un doble envio espera aqui y ve el seguimiento ya cerrado
        # (409) en vez de chocar con el indice unico (500).
        follow_up = self.repo.get_by_id_for_update(follow_up_id)
        if follow_up is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Follow-up {follow_up_id} not found",
            )
        if follow_up.status != OPEN:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El seguimiento ya esta cerrado",
            )
        return follow_up

    def _close(
        self,
        follow_up: FollowUp,
        reason: str,
        *,
        sale_id: Optional[uuid.UUID] = None,
        closed_by: Optional[uuid.UUID] = None,
    ) -> None:
        follow_up.status = CLOSED
        follow_up.close_reason = reason
        follow_up.sale_id = sale_id
        follow_up.closed_by = closed_by
        follow_up.closed_at = datetime.now(timezone.utc)
        follow_up.sync_status = SYNC_PENDING
        follow_up.sync_error = None
        # El UPDATE tiene que llegar a la base antes del INSERT del siguiente:
        # si no, el indice unico parcial ve dos abiertos del mismo par.
        self.db.flush()

    def _open(
        self, customer_id: uuid.UUID, product_id: uuid.UUID, due_date: date
    ) -> FollowUp:
        follow_up = FollowUp(
            customer_id=customer_id,
            product_id=product_id,
            due_date=due_date,
            status=OPEN,
            sync_status=SYNC_PENDING,
        )
        self.repo.add(follow_up)
        self.db.flush()
        return follow_up
