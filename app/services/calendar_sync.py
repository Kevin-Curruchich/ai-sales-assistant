"""Espejo de los seguimientos en Google Calendar.

Quien escribe seguimientos llama a `request_calendar_sync()` despues de su
commit; eso solo enciende una senal. El hilo que la atiende (mas abajo, Task 7)
duerme sin temporizador mientras no haya trabajo.
"""

import logging
import threading
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.follow_up import OPEN, SYNC_FAILED, SYNC_SYNCED, FollowUp
from app.repositories.customer_product_cycle_repository import (
    CustomerProductCycleRepository,
)
from app.repositories.follow_up_repository import FollowUpRepository

logger = logging.getLogger(__name__)

_wake = threading.Event()


def request_calendar_sync() -> None:
    """Pide una pasada de sincronizacion. No bloquea ni falla."""
    _wake.set()


EVENT_FIELDS = ("summary", "description", "start", "end", "status")


def build_event(follow_up: FollowUp, cycle) -> dict:
    lines = []
    if cycle is not None:
        lines.append(
            f"Ultima compra: {cycle.last_purchase_date:%d/%m/%Y} "
            f"({cycle.last_quantity.normalize()} u.)"
        )
        if cycle.avg_interval_days is not None:
            lines.append(f"Intervalo promedio: {cycle.avg_interval_days:.1f} dias")
    if follow_up.customer.email:
        lines.append(f"Email: {follow_up.customer.email}")
    return {
        "id": follow_up.id.hex,
        "summary": f"Seguimiento: {follow_up.customer.name} · {follow_up.product.name}",
        "description": "\n".join(lines),
        "start": {"date": follow_up.due_date.isoformat()},
        "end": {"date": (follow_up.due_date + timedelta(days=1)).isoformat()},
        # Restaura un evento que alguien borro a mano en Google (queda
        # `cancelled` con el mismo id y el insert responde 409).
        "status": "confirmed",
    }


def sync_pending_follow_ups(db: Session, client) -> int:
    """Lleva a Google cada seguimiento `pending`/`failed`.

    Devuelve cuantas filas quedaron en `failed`. Cada fila se confirma por
    separado: un error de Google no deshace lo que ya se sincronizo.
    """
    cycles = CustomerProductCycleRepository(db)
    failed = 0
    for follow_up in FollowUpRepository(db).get_to_sync():
        seen_status = follow_up.status
        try:
            if seen_status == OPEN:
                event = build_event(
                    follow_up,
                    cycles.get_by_customer_and_product(
                        follow_up.customer_id, follow_up.product_id
                    ),
                )
                if not client.insert(event):
                    client.patch(event["id"], {k: event[k] for k in EVENT_FIELDS})
            else:
                client.delete(follow_up.id.hex)
            values = {"sync_status": SYNC_SYNCED, "sync_error": None}
        except Exception as exc:  # CalendarError, timeouts de requests
            logger.warning(
                "No se pudo sincronizar el seguimiento %s: %s", follow_up.id, exc
            )
            values = {"sync_status": SYNC_FAILED, "sync_error": str(exc)[:1000]}
            failed += 1
        # Solo si el estado no cambio mientras se hablaba con Google: si lo
        # cerraron entretanto, la fila sigue `pending` y su escritor ya pidio
        # otra pasada, que borrara el evento.
        db.execute(
            update(FollowUp)
            .where(FollowUp.id == follow_up.id, FollowUp.status == seen_status)
            .values(**values)
        )
        db.commit()
    return failed
