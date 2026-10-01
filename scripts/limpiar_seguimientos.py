"""Borra los ciclos de seguimiento anteriores al corte de octubre 2026.

Uso:
    set -a; source .env.production; set +a
    venv/bin/python -m scripts.limpiar_seguimientos --dry-run
    venv/bin/python -m scripts.limpiar_seguimientos

## Por que

Las ventas de septiembre de 2026 se llevaron en una hoja de Google y nunca
entraron a esta base (ver `scripts/corte_inventario_octubre.py`). Los ciclos de
`customer_product_cycles` quedaron calculados contra la ultima venta que la app
conoce -- el 27/08 -- asi que el dashboard muestra una lista de clientes
«Atrasado» cuyas fechas estimadas vencieron hace un mes y que no dicen nada
sobre el estado real del negocio.

## Por que es seguro borrarlos

Son estado DERIVADO, no un registro. `SaleOrchestrator._update_cycle` los
reconstruye desde cero en cada venta: toma todas las fechas de compra de ese
cliente para ese producto y las pasa por `project()`. Un ciclo borrado reaparece
solo con la proxima venta del par, con exactamente el mismo contenido que
tendria si nunca se hubiera borrado.

Nada referencia esta tabla: sus unicas claves foraneas son salientes, hacia
`customers` y `products` (verificado en la migracion inicial). Y
`calendar_event_id` -- la columna que haria pensar que hay eventos de Google
Calendar colgando -- no la escribe NADIE en todo el repo: su unica aparicion es
la declaracion en el modelo. En produccion esta en NULL en las 26 filas. Asi que
no queda nada huerfano.

## Lo que este script NO arregla

La proyeccion se recalcula desde el historial de ventas, y ese historial sigue
sin septiembre. Cuando un cliente compre en octubre, `project()` va a ver sus
fechas terminando en agosto y saltando a octubre, y va a inferir un intervalo de
unas seis semanas que nunca existio. Con `ALPHA = 0.3` ese intervalo falso pesa
30% al principio y se diluye en cuatro o cinco compras reales.

Borrar las filas calla el dashboard hoy; no corrige esa distorsion, porque la
distorsion vive en el hueco del historial, no en estas filas. Arreglarla de raiz
pediria que `project()` respetara una fecha de corte, que es otro cambio.

## Por que filtra por fecha en vez de borrar todo

Un `DELETE` sin `WHERE` borraria tambien el ciclo que haya creado una venta de
octubre entre que se lee el dry-run y se corre en firme. Filtrando por
`last_purchase_date < FECHA_CORTE` se van solo los que quedaron viejos, y lo
nuevo se conserva.

## Sin tests

Script de un solo uso contra datos reales, igual que
`fix_historical_cost_basis.py` y `corte_inventario_octubre.py`. La red es
`--dry-run` y la verificacion que corre dentro de la misma transaccion.
"""

from __future__ import annotations

import argparse
import os
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal

PROTECTED_SCHEMA = "db_dev"
ALLOW_PROTECTED_SCHEMA_ENV_VAR = "REVENEW_ALLOW_DB_DEV"

# El mismo corte que `scripts/corte_inventario_octubre.py`.
FECHA_CORTE = date(2026, 9, 30)


def _guard_against_protected_schema(schema: str) -> None:
    if schema == PROTECTED_SCHEMA and os.environ.get(ALLOW_PROTECTED_SCHEMA_ENV_VAR) != "1":
        raise RuntimeError(
            f"El schema resuelto es {schema!r}, que guarda el respaldo vivo de "
            "produccion. Este script escribe. Me niego a correr.\n"
            f"Si de verdad querés escribir en {schema!r}, poné "
            f"{ALLOW_PROTECTED_SCHEMA_ENV_VAR}=1."
        )


