"""Copia los datos de un schema a otro dentro de la misma base.

Uso:
    venv/bin/python -m scripts.copy_schema_data --source db_dev --target db_v2
    venv/bin/python -m scripts.copy_schema_data --source db_dev --target db_v2 --dry-run

Todo ocurre en una sola transaccion: o se copia entero, o no se copia nada.
El schema origen nunca se modifica.
"""

from __future__ import annotations

import argparse
import os

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

# Tablas que existen en el schema migrado pero que esta copia deliberadamente
# NO mueve porque nacen vacias.  cash_movements (revision 8df5b1f79497) es el
# libro de caja: la migracion la crea vacia y la importacion de la pestania del
# Sheet esta fuera del alcance, asi que en db_dev no hay nada que copiar.  Sin
# esta exencion, _unknown_source_tables la reportaria como tabla desconocida y
# cualquier copia abortaria, que es justo lo que el mensaje de error de ese
# guard anticipaba al decir "o confirma que nacen vacias, como cash_movements":
# esto es esa confirmacion, escrita como codigo en vez de como costumbre oral.
#
# La exencion NO es incondicional.  _unknown_source_tables solo la aplica
# mientras la tabla siga VACIA en el origen; en cuanto tenga una fila vuelve a
# reportarse como desconocida y la copia aborta.  Una exencion incondicional
# convertiria el guard en su propio agujero: el dia que alguien empiece a
# registrar movimientos de caja, la copia los dejaria atras en silencio y el
# TOTAL impreso al final seguiria pareciendo completo.
BORN_EMPTY_TABLES = frozenset({"cash_movements"})

# db_dev es el schema que .env apunta por defecto y contiene los datos reales
# de produccion (156 ventas al momento de escribir esto). Igual que el guard
# de alembic/env.py para las migraciones, este script no debe poder
# sobreescribirlo silenciosamente: --source y --target son faciles de
# invertir al escribirlos a mano, y lo mas facil de tipear no puede ser lo
# prohibido.
PROTECTED_SCHEMA = "db_dev"
ALLOW_PROTECTED_SCHEMA_ENV_VAR = "REVENEW_ALLOW_DB_DEV"

# Claves de information_schema.columns que definen si dos columnas del mismo
# nombre son realmente compatibles para una copia sin perdida. Comparar solo
# el nombre no alcanza: un destino con numeric(10,2) donde el origen tiene
# numeric(10,4) acepta el INSERT sin error y redondea la plata en silencio
# (0.5000 -> 0.50). udt_schema queda deliberadamente afuera de esta lista:
# source y target viven en schemas distintos por definicion, asi que su
# udt_schema difiere aun cuando el tipo es identico.
_TYPE_COMPARISON_KEYS = (
    "data_type",
    "udt_name",
    "numeric_precision",
    "numeric_scale",
    "character_maximum_length",
)


class SchemaMismatch(Exception):
    """El origen y el destino no son lo bastante iguales como para copiar entre ambos."""


def _guard_against_protected_target(target: str) -> None:
    if target == PROTECTED_SCHEMA and os.environ.get(ALLOW_PROTECTED_SCHEMA_ENV_VAR) != "1":
        raise RuntimeError(
            f"El destino pedido es {target!r}. Ese schema tiene los datos reales "
            "de produccion y este script no debe escribirle silenciosamente.\n"
            f"Si de verdad queres escribir sobre {target!r}, seteá "
            f"{ALLOW_PROTECTED_SCHEMA_ENV_VAR}=1 en el entorno y corré de nuevo."
        )


def _column_info(conn, schema: str, table: str) -> dict[str, dict]:
    """Columna -> info de tipo, en orden ordinal.

    Se trae udt_schema ademas de udt_name porque los tipos ENUM nativos de
    Postgres son propiedad de un schema: aunque source y target tengan una
    columna "earning_mode" del mismo nombre de tipo (earning_mode_enum), son
    dos tipos distintos a nivel de motor. udt_schema le dice a _select_expr
    donde vive de verdad el tipo destino en vez de asumir que coincide con
    el nombre del schema target.
    """
    rows = conn.execute(
        text(
            "SELECT column_name, data_type, udt_name, udt_schema, "
            "numeric_precision, numeric_scale, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t "
            "ORDER BY ordinal_position"
        ),
        {"s": schema, "t": table},
    )
    return {
        r[0]: {
            "data_type": r[1],
            "udt_name": r[2],
            "udt_schema": r[3],
            "numeric_precision": r[4],
            "numeric_scale": r[5],
            "character_maximum_length": r[6],
        }
        for r in rows
    }


def _describe_type(info: dict) -> str:
    if info["data_type"] == "numeric" and info["numeric_precision"] is not None:
        return f"numeric({info['numeric_precision']},{info['numeric_scale']})"
    if info["character_maximum_length"] is not None:
        return f"{info['data_type']}({info['character_maximum_length']})"
    if info["data_type"] == "USER-DEFINED":
        return info["udt_name"]
    return info["data_type"]


