import pytest
from alembic import command
from sqlalchemy import text


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


def test_db_dev_guard_blocks_migrations_without_explicit_opt_in(alembic_config, monkeypatch):
    """env.py debe negarse a migrar contra db_dev salvo opt-in explicito.

    db_dev es el schema de produccion (156 ventas reales). El default de
    settings.POSTGRES_SCHEMA y el .env del proyecto apuntan ahi, asi que un
    `alembic upgrade head` corrido sin argumentos no debe poder tocarlo.
    """
    monkeypatch.delenv("REVENEW_ALLOW_DB_DEV", raising=False)
    alembic_config.attributes["target_schema"] = "db_dev"

    with pytest.raises(RuntimeError, match="db_dev"):
        command.upgrade(alembic_config, "head")
