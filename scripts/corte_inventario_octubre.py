"""Cierra septiembre y abre octubre 2026 con el inventario y la caja contados a mano.

Uso:
    set -a; source .env.production; set +a
    venv/bin/python -m scripts.corte_inventario_octubre --dry-run
    venv/bin/python -m scripts.corte_inventario_octubre

## Por que existe

Durante agosto y septiembre de 2026 la operacion se llevo en una hoja de Google
(«Revenew - Inventario y Ventas») en paralelo a esta base. Los dos registros
divergieron: al 30/09/2026 la base tenia ventas de abril al 27/08 y ningun
movimiento de caja, mientras la hoja tenia 29 ventas de agosto por Q1.886,00
contra las 27 por Q1.614,00 de la base, mas 23 ventas de septiembre que la base
no tiene. Y la hoja no coincidia ni consigo misma: para el cilindro de gas, sus
tres fuentes daban 4, 6 y 7 unidades.

Reconciliar los dos libros transaccion por transaccion es un proyecto de datos.
Lo que se decidio es un corte: la base conserva su historia de abril a agosto,
septiembre queda documentado en la hoja, y octubre arranca con lo que de verdad
hay en la casa -- contado fisicamente el 30/09/2026.

## Por que escribe las filas directamente, sin pasar por PurchaseService

`PurchaseService.confirm` crea un movimiento de caja de SALIDA por el total de la
compra (`app/services/purchase_service.py`). El inventario inicial ya se pago en
septiembre, asi que una compra normal al costo real duplicaria ese gasto en la
caja de octubre. Y registrarla a costo cero evitaria la salida pero dejaria el
FIFO en Q0,00: toda venta de octubre mostraria 100% de margen, que es peor que
el problema que vino a resolver.

Escribiendo las filas directamente, el costo unitario queda real (para que el
margen de octubre sea cierto) y la caja entra una sola vez, por su monto contado.
Mismo patron que `scripts/backfill_projections.py` y
`scripts/fix_historical_cost_basis.py`.

## Por que la compra se fecha 30/09 y no 01/10

El inventario existia al cerrar septiembre. Fechandola 30/09, los reportes de
compras de octubre quedan limpios: no aparece un gasto de Q625,00 que no ocurrio
en octubre. Septiembre en la base ya estaba vacio, asi que es el lugar menos malo
para una fila de apertura.

## Idempotencia

`purchases.reference_number` es UNIQUE, asi que una segunda corrida choca contra
la base misma, no contra una comprobacion que yo pueda haber escrito mal. El
movimiento de caja se comprueba aparte antes de insertar.

## Sin tests

Es un script de un solo uso contra datos reales, igual que
`fix_historical_cost_basis.py`. La red es `--dry-run`, que imprime exactamente
las filas que va a escribir y el estado resultante, y la verificacion final que
corre dentro de la misma transaccion y aborta si algo no cuadra.
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.datetime_utils import business_midnight

# Los mismos nombres que usan alembic/env.py, copy_schema_data.py,
# backfill_projections.py y fix_historical_cost_basis.py.
PROTECTED_SCHEMA = "db_dev"
ALLOW_PROTECTED_SCHEMA_ENV_VAR = "REVENEW_ALLOW_DB_DEV"

FECHA_CORTE = date(2026, 9, 30)
REFERENCIA = "INV-INICIAL-2026-10"

# CONTEO FISICO del 30/09/2026, dado por el dueño del negocio. No se deriva de
# la hoja ni de la base: las dos estaban equivocadas, y las dos se contradecian.
# Un lote por costo, para que el FIFO de octubre diga la verdad sobre el margen.
CONTEO = [
    # sku, nombre esperado, cantidad, costo unitario
    ("LKCX-001", "Cilindro de gas", Decimal("4"), Decimal("95.00")),
    ("LKCX-002", "Cartón de huevos (30 U)", Decimal("7"), Decimal("35.00")),
]

# Efectivo contado en caja el 30/09/2026. La columna `saldo_acumulado` de la
# hoja terminaba en Q144,15 o Q169,98 segun como se ordenaran sus filas (esa
# columna quedo desordenada), asi que tampoco se usa como fuente.
EFECTIVO_EN_CAJA = Decimal("190.00")


def _guard_against_protected_schema(schema: str) -> None:
    if schema == PROTECTED_SCHEMA and os.environ.get(ALLOW_PROTECTED_SCHEMA_ENV_VAR) != "1":
        raise RuntimeError(
            f"El schema resuelto es {schema!r}, que guarda el respaldo vivo de "
            "produccion. Este script escribe. Me niego a correr.\n"
            f"Si de verdad querés escribir en {schema!r}, poné "
            f"{ALLOW_PROTECTED_SCHEMA_ENV_VAR}=1."
        )


def _productos(db: Session) -> dict[str, dict]:
    filas = db.execute(
        text("SELECT id, sku, name, stock FROM products ORDER BY sku")
    ).mappings().all()
    return {f["sku"]: dict(f) for f in filas}


def _lotes_abiertos(db: Session) -> list[dict]:
    return [
        dict(f)
        for f in db.execute(
            text(
                """
                SELECT pi.id, p.name, pi.remaining_quantity, pi.unit_cost, pu.date
                FROM purchase_items pi
                JOIN products p ON p.id = pi.product_id
                JOIN purchases pu ON pu.id = pi.purchase_id
                WHERE pi.remaining_quantity > 0
                ORDER BY pu.date, p.name
                """
            )
        ).mappings()
    ]


def _saldo_caja(db: Session) -> Decimal:
    fila = db.execute(
        text(
            """
            SELECT COALESCE(SUM(
                CASE WHEN type IN ('salida', 'retiro_socio') THEN -amount ELSE amount END
            ), 0) AS saldo
            FROM cash_movements
            """
        )
    ).scalar_one()
    return Decimal(str(fila))


def _movimientos_previos(db: Session) -> list[dict]:
    return [
        dict(f)
        for f in db.execute(
            text(
                """
                SELECT id, occurred_at, type, amount, sale_id, purchase_id, note
                FROM cash_movements ORDER BY occurred_at
                """
            )
        ).mappings()
    ]


def _usuario_del_corte(db: Session) -> uuid.UUID:
    """El dueño del negocio, que es a cuyo nombre queda la fila de apertura.

    Se resuelve por el usuario mas antiguo en vez de por un UUID escrito a mano:
    un id pegado aca seria correcto solo en esta base.
    """
    fila = db.execute(
        text("SELECT id FROM users ORDER BY created_at LIMIT 1")
    ).scalar_one()
    return fila


def _imprimir_estado(titulo: str, productos: dict[str, dict], lotes: list[dict], saldo: Decimal) -> None:
    print(f"\n--- {titulo} ---")
    print(f"{'sku':<10} {'producto':<26} {'contador':>9} {'lotes':>8}  {'coinciden'}")
    por_nombre: dict[str, Decimal] = {}
    for lote in lotes:
        por_nombre[lote["name"]] = por_nombre.get(lote["name"], Decimal(0)) + Decimal(str(lote["remaining_quantity"]))
    for sku, p in sorted(productos.items()):
        contador = Decimal(str(p["stock"]))
        en_lotes = por_nombre.get(p["name"], Decimal(0))
        marca = "si" if contador == en_lotes else "NO"
        print(f"{sku:<10} {p['name']:<26} {contador:>9} {en_lotes:>8}  {marca}")
    if lotes:
        print("  lotes abiertos:")
        for lote in lotes:
            print(f"    {lote['date']}  {lote['name']:<26} {lote['remaining_quantity']:>8} u. @ Q{lote['unit_cost']}")
    else:
        print("  lotes abiertos: ninguno")
    print(f"  saldo de caja: Q{saldo}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Imprime lo que haria y no escribe nada.",
    )
    parser.add_argument(
        "--borrar-movimientos-previos",
        action="store_true",
        help=(
            "Borra los movimientos de caja que existan antes del corte. Sin esta "
            "bandera, el script se niega a correr si el libro no esta vacio: "
            "borrar filas de caja en produccion no puede ser un efecto escondido."
        ),
    )
    args = parser.parse_args()

    # El schema sale de `settings`, NO de `os.environ`: es el que la conexion va
    # a usar de verdad. `fix_historical_cost_basis.py` lo lee del entorno, y con
    # el valor viniendo de un `.env` (que pydantic-settings lee pero no exporta)
    # ese guard ve una cadena vacia y no protege nada -- justo el caso en que
    # mas haria falta.
    schema = settings.POSTGRES_SCHEMA
    _guard_against_protected_schema(schema)
    print(f"schema: {schema}   (de settings, el que usa la conexion)")

    db: Session = SessionLocal()
    try:
        productos = _productos(db)
        faltantes = [sku for sku, _, _, _ in CONTEO if sku not in productos]
        if faltantes:
            raise RuntimeError(f"No encontre estos SKU en la base: {faltantes}")
        sin_contar = [sku for sku in productos if sku not in {c[0] for c in CONTEO}]
        if sin_contar:
            raise RuntimeError(
                f"Hay productos en la base que el conteo no cubre: {sin_contar}. "
                "Dejarlos afuera los dejaria con el contador y los lotes en "
                "desacuerdo. Contalos y agregalos a CONTEO."
            )
        for sku, nombre_esperado, _, _ in CONTEO:
            real = productos[sku]["name"]
            if real != nombre_esperado:
                raise RuntimeError(
                    f"El SKU {sku} se llama {real!r} en la base y {nombre_esperado!r} "
                    "en el conteo. Verificá que estás apuntando a la base correcta."
                )

        ya_hecho = db.execute(
            text("SELECT count(*) FROM purchases WHERE reference_number = :r"),
            {"r": REFERENCIA},
        ).scalar_one()
        if ya_hecho:
            print(f"\nEl corte {REFERENCIA} ya existe. No hay nada que hacer.")
            return 0

        _imprimir_estado("ANTES", productos, _lotes_abiertos(db), _saldo_caja(db))

        total = sum(cant * costo for _, _, cant, costo in CONTEO)
        print(f"\n--- LO QUE VA A ESCRIBIR ---")
        print(f"1. Cerrar los lotes abiertos de arriba (remaining_quantity -> 0).")
        print(f"2. Compra {REFERENCIA}, fecha {FECHA_CORTE}, status confirmed, total Q{total}:")
        for sku, nombre, cant, costo in CONTEO:
            print(f"     {nombre:<26} {cant:>6} u. @ Q{costo}  = Q{cant * costo}")
        print(f"3. products.stock = la cantidad contada, para los dos productos.")
        previos = _movimientos_previos(db)
        if previos and not args.borrar_movimientos_previos:
            print("\n--- ABORTA ---")
            print("El libro de caja NO esta vacio. Hay estos movimientos:")
            for m in previos:
                ref = m["sale_id"] or m["purchase_id"] or ""
                print(f"    {m['occurred_at']}  {m['type']:<14} Q{m['amount']:<10} {ref}")
            print(
                "\nEl saldo_inicial se suma al libro, no lo reemplaza, asi que con\n"
                "estos movimientos presentes el saldo final no seria el que contaste.\n"
                "Si estos movimientos no corresponden al dinero que contaste, corré\n"
                "con --borrar-movimientos-previos. Si SI corresponden, hay que\n"
                "recalcular EFECTIVO_EN_CAJA en vez de borrarlos."
            )
            return 1
        if previos:
            print(f"4. BORRAR estos {len(previos)} movimiento(s) de caja:")
            for m in previos:
                ref = m["sale_id"] or m["purchase_id"] or ""
                print(f"     {m['occurred_at']}  {m['type']:<14} Q{m['amount']:<10} {ref}")
            print("     (la venta asociada SIGUE marcada como pagada; solo se va la fila de caja)")
            print(f"5. Movimiento saldo_inicial de Q{EFECTIVO_EN_CAJA}, fecha {FECHA_CORTE}.")
        else:
            print(f"4. Movimiento saldo_inicial de Q{EFECTIVO_EN_CAJA}, fecha {FECHA_CORTE}.")

        if args.dry_run:
            print("\n--dry-run: no se escribio nada.")
            return 0

        usuario = _usuario_del_corte(db)
        compra_id = uuid.uuid4()

        db.execute(text("UPDATE purchase_items SET remaining_quantity = 0 WHERE remaining_quantity > 0"))

        db.execute(
            text(
                """
                INSERT INTO purchases
                    (id, user_id, date, occurred_at, reference_number, notes, total, status)
                VALUES
                    (:id, :user_id, :fecha, :occurred_at, :ref, :notas, :total, 'confirmed')
                """
            ),
            {
                "id": compra_id,
                "user_id": usuario,
                "fecha": FECHA_CORTE,
                "occurred_at": business_midnight(FECHA_CORTE),
                "ref": REFERENCIA,
                "notas": (
                    "Inventario inicial: corte al 30/09/2026, contado fisicamente. "
                    "Agosto y septiembre se llevaron en la hoja de Google y los dos "
                    "registros divergieron; esta fila abre octubre con lo que de "
                    "verdad hay. No genera salida de caja porque ya se pago en "
                    "septiembre."
                ),
                "total": total,
            },
        )

        for sku, _, cant, costo in CONTEO:
            db.execute(
                text(
                    """
                    INSERT INTO purchase_items
                        (id, purchase_id, product_id, quantity, remaining_quantity, unit_cost, subtotal)
                    VALUES
                        (:id, :compra, :producto, :cant, :cant, :costo, :subtotal)
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "compra": compra_id,
                    "producto": productos[sku]["id"],
                    "cant": cant,
                    "costo": costo,
                    "subtotal": cant * costo,
                },
            )
            db.execute(
                text("UPDATE products SET stock = :cant WHERE id = :id"),
                {"cant": cant, "id": productos[sku]["id"]},
            )

        if previos:
            db.execute(text("DELETE FROM cash_movements WHERE id = ANY(:ids)"),
                       {"ids": [m["id"] for m in previos]})

        db.execute(
            text(
                """
                INSERT INTO cash_movements
                    (id, occurred_at, type, amount, payment_method, note)
                VALUES
                    (:id, :occurred_at, 'saldo_inicial', :monto, 'efectivo', :nota)
                """
            ),
            {
                "id": uuid.uuid4(),
                "occurred_at": business_midnight(FECHA_CORTE),
                "monto": EFECTIVO_EN_CAJA,
                "nota": "Efectivo contado en caja al cerrar septiembre de 2026.",
            },
        )

        # Verificacion DENTRO de la transaccion: si algo no cuadra, nada se
        # escribe. Es la unica red que tiene un script sin tests.
        productos_despues = _productos(db)
        lotes_despues = _lotes_abiertos(db)
        por_nombre: dict[str, Decimal] = {}
        for lote in lotes_despues:
            por_nombre[lote["name"]] = por_nombre.get(lote["name"], Decimal(0)) + Decimal(
                str(lote["remaining_quantity"])
            )
        for sku, nombre, cant, _ in CONTEO:
            contador = Decimal(str(productos_despues[sku]["stock"]))
            en_lotes = por_nombre.get(nombre, Decimal(0))
            if not (contador == en_lotes == cant):
                raise RuntimeError(
                    f"Verificacion fallida para {nombre}: contador={contador}, "
                    f"lotes={en_lotes}, contado={cant}. Nada se escribio."
                )
        saldo_despues = _saldo_caja(db)
        if saldo_despues != EFECTIVO_EN_CAJA:
            raise RuntimeError(
                f"Verificacion fallida: el saldo de caja quedo en Q{saldo_despues} "
                f"y el conteo dice Q{EFECTIVO_EN_CAJA}. Nada se escribio."
            )

        db.commit()
        _imprimir_estado("DESPUES", productos_despues, lotes_despues, saldo_despues)
        print(f"\nListo. Compra de apertura: {REFERENCIA}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
