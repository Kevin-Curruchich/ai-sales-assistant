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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CustomerProductCycle, Sale, SaleItem
from app.services.sales.projection import project


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


def main() -> None:
    from app.core.database import SessionLocal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

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
