"""Copia los datos de un schema a otro dentro de la misma base.

Uso:
    venv/bin/python -m scripts.copy_schema_data --source db_dev --target db_v2
    venv/bin/python -m scripts.copy_schema_data --source db_dev --target db_v2 --dry-run

Todo ocurre en una sola transaccion: o se copia entero, o no se copia nada.
El schema origen nunca se modifica.
"""

from __future__ import annotations

import argparse

from sqlalchemy import Engine, create_engine, text

# Orden seguro de claves foraneas: cada tabla va despues de aquellas a las que apunta.
TABLE_ORDER = [
    "users",
    "customers",
    "products",
    "purchases",
    "purchase_items",
    "sales",
    "sale_items",
    "sale_item_lot_allocations",
    "customer_product_cycles",
]


class SchemaMismatch(Exception):
    """El origen tiene columnas que el destino no puede recibir."""


def _columns(conn, schema: str, table: str) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t "
            "ORDER BY ordinal_position"
        ),
        {"s": schema, "t": table},
    )
    return [r[0] for r in rows]


def _column_types(conn, schema: str, table: str) -> dict[str, tuple[str, str]]:
    """Mapea columna -> (data_type, udt_name).

    Postgres crea los tipos ENUM nativos por schema: aunque source y target
    tengan una columna "earning_mode" con el mismo nombre de tipo
    (earning_mode_enum), son dos tipos distintos a nivel de motor y un
    INSERT ... SELECT directo falla con DatatypeMismatch. Por eso este script
    necesita saber, columna por columna, si es un tipo definido por el
    usuario (data_type = 'USER-DEFINED') para poder castearla explicitamente.
    """
    rows = conn.execute(
        text(
            "SELECT column_name, data_type, udt_name FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t"
        ),
        {"s": schema, "t": table},
    )
    return {r[0]: (r[1], r[2]) for r in rows}


def copy_schema_data(
    engine: Engine, source: str, target: str, dry_run: bool = False
) -> dict[str, int]:
    """Copia TABLE_ORDER de source a target.  Devuelve {tabla: filas copiadas}."""
    if source == target:
        raise ValueError("El origen y el destino no pueden ser el mismo schema")

    copied: dict[str, int] = {}
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            copied = _copy_all(conn, source, target)
        except Exception:
            trans.rollback()
            raise
        if dry_run:
            trans.rollback()
        else:
            trans.commit()
    return copied


def _select_expr(column: str, dst_types: dict[str, tuple[str, str]], target: str) -> str:
    """Expresion a usar en el SELECT para `column`.

    Las columnas normales se seleccionan tal cual. Las columnas cuyo tipo de
    destino es un ENUM nativo (USER-DEFINED) se castean explicitamente
    text -> "{target}"."{udt_name}" porque el enum del schema origen y el
    del destino son tipos distintos aunque compartan nombre.
    """
    data_type, udt_name = dst_types.get(column, (None, None))
    if data_type == "USER-DEFINED":
        return f'"{column}"::text::"{target}"."{udt_name}"'
    return f'"{column}"'


def _copy_all(conn, source: str, target: str) -> dict[str, int]:
    copied: dict[str, int] = {}
    for table in TABLE_ORDER:
        src_cols = _columns(conn, source, table)
        dst_cols = _columns(conn, target, table)
        if not src_cols:
            raise SchemaMismatch(f"{source}.{table} no existe")
        if not dst_cols:
            raise SchemaMismatch(f"{target}.{table} no existe")

        missing = [c for c in src_cols if c not in dst_cols]
        if missing:
            raise SchemaMismatch(
                f"{target}.{table} no tiene las columnas {missing} que si tiene "
                f"{source}.{table}.  Revisa el drift antes de copiar."
            )

        dst_types = _column_types(conn, target, table)
        insert_list = ", ".join(f'"{c}"' for c in src_cols)
        select_list = ", ".join(
            _select_expr(c, dst_types, target) for c in src_cols
        )
        result = conn.execute(
            text(
                f'INSERT INTO "{target}"."{table}" ({insert_list}) '
                f'SELECT {select_list} FROM "{source}"."{table}"'
            )
        )
        copied[table] = result.rowcount

    return copied


def main() -> None:
    from app.core.config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--url", default=None, help="Por defecto, la de settings")
    args = parser.parse_args()

    engine = create_engine(args.url or settings.SQLALCHEMY_DATABASE_URI)
    try:
        copied = copy_schema_data(engine, args.source, args.target, args.dry_run)
    finally:
        engine.dispose()

    total = sum(copied.values())
    for table, count in copied.items():
        print(f"  {table:<32} {count:>6}")
    print(f"  {'TOTAL':<32} {total:>6}")
    if args.dry_run:
        print("\n  (dry-run: no se escribio nada)")


if __name__ == "__main__":
    main()
