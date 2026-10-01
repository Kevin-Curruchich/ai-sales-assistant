"""Corrige el costo unitario de un lote del inventario inicial de octubre 2026.

Uso:
    set -a; source .env.production; set +a
    venv/bin/python -m scripts.corregir_costo_inventario_inicial --dry-run
    venv/bin/python -m scripts.corregir_costo_inventario_inicial

## Por que

El inventario inicial del corte (`scripts/corte_inventario_octubre.py`) se cargo
con los costos que se dictaron al contar. El carton de huevos quedo en Q35,00 y
su costo real es Q36,00.

## Por que se niega a correr si el lote ya se consumio

Es el motivo por el que esto es un script y no un UPDATE a mano. Cuando una
venta consume un lote, el costo se COPIA a la venta: `sale_items.cost_basis_unit`
y `gross_profit_total` se calculan en ese momento y quedan grabados. Cambiar el
`unit_cost` del lote despues no los corrige -- deja ventas con un costo que no
corresponde a ningun lote existente, que es exactamente el defecto que
`scripts/fix_historical_cost_basis.py` tuvo que reparar.

Asi que si hay una sola asignacion contra este lote, el script para y no escribe
nada: a esa altura la correccion ya no son tres campos, hay que recalcular las
ventas afectadas tambien.

## Por que el total de la compra se deriva y no se escribe a mano

`purchases.total` se recalcula sumando los subtotales de sus items. Escribir
Q632,00 como constante seria un segundo lugar donde equivocarse, y el dia que se
corrija otro lote quedaria desactualizado en silencio.

## Sin tests

Script de un solo uso contra datos reales, igual que los otros del corte. La red
es `--dry-run` y la verificacion dentro de la misma transaccion.
"""

from __future__ import annotations

import argparse
import os
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal

PROTECTED_SCHEMA = "db_dev"
ALLOW_PROTECTED_SCHEMA_ENV_VAR = "REVENEW_ALLOW_DB_DEV"

REFERENCIA = "INV-INICIAL-2026-10"

MONEY = Decimal("0.01")


def _money(valor: Decimal) -> Decimal:
    """Dos decimales, como el resto del dinero de este repo."""
    return Decimal(str(valor)).quantize(MONEY, rounding=ROUND_HALF_UP)

# El costo corregido, dictado por el dueño del negocio. Un lote por SKU: si
# alguna vez hay que corregir dos, se agregan aca y el total de la compra se
# recalcula solo.
CORRECCIONES = {
    # sku: costo unitario correcto
    "LKCX-002": Decimal("36.00"),  # Carton de huevos (30 U): se cargo 35,00
}


def _guard_against_protected_schema(schema: str) -> None:
    if schema == PROTECTED_SCHEMA and os.environ.get(ALLOW_PROTECTED_SCHEMA_ENV_VAR) != "1":
        raise RuntimeError(
            f"El schema resuelto es {schema!r}, que guarda el respaldo vivo de "
            "produccion. Este script escribe. Me niego a correr.\n"
            f"Si de verdad querés escribir en {schema!r}, poné "
            f"{ALLOW_PROTECTED_SCHEMA_ENV_VAR}=1."
        )


def _lotes(db: Session) -> list[dict]:
    return [
        dict(f)
        for f in db.execute(
            text(
                """
                SELECT pi.id, p.sku, p.name, pi.quantity, pi.remaining_quantity,
                       pi.unit_cost, pi.subtotal
                FROM purchase_items pi
                JOIN products p ON p.id = pi.product_id
                JOIN purchases pu ON pu.id = pi.purchase_id
                WHERE pu.reference_number = :ref
                ORDER BY p.sku
                """
            ),
            {"ref": REFERENCIA},
        ).mappings()
    ]


def _consumos(db: Session) -> int:
    return db.execute(
        text(
            """
            SELECT count(*)
            FROM sale_item_lot_allocations a
            JOIN purchase_items pi ON pi.id = a.purchase_item_id
            JOIN purchases pu ON pu.id = pi.purchase_id
            WHERE pu.reference_number = :ref
            """
        ),
        {"ref": REFERENCIA},
    ).scalar_one()