def _resumen(db: Session) -> dict:
    return dict(
        db.execute(
            text(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE last_purchase_date < :corte) AS viejos,
                       count(*) FILTER (WHERE last_purchase_date >= :corte) AS nuevos,
                       count(*) FILTER (WHERE calendar_event_id IS NOT NULL) AS con_evento
                FROM customer_product_cycles
                """
            ),
            {"corte": FECHA_CORTE},
        ).mappings().one()
    )


def _a_borrar(db: Session) -> list[dict]:
    return [
        dict(f)
        for f in db.execute(
            text(
                """
                SELECT c.name AS cliente, p.name AS producto,
                       cy.last_purchase_date, cy.estimated_next_purchase,
                       cy.projection_confidence
                FROM customer_product_cycles cy
                JOIN customers c ON c.id = cy.customer_id
                JOIN products p ON p.id = cy.product_id
                WHERE cy.last_purchase_date < :corte
                ORDER BY cy.estimated_next_purchase NULLS LAST, c.name
                """
            ),
            {"corte": FECHA_CORTE},
        ).mappings()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Imprime lo que haria y no escribe nada."
    )
    args = parser.parse_args()

    # De `settings`, no de `os.environ`: es el schema que la conexion usa de
    # verdad. Leerlo del entorno hace que el guard vea una cadena vacia cuando
    # el valor viene de un `.env`, que es justo cuando mas haria falta.
    schema = settings.POSTGRES_SCHEMA
    _guard_against_protected_schema(schema)
    print(f"schema: {schema}   (de settings, el que usa la conexion)")

    db: Session = SessionLocal()
    try:
        antes = _resumen(db)
        print(
            f"\n--- ANTES ---\n"
            f"  ciclos en total:            {antes['total']}\n"
            f"  anteriores al {FECHA_CORTE}:  {antes['viejos']}  <- se borran\n"
            f"  del corte en adelante:      {antes['nuevos']}  <- se conservan\n"
            f"  con calendar_event_id:      {antes['con_evento']}"
        )

        if antes["con_evento"]:
            raise RuntimeError(
                f"{antes['con_evento']} ciclo(s) tienen `calendar_event_id`. Este "
                "script asume que nadie lo escribe (en todo el repo no hay una sola "
                "escritura de esa columna). Si ahora hay eventos de calendario "
                "colgando de estas filas, borrarlas los dejaria huerfanos: pará y "
                "revisá antes de seguir."
            )

        if not antes["viejos"]:
            print("\nNo hay ciclos anteriores al corte. No hay nada que hacer.")
            return 0

        filas = _a_borrar(db)
        print(f"\n--- LO QUE VA A BORRAR ({len(filas)}) ---")
        print(f"  {'cliente':<20} {'producto':<26} {'ult. compra':<12} {'estimada':<12} confianza")
        for f in filas:
            estimada = f["estimated_next_purchase"] or "-"
            print(
                f"  {f['cliente'][:19]:<20} {f['producto'][:25]:<26} "
                f"{str(f['last_purchase_date']):<12} {str(estimada):<12} {f['projection_confidence']}"
            )
        print(
            "\n  Se regeneran solos: la proxima venta de cada par cliente+producto\n"
            "  vuelve a crear su ciclo desde la historia completa."
        )

        if args.dry_run:
            print("\n--dry-run: no se escribio nada.")
            return 0

        borradas = db.execute(
            text("DELETE FROM customer_product_cycles WHERE last_purchase_date < :corte"),
            {"corte": FECHA_CORTE},
        ).rowcount

        despues = _resumen(db)
        if despues["viejos"] != 0:
            raise RuntimeError(
                f"Verificacion fallida: quedaron {despues['viejos']} ciclos anteriores "
                "al corte. Nada se escribio."
            )
        if despues["nuevos"] != antes["nuevos"]:
            raise RuntimeError(
                f"Verificacion fallida: los ciclos posteriores al corte pasaron de "
                f"{antes['nuevos']} a {despues['nuevos']}. Nada se escribio."
            )
        if borradas != antes["viejos"]:
            raise RuntimeError(
                f"Verificacion fallida: se borraron {borradas} filas y se esperaban "
                f"{antes['viejos']}. Nada se escribio."
            )

        db.commit()
        print(f"\n--- DESPUES ---")
        print(f"  borrados:    {borradas}")
        print(f"  conservados: {despues['nuevos']}")
        print(f"  total:       {despues['total']}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
