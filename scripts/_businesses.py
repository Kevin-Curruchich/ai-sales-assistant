"""Recorrido por comercio para los scripts de mantenimiento."""

from __future__ import annotations

import uuid
from typing import Callable, Optional

from sqlalchemy import text

from app.core.database import bind_business


def run_per_business(
    session_factory, fn: Callable, business_id: Optional[uuid.UUID] = None
) -> list:
    """Llama `fn(db)` con una sesion ligada a cada comercio (o solo a `business_id`).

    `businesses` no tiene RLS, asi que se lista sin comercio ligado.
    """
    with session_factory() as db:
        rows = db.execute(text("SELECT id, timezone FROM businesses")).all()
    rows = [r for r in rows if business_id is None or r.id == business_id]
    if business_id is not None and not rows:
        raise SystemExit(f"No existe el comercio {business_id}")
    results = []
    for row in rows:
        with session_factory() as db:
            bind_business(db, row.id, row.timezone)
            results.append(fn(db))
    return results
