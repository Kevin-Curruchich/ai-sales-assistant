"""Marca como pagadas las ventas pendientes anteriores al corte, sin mover la caja.

Uso:
    set -a; source .env.production; set +a
    venv/bin/python -m scripts.saldar_ventas_viejas --dry-run
    venv/bin/python -m scripts.saldar_ventas_viejas

## Por que

Quedaron ventas de junio y agosto de 2026 en `is_payment_pending = true`. Se
cobraron en su momento, fuera de esta base -- el libro de caja de la app recien
empieza el 30/09 con el `saldo_inicial` del corte
(`scripts/corte_inventario_octubre.py`). Mientras sigan marcadas pendientes
ensucian la lista de ventas y, sobre todo, son una bomba de tiempo: el dia que
alguien las marque pagadas desde la UI, la caja de octubre recibe una entrada
por plata que entro meses atras.

## Por que escribe el flag directo y no usa el servicio

`SaleService._apply_payment_status_transition` crea un movimiento de caja de
ENTRADA al pasar de pendiente a pagada
(`app/services/sales/orchestrator.py`). Eso es correcto para una cobranza de
verdad y es exactamente lo que NO queremos aca: la plata de estas dos ventas no
esta en los Q190 que se contaron, porque entro antes y ya se gasto o se deposito.
Ya paso una vez -- marcar una venta de agosto desde la UI dejo una entrada de
Q36,00 en octubre que hubo que borrar.

## Por que deja `payment_date` y `payment_method` en NULL

Es como lucen las otras 154 ventas pagadas de la base: la unica con
`payment_date` es justamente la que se marco desde la UI. Ponerles la fecha de
hoy afirmaria que se cobraron hoy, que es falso. Sin dato es mas honesto que con
un dato inventado.

## Por que filtra por fecha y no por id

Se explica solo, y si aparece una tercera venta vieja pendiente la toma tambien.
Una venta de OCTUBRE que quede pendiente no se toca: esa si es una cobranza real
por venir, y cuando se cobre tiene que mover la caja.

## Sin tests

Script de un solo uso contra datos reales, igual que los otros dos del corte. La
red es `--dry-run` y la verificacion dentro de la misma transaccion, que incluye
comprobar que el libro de caja quedo EXACTAMENTE como estaba.
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

# El mismo corte que `corte_inventario_octubre.py` y `limpiar_seguimientos.py`.
FECHA_CORTE = date(2026, 9, 30)


def _guard_against_protected_schema(schema: str) -> None:
    if schema == PROTECTED_SCHEMA and os.environ.get(ALLOW_PROTECTED_SCHEMA_ENV_VAR) != "1":
        raise RuntimeError(
            f"El schema resuelto es {schema!r}, que guarda el respaldo vivo de "
            "produccion. Este script escribe. Me niego a correr.\n"
            f"Si de verdad querés escribir en {schema!r}, poné "
            f"{ALLOW_PROTECTED_SCHEMA_ENV_VAR}=1."
        )


def _pendientes(db: Session, solo_viejas: bool) -> list[dict]:
    corte = "AND s.date < :corte" if solo_viejas else ""
    return [
        dict(f)
        for f in db.execute(
            text(
                f"""
                SELECT s.id, s.date, c.name AS cliente, s.total
                FROM sales s JOIN customers c ON c.id = s.customer_id
                WHERE s.is_payment_pending = true {corte}
                ORDER BY s.date
                """
            ),
            {"corte": FECHA_CORTE},
        ).mappings()
    ]


def _caja(db: Session) -> dict:
    return dict(
        db.execute(
            text(
                """
                SELECT count(*) AS movimientos,
                       COALESCE(SUM(CASE WHEN type IN ('salida','retiro_socio')
                                         THEN -amount ELSE amount END), 0) AS saldo
                FROM cash_movements
                """
            )
        ).mappings().one()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Imprime lo que haria y no escribe nada."
    )
    args = parser.parse_args()

    # De `settings`, no de `os.environ`: es el schema que la conexion usa de
    # verdad. Leerlo del entorno deja el guard ciego cuando el valor viene de
    # un `.env`, que es justo cuando haria falta.
    schema = settings.POSTGRES_SCHEMA
    _guard_against_protected_schema(schema)
    print(f"schema: {schema}   (de settings, el que usa la conexion)")

    db: Session = SessionLocal()
    try:
        viejas = _pendientes(db, solo_viejas=True)
        todas = _pendientes(db, solo_viejas=False)
        nuevas = [v for v in todas if v["id"] not in {x["id"] for x in viejas}]
        caja_antes = _caja(db)

        print(f"\n--- ANTES ---")
        print(f"  movimientos de caja: {caja_antes['movimientos']}   saldo: Q{caja_antes['saldo']}")
        print(f"  ventas pendientes anteriores al {FECHA_CORTE}: {len(viejas)}  <- se saldan")
        print(f"  ventas pendientes del corte en adelante:      {len(nuevas)}  <- NO se tocan")
        for v in nuevas:
            print(f"      {v['date']}  {v['cliente']:<22} Q{v['total']}  (cobranza real por venir)")

        if not viejas:
            print("\nNo hay ventas viejas pendientes. No hay nada que hacer.")
            return 0

        print(f"\n--- LO QUE VA A ESCRIBIR ---")
        print(f"  is_payment_pending = false en estas {len(viejas)} ventas:")
        for v in viejas:
            print(f"      {v['date']}  {v['cliente']:<22} Q{v['total']}   {v['id']}")
        print("  `payment_date` y `payment_method` quedan en NULL, como las otras pagadas.")
        print("  NO se crea ningun movimiento de caja: esa plata entro antes del corte.")

        if args.dry_run:
            print("\n--dry-run: no se escribio nada.")
            return 0

        actualizadas = db.execute(
            text(
                """
                UPDATE sales SET is_payment_pending = false
                WHERE is_payment_pending = true AND date < :corte
                """
            ),
            {"corte": FECHA_CORTE},
        ).rowcount

        # Verificacion dentro de la transaccion. La del libro de caja es la que
        # justifica el script entero: si una entrada se colo, nada se escribe.
        caja_despues = _caja(db)
        if caja_despues != caja_antes:
            raise RuntimeError(
                f"Verificacion fallida: el libro de caja cambio de {caja_antes} a "
                f"{caja_despues}. Nada se escribio."
            )
        if actualizadas != len(viejas):
            raise RuntimeError(
                f"Verificacion fallida: se actualizaron {actualizadas} ventas y se "
                f"esperaban {len(viejas)}. Nada se escribio."
            )
        quedan_viejas = _pendientes(db, solo_viejas=True)
        if quedan_viejas:
            raise RuntimeError(
                f"Verificacion fallida: quedaron {len(quedan_viejas)} ventas viejas "
                "pendientes. Nada se escribio."
            )
        quedan_nuevas = [
            v for v in _pendientes(db, solo_viejas=False)
            if v["id"] not in {x["id"] for x in viejas}
        ]
        if len(quedan_nuevas) != len(nuevas):
            raise RuntimeError(
                f"Verificacion fallida: las ventas pendientes del corte en adelante "
                f"pasaron de {len(nuevas)} a {len(quedan_nuevas)}. Nada se escribio."
            )

        db.commit()
        print(f"\n--- DESPUES ---")
        print(f"  saldadas:                 {actualizadas}")
        print(f"  pendientes que quedan:    {len(quedan_nuevas)}")
        print(f"  movimientos de caja:      {caja_despues['movimientos']}   saldo: Q{caja_despues['saldo']}  (sin cambios)")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
