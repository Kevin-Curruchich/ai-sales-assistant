"""Corrige el costo unitario de las ventas que quedaron con el redondeo viejo.

Uso:
    POSTGRES_SCHEMA=db_v2 venv/bin/python -m scripts.fix_historical_cost_basis --dry-run
    POSTGRES_SCHEMA=db_v2 venv/bin/python -m scripts.fix_historical_cost_basis

El codigo viejo redondeaba el costo FIFO en cada iteracion del bucle de lotes:
`money(0.5 * 33.33)` daba 16.67, y 16.67 / 0.5 = 33.34.  El codigo actual
acumula y redondea UNA sola vez al final: 16.665 / 0.5 = 33.33.

El dano no es el centavo: es que un costo como 33.34 no corresponde a ningun
lote que haya existido.  Si se audita esa venta preguntando de donde salio ese
costo, no hay respuesta.  Las asignaciones de lote -- que son la fuente de
verdad y estan bien -- dicen otra cosa.

Este script recalcula desde `sale_item_lot_allocations`, nunca desde constantes
escritas a mano, y corrige TAMBIEN `gross_profit_total`, que se derivo del
costo equivocado.  Corregir solo el costo dejaria la fila incoherente consigo
misma.

Es idempotente: la segunda corrida no encuentra nada que hacer.

Los items sin asignaciones de lote (las ventas mas viejas, anteriores al
rastreo de lotes) se saltean: no hay contra que recalcular.
"""

from __future__ import annotations

import argparse
import os
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import SessionLocal

# Los mismos nombres que usan alembic/env.py, copy_schema_data.py y
# backfill_projections.py: un solo par para todo el repo.
PROTECTED_SCHEMA = "db_dev"
ALLOW_PROTECTED_SCHEMA_ENV_VAR = "REVENEW_ALLOW_DB_DEV"

MONEY = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _guard_against_protected_schema(schema: str) -> None:
    if (
        schema == PROTECTED_SCHEMA
        and os.environ.get(ALLOW_PROTECTED_SCHEMA_ENV_VAR) != "1"
    ):
        raise RuntimeError(
            f"El schema resuelto es {schema!r}, que guarda el respaldo vivo de "
            "produccion. Este script escribe. Me niego a correr.\n"
            f"Si de verdad querés escribir en {schema!r}, poné "
            f"{ALLOW_PROTECTED_SCHEMA_ENV_VAR}=1."
        )


def _discrepancias(db: Session) -> list[dict]:
    """Items cuyo costo guardado no coincide con el que dicen sus lotes."""
    filas = (
        db.execute(
            text(
                """
            SELECT si.id,
                   s.date               AS fecha,
                   c.name               AS cliente,
                   p.name               AS producto,
                   si.quantity          AS cantidad,
                   si.unit_price        AS precio,
                   si.cost_basis_unit   AS costo_guardado,
                   si.gross_profit_total AS ganancia_guardada,
                   SUM(a.quantity_allocated * a.unit_cost_snapshot) AS costo_total_lotes
              FROM sale_items si
              JOIN sales s     ON s.id = si.sale_id
              JOIN customers c ON c.id = s.customer_id
              JOIN products p  ON p.id = si.product_id
              JOIN sale_item_lot_allocations a ON a.sale_item_id = si.id
          GROUP BY si.id, s.date, c.name, p.name, si.quantity, si.unit_price,
                   si.cost_basis_unit, si.gross_profit_total
          ORDER BY s.date
            """
            )
        )
        .mappings()
        .all()
    )

    pendientes = []
    for f in filas:
        cantidad = Decimal(str(f["cantidad"]))
        if cantidad <= 0:
            continue
        # Acumular y redondear UNA vez, que es lo que hace el codigo actual.
        costo_correcto = _money(Decimal(str(f["costo_total_lotes"])) / cantidad)
        precio = Decimal(str(f["precio"]))
        ganancia_correcta = _money(_money(precio - costo_correcto) * cantidad)

        if (
            _money(Decimal(str(f["costo_guardado"]))) == costo_correcto
            and _money(Decimal(str(f["ganancia_guardada"]))) == ganancia_correcta
        ):
            continue

        pendientes.append(
            {
                "id": f["id"],
                "fecha": f["fecha"],
                "cliente": f["cliente"],
                "producto": f["producto"],
                "cantidad": cantidad,
                "costo_guardado": Decimal(str(f["costo_guardado"])),
                "costo_correcto": costo_correcto,
                "ganancia_guardada": Decimal(str(f["ganancia_guardada"])),
                "ganancia_correcta": ganancia_correcta,
            }
        )
    return pendientes


def corregir(db: Session, dry_run: bool = False) -> list[dict]:
    pendientes = _discrepancias(db)
    if not dry_run:
        for p in pendientes:
            db.execute(
                text(
                    "UPDATE sale_items "
                    "   SET cost_basis_unit = :costo, gross_profit_total = :ganancia "
                    " WHERE id = :id"
                ),
                {
                    "costo": p["costo_correcto"],
                    "ganancia": p["ganancia_correcta"],
                    "id": p["id"],
                },
            )
        db.commit()
    return pendientes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="no escribe nada")
    args = parser.parse_args()

    from app.core.config import settings

    schema = settings.POSTGRES_SCHEMA
    print(f"  schema resuelto: {schema!r}")
    _guard_against_protected_schema(schema)

    db = SessionLocal()
    try:
        pendientes = corregir(db, dry_run=args.dry_run)
        if not pendientes:
            print("  nada que corregir: todos los costos coinciden con sus lotes")
            return

        print(f"\n  {len(pendientes)} fila(s) a corregir:\n")
        print(
            f"  {'fecha':<12} {'cliente':<20} {'producto':<26} "
            f"{'costo':>16} {'ganancia':>16}"
        )
        impacto = Decimal("0.00")
        for p in pendientes:
            costo = f"{p['costo_guardado']} -> {p['costo_correcto']}"
            gan = f"{p['ganancia_guardada']} -> {p['ganancia_correcta']}"
            print(
                f"  {str(p['fecha']):<12} {p['cliente'][:20]:<20} "
                f"{p['producto'][:26]:<26} {costo:>16} {gan:>16}"
            )
            impacto += abs(p["ganancia_correcta"] - p["ganancia_guardada"])
        print(f"\n  impacto en ganancia reportada: Q{impacto}")
        if args.dry_run:
            print("\n  (dry-run: no se escribio nada)")
        else:
            print("\n  escrito.")
            restantes = _discrepancias(db)
            print(f"  verificacion: quedan {len(restantes)} discrepancia(s)")
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
