from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from app.core.database import Base
import app.models  # noqa: F401

EXPECTED_TABLES = {
    "users", "customers", "products", "purchases", "purchase_items",
    "sales", "sale_items", "sale_item_lot_allocations", "customer_product_cycles",
}


def _tables_in(conn, schema: str) -> set[str]:
    rows = conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": schema}
    )
    return {r[0] for r in rows}


def test_upgrade_head_creates_every_table(test_engine, migrated_schema):
    with test_engine.connect() as conn:
        tables = _tables_in(conn, migrated_schema)
    assert EXPECTED_TABLES <= tables


def test_models_and_migrations_do_not_drift(test_engine, migrated_schema):
    """El test mas importante: tras migrar, autogenerate no debe detectar nada."""
    with test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        ctx = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "include_schemas": False,
                # version_table_schema debe ir en None, no en migrated_schema.  Con
                # include_schemas=False el autogenerate refleja el schema por
                # defecto de la conexion (que el SET search_path de arriba ya fijo
                # en migrated_schema) y lo identifica como None; alembic excluye su
                # propia tabla de version comparando (schema, nombre) literalmente
                # (autogenerate/compare.py, _autogen_for_tables), asi que pasarle el
                # nombre real hace que no reconozca alembic_version y la reporte
                # como "removed table".  Ese falso positivo taparia drift real.
                "version_table_schema": None,
            },
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"Los modelos y las migraciones divergen: {diff}"


def test_downgrade_removes_everything(test_engine, alembic_config):
    schema = alembic_config.attributes["target_schema"]
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    with test_engine.connect() as conn:
        tables = _tables_in(conn, schema)
    assert EXPECTED_TABLES & tables == set(), f"Quedaron tablas tras downgrade: {tables}"


def test_upgrade_downgrade_upgrade_round_trip(test_engine, alembic_config):
    """La reversibilidad tiene que ser real: bajar y volver a subir el MISMO schema.

    test_downgrade_removes_everything solo va en una direccion, y cada test recibe
    un schema desechable que se destruye con CASCADE, asi que ninguno de los dos
    nota si el downgrade se deja algo que no sea una tabla.  El caso concreto es el
    enum nativo earning_mode_enum: op.create_table emite su CREATE TYPE, pero
    op.drop_table no emite el DROP TYPE.  Sin el drop explicito del downgrade, este
    segundo upgrade revienta con 'type "earning_mode_enum" already exists'.
    """
    schema = alembic_config.attributes["target_schema"]

    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    with test_engine.connect() as conn:
        leftover_types = conn.execute(
            text(
                "SELECT t.typname FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = :s AND t.typtype = 'e'"
            ),
            {"s": schema},
        ).fetchall()
    assert leftover_types == [], (
        f"El downgrade dejo tipos sin borrar en {schema}: {leftover_types}. "
        "El siguiente upgrade fallaria."
    )

    command.upgrade(alembic_config, "head")

    with test_engine.connect() as conn:
        tables = _tables_in(conn, schema)
    assert EXPECTED_TABLES <= tables


def test_migration_emits_no_hardcoded_schema():
    """Una revision con schema='db_dev' incrustado no sirve para db_v2."""
    from pathlib import Path

    for path in Path("alembic/versions").glob("*.py"):
        source = path.read_text()
        assert "schema='db_dev'" not in source, f"{path} lleva el schema incrustado"
        assert 'schema="db_dev"' not in source, f"{path} lleva el schema incrustado"
