import re
import time

import pytest
from alembic import command
from sqlalchemy import text

# Debe coincidir con MIGRATION_LOCK_KEY en alembic/env.py. No se importa ese
# modulo directamente porque, al ser un script de Alembic, ejecuta
# run_migrations_online() al nivel de modulo con la conexion por default de
# settings con solo importarlo — justo lo que estos tests evitan.
_MIGRATION_LOCK_KEY = 0x5245564E


def test_upgrade_creates_the_schema_if_missing(test_engine, alembic_config):
    """env.py debe crear el schema destino, no asumir que existe."""
    schema = alembic_config.attributes["target_schema"]
    with test_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    command.upgrade(alembic_config, "head")

    with test_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": schema}
        ).scalar()
    assert exists == 1


def test_version_table_lives_inside_the_target_schema(test_engine, alembic_config):
    """alembic_version no debe caer en public."""
    schema = alembic_config.attributes["target_schema"]
    command.upgrade(alembic_config, "head")

    with test_engine.connect() as conn:
        in_target = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = :s AND tablename = 'alembic_version'"
            ),
            {"s": schema},
        ).scalar()
        in_public = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = 'alembic_version'"
            )
        ).scalar()
    assert in_target == 1
    assert in_public == 0


def test_db_dev_guard_blocks_migrations_without_explicit_opt_in(
    test_engine, alembic_config, monkeypatch
):
    """env.py debe negarse a migrar contra db_dev salvo opt-in explicito.

    db_dev es el schema de produccion (156 ventas reales). El default de
    settings.POSTGRES_SCHEMA y el .env del proyecto apuntan ahi, asi que un
    `alembic upgrade head` corrido sin argumentos no debe poder tocarlo.
    """
    monkeypatch.delenv("REVENEW_ALLOW_DB_DEV", raising=False)
    alembic_config.attributes["target_schema"] = "db_dev"

    with pytest.raises(RuntimeError, match="db_dev"):
        command.upgrade(alembic_config, "head")

    # No basta con que el guard levante: tiene que levantar ANTES de crear
    # nada. Un regresion que moviera el guard a despues del CREATE SCHEMA
    # seguiria pasando el assert de arriba.
    with test_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = 'db_dev'")
        ).scalar()
    assert exists is None, "el guard corrio despues de crear el schema db_dev"


def test_advisory_lock_wait_is_bounded_and_explains_itself(
    test_engine, alembic_config, monkeypatch
):
    """Con el lock tomado por otra sesion, upgrade debe fallar rapido y explicar
    por que, en vez de colgarse indefinidamente sin imprimir nada (el bug que
    encontro el reviewer).
    """
    monkeypatch.setenv("REVENEW_MIGRATION_LOCK_TIMEOUT_SECONDS", "1")
    schema = alembic_config.attributes["target_schema"]

    holder = test_engine.connect()
    holder.execute(
        text("SELECT pg_advisory_lock(:key, hashtext(:schema))"),
        {"key": _MIGRATION_LOCK_KEY, "schema": schema},
    )
    try:
        start = time.monotonic()
        with pytest.raises(RuntimeError, match=re.escape(schema)):
            command.upgrade(alembic_config, "head")
        elapsed = time.monotonic() - start

        # Evidencia de que el timeout realmente acota la espera, no solo
        # esta configurado: si el lock siguiera bloqueando indefinidamente
        # (pg_advisory_lock) esto nunca volveria dentro del test's timeout
        # de pytest. Y si el guard fallara instantaneamente sin sondear de
        # verdad, elapsed rondaria 0 en vez del ~1s configurado.
        assert 0.9 <= elapsed <= 5.0, (
            f"se esperaba que la espera estuviera acotada a ~1s, tardo {elapsed:.2f}s"
        )
    finally:
        holder.execute(
            text("SELECT pg_advisory_unlock(:key, hashtext(:schema))"),
            {"key": _MIGRATION_LOCK_KEY, "schema": schema},
        )
        holder.close()

    # El lock quedo liberado: una migracion posterior contra el mismo
    # schema debe funcionar sin volver a esperar.
    command.upgrade(alembic_config, "head")