def _total_compra(db: Session) -> Decimal:
    return Decimal(
        str(
            db.execute(
                text("SELECT total FROM purchases WHERE reference_number = :ref"),
                {"ref": REFERENCIA},
            ).scalar_one()
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Imprime lo que haria y no escribe nada."
    )
    args = parser.parse_args()

    schema = settings.POSTGRES_SCHEMA
    _guard_against_protected_schema(schema)
    print(f"schema: {schema}   (de settings, el que usa la conexion)")

    db: Session = SessionLocal()
    try:
        lotes = _lotes(db)
        if not lotes:
            raise RuntimeError(f"No existe la compra {REFERENCIA} en este schema.")

        consumidas = _consumos(db)
        print(f"\n--- ANTES ---")
        print(f"  compra {REFERENCIA}, total Q{_total_compra(db)}")
        print(f"  {'sku':<10} {'producto':<26} {'cant':>6} {'resto':>6} {'costo':>8} {'subtotal':>9}")
        for l in lotes:
            print(
                f"  {l['sku']:<10} {l['name']:<26} {l['quantity']:>6} "
                f"{l['remaining_quantity']:>6} {l['unit_cost']:>8} {l['subtotal']:>9}"
            )
        print(f"  ventas que ya consumieron estos lotes: {consumidas}")

        if consumidas:
            raise RuntimeError(
                f"{consumidas} venta(s) ya consumieron estos lotes. Cuando una venta "
                "consume un lote, el costo se COPIA a `sale_items.cost_basis_unit` y a "
                "`gross_profit_total`: cambiar el costo del lote ahora dejaria esas "
                "ventas con un costo que no corresponde a ningun lote. La correccion "
                "ya no son tres campos -- hay que recalcular las ventas afectadas "
                "tambien. Nada se escribio."
            )

        por_sku = {l["sku"]: l for l in lotes}
        faltantes = [sku for sku in CORRECCIONES if sku not in por_sku]
        if faltantes:
            raise RuntimeError(f"Estos SKU no estan en la compra de apertura: {faltantes}")

        print(f"\n--- LO QUE VA A ESCRIBIR ---")
        cambios = []
        for sku, costo_nuevo in CORRECCIONES.items():
            lote = por_sku[sku]
            costo_viejo = Decimal(str(lote["unit_cost"]))
            if costo_viejo == costo_nuevo:
                print(f"  {sku} ya esta en Q{costo_nuevo}. Sin cambios.")
                continue
            cantidad = Decimal(str(lote["quantity"]))
            subtotal_nuevo = _money(cantidad * costo_nuevo)
            cambios.append((lote, costo_nuevo, subtotal_nuevo))
            print(
                f"  {lote['name']}: Q{costo_viejo} -> Q{costo_nuevo} por unidad, "
                f"subtotal Q{lote['subtotal']} -> Q{subtotal_nuevo}"
            )

        if not cambios:
            print("\nNada que corregir.")
            return 0

        print(f"  `purchases.total` se recalcula sumando los subtotales de sus items.")
        print(f"  La caja NO se toca: esta compra nunca genero movimiento.")

        if args.dry_run:
            print("\n--dry-run: no se escribio nada.")
            return 0

        for lote, costo_nuevo, subtotal_nuevo in cambios:
            db.execute(
                text(
                    "UPDATE purchase_items SET unit_cost = :costo, subtotal = :sub "
                    "WHERE id = :id"
                ),
                {"costo": costo_nuevo, "sub": subtotal_nuevo, "id": lote["id"]},
            )

        db.execute(
            text(
                """
                UPDATE purchases SET total = (
                    SELECT COALESCE(SUM(subtotal), 0) FROM purchase_items
                    WHERE purchase_id = purchases.id
                )
                WHERE reference_number = :ref
                """
            ),
            {"ref": REFERENCIA},
        )

        # Verificacion dentro de la transaccion.
        lotes_despues = _lotes(db)
        for sku, costo_nuevo in CORRECCIONES.items():
            l = next(x for x in lotes_despues if x["sku"] == sku)
            esperado = _money(Decimal(str(l["quantity"])) * costo_nuevo)
            if Decimal(str(l["unit_cost"])) != costo_nuevo:
                raise RuntimeError(f"Verificacion fallida: {sku} quedo en Q{l['unit_cost']}.")
            if Decimal(str(l["subtotal"])) != esperado:
                raise RuntimeError(
                    f"Verificacion fallida: el subtotal de {sku} quedo en "
                    f"Q{l['subtotal']} y se esperaba Q{esperado}."
                )
        suma_items = sum(Decimal(str(l["subtotal"])) for l in lotes_despues)
        total_despues = _total_compra(db)
        if total_despues != suma_items:
            raise RuntimeError(
                f"Verificacion fallida: el total de la compra quedo en Q{total_despues} "
                f"y la suma de sus items da Q{suma_items}. Nada se escribio."
            )
        if _consumos(db) != 0:
            raise RuntimeError("Verificacion fallida: aparecieron consumos. Nada se escribio.")

        db.commit()
        print(f"\n--- DESPUES ---")
        print(f"  compra {REFERENCIA}, total Q{total_despues}")
        print(f"  {'sku':<10} {'producto':<26} {'cant':>6} {'costo':>8} {'subtotal':>9}")
        for l in lotes_despues:
            print(
                f"  {l['sku']:<10} {l['name']:<26} {l['quantity']:>6} "
                f"{l['unit_cost']:>8} {l['subtotal']:>9}"
            )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
