"""Recalcula las proyecciones de todos los ciclos desde el historial de ventas.

Uso:
    POSTGRES_SCHEMA=db_v2 venv/bin/python -m scripts.backfill_projections --dry-run
    POSTGRES_SCHEMA=db_v2 venv/bin/python -m scripts.backfill_projections

Es idempotente: reproduce las ventas a traves de EWMA cada vez, no acumula.
Los ciclos con una sola compra pierden su fecha inventada y quedan marcados
`insufficient`, que es lo que pide el AGENTS.md en vez de adivinar.
"""

from __future__ import annotations

import argparse
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CustomerProductCycle, Sale, SaleItem
from app.services.sales.projection import project

# db_dev es el schema que .env apunta por defecto y contiene los datos reales
# de produccion (156 ventas al momento de escribir esto). Los mismos nombres
# que usan alembic/env.py y scripts/copy_schema_data.py: un solo par de
# variables para aprender, no una por script.
PROTECTED_SCHEMA = "db_dev"
ALLOW_PROTECTED_SCHEMA_ENV_VAR = "REVENEW_ALLOW_DB_DEV"


def _purchase_dates(session: Session, customer_id, product_id) -> list:
    stmt = (
        select(Sale.date)
        .join(SaleItem, SaleItem.sale_id == Sale.id)
        .where(Sale.customer_id == customer_id, SaleItem.product_id == product_id)
        .order_by(Sale.date)
    )
    return [row[0] for row in session.execute(stmt).all()]


def backfill_projections(session: Session, dry_run: bool = False) -> dict[str, int]:
    cycles = list(session.execute(select(CustomerProductCycle)).scalars().all())
    counts = {"updated": 0, "cleared": 0, "unchanged": 0}

    for cycle in cycles:
        dates = _purchase_dates(session, cycle.customer_id, cycle.product_id)
        interval, next_date, method, confidence = project(dates)

        unchanged = (
            cycle.avg_interval_days == interval
            and cycle.estimated_next_purchase == next_date
            and cycle.projection_method == method
            and cycle.projection_confidence == confidence
            and cycle.total_purchases == len(set(dates))
        )
        if unchanged:
            counts["unchanged"] += 1
            continue

        if next_date is None and cycle.estimated_next_purchase is not None:
            counts["cleared"] += 1
        else:
            counts["updated"] += 1

        if not dry_run:
            cycle.avg_interval_days = interval
            cycle.estimated_next_purchase = next_date
            cycle.projection_method = method
            cycle.projection_confidence = confidence
            cycle.total_purchases = len(set(dates))

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return counts


def _guard_against_protected_schema(schema: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    if schema == PROTECTED_SCHEMA and os.environ.get(ALLOW_PROTECTED_SCHEMA_ENV_VAR) != "1":
        raise RuntimeError(
            f"POSTGRES_SCHEMA resolvio a {schema!r}. Ese schema tiene los datos "
            "reales de produccion y este script no debe escribirle "
            "silenciosamente.\n"
            f"Si de verdad queres escribir sobre {schema!r}, seteá "
            f"{ALLOW_PROTECTED_SCHEMA_ENV_VAR}=1 en el entorno y corré de nuevo."
        )


def main() -> None:
    from app.core.database import SCHEMA, SessionLocal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"  schema resuelto: {SCHEMA!r}")
    _guard_against_protected_schema(SCHEMA, dry_run=args.dry_run)

    session = SessionLocal()
    try:
        counts = backfill_projections(session, dry_run=args.dry_run)
    finally:
        session.close()

    for key, value in counts.items():
        print(f"  {key:<12} {value:>4}")
    if args.dry_run:
        print("\n  (dry-run: no se escribio nada)")


if __name__ == "__main__":
    main()