def _unknown_source_tables(conn, source: str) -> list[str]:
    """Tablas que existen en source pero de las que TABLE_ORDER no sabe nada.

    TABLE_ORDER es una lista fija a mano. Si el schema origen tiene una
    tabla que no esta ahi, el loop de _copy_all simplemente nunca la toca:
    no hay excepcion, el TOTAL impreso al final parece completo, y el
    operador no tiene ninguna senal de que algo quedo afuera.

    Las tablas de BORN_EMPTY_TABLES se exceptuan, pero solo mientras sigan
    vacias en el origen: una que ya tenga filas se reporta como desconocida
    igual que cualquier otra, porque saltarsela seria exactamente la copia
    incompleta y silenciosa que este guard existe para impedir.
    """
    rows = conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
        ),
        {"s": source},
    )
    names = {r[0] for r in rows}
    names.discard("alembic_version")
    unknown = names - set(TABLE_ORDER)

    for table in sorted(unknown & BORN_EMPTY_TABLES):
        count = conn.execute(
            text(f'SELECT count(*) FROM "{source}"."{table}"')
        ).scalar()
        if count == 0:
            unknown.discard(table)

    return sorted(unknown)


def copy_schema_data(
    engine: Engine, source: str, target: str, dry_run: bool = False
) -> dict[str, int]:
    """Copia TABLE_ORDER de source a target.  Devuelve {tabla: filas copiadas}."""
    if source == target:
        raise ValueError("El origen y el destino no pueden ser el mismo schema")
    _guard_against_protected_target(target)

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


def _select_expr(column: str, dst_info: dict[str, dict]) -> str:
    """Expresion a usar en el SELECT para `column`.

    Las columnas normales se seleccionan tal cual. Las columnas cuyo tipo de
    destino es un ENUM nativo (USER-DEFINED) se castean explicitamente
    text -> "{udt_schema}"."{udt_name}" del lado del destino, porque el enum
    del schema origen y el del destino son tipos distintos aunque compartan
    nombre.
    """
    info = dst_info.get(column, {})
    if info.get("data_type") == "USER-DEFINED":
        return f'"{column}"::text::"{info["udt_schema"]}"."{info["udt_name"]}"'
    return f'"{column}"'


def _copy_all(conn, source: str, target: str) -> dict[str, int]:
    unknown = _unknown_source_tables(conn, source)
    if unknown:
        # Las tablas de BORN_EMPTY_TABLES que aparecen aca ya perdieron su
        # exencion (dejaron de estar vacias en el origen): decirle al
        # operador que "confirme que nacen vacias" para esa tabla puntual
        # esta mal - es justo la excepcion que se acaba de negar a aplicar.
        # El mensaje tiene que distinguir ese caso del de una tabla
        # realmente nueva y desconocida.
        rearmed = sorted(set(unknown) & BORN_EMPTY_TABLES)
        genuinely_unknown = sorted(set(unknown) - BORN_EMPTY_TABLES)

        parts = [f"{source} tiene tablas que TABLE_ORDER no conoce: {unknown}."]
        if rearmed:
            parts.append(
                f"{rearmed} estan en BORN_EMPTY_TABLES, pero esa exencion solo "
                "aplica mientras la tabla este vacia en el origen, y ahora tiene "
                "filas. Actualiza TABLE_ORDER para incluirla(s) antes de copiar."
            )
        if genuinely_unknown:
            parts.append(
                f"Actualiza TABLE_ORDER (o confirma que {genuinely_unknown} "
                "nace(n) vacia(s), como cash_movements) antes de copiar."
            )
        parts.append("Si no, se copia de menos y sin avisar.")
        raise SchemaMismatch(" ".join(parts))

    copied: dict[str, int] = {}
    for table in TABLE_ORDER:
        src_info = _column_info(conn, source, table)
        dst_info = _column_info(conn, target, table)
        if not src_info:
            raise SchemaMismatch(f"{source}.{table} no existe")
        if not dst_info:
            raise SchemaMismatch(f"{target}.{table} no existe")

        src_cols = list(src_info.keys())
        missing = [c for c in src_cols if c not in dst_info]
        if missing:
            raise SchemaMismatch(
                f"{target}.{table} no tiene las columnas {missing} que si tiene "
                f"{source}.{table}.  Revisa el drift antes de copiar."
            )

        mismatched = [
            f'"{col}": {source} tiene {_describe_type(src_info[col])}, '
            f"{target} tiene {_describe_type(dst_info[col])}"
            for col in src_cols
            if any(
                src_info[col][k] != dst_info[col][k] for k in _TYPE_COMPARISON_KEYS
            )
        ]
        if mismatched:
            raise SchemaMismatch(
                f"{table} tiene columnas con tipos distintos entre {source} y "
                f"{target} (riesgo de truncar o redondear datos en silencio): "
                + "; ".join(mismatched)
            )

        insert_list = ", ".join(f'"{c}"' for c in src_cols)
        select_list = ", ".join(_select_expr(c, dst_info) for c in src_cols)
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
