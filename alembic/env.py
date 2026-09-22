import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.core.config import settings
from app.core.database import Base

import app.models  # noqa: F401  — registra todas las tablas en Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Clave del advisory lock: si dos deploys migran a la vez, se serializan.
MIGRATION_LOCK_KEY = 0x5245564E  # "REVN"

# db_dev es el schema que .env apunta por defecto y contiene datos reales de
# produccion (156 ventas al momento de escribir esto). Un `alembic upgrade head`
# corrido sin argumentos, en una maquina con el .env normal del proyecto, no debe
# poder tocarlo silenciosamente.
PROTECTED_SCHEMA = "db_dev"
ALLOW_PROTECTED_SCHEMA_ENV_VAR = "REVENEW_ALLOW_DB_DEV"


def _url() -> str:
    return config.attributes.get("sqlalchemy_url") or settings.SQLALCHEMY_DATABASE_URI


def _schema() -> str:
    return config.attributes.get("target_schema") or settings.POSTGRES_SCHEMA


def _guard_against_protected_schema(schema: str) -> None:
    if schema == PROTECTED_SCHEMA and os.environ.get(ALLOW_PROTECTED_SCHEMA_ENV_VAR) != "1":
        raise RuntimeError(
            f"Alembic resolved the target schema to {schema!r}. That schema holds "
            "live production data and this project's plan forbids migrating it "
            "implicitly. Refusing to run migrations.\n"
            f"If you deliberately intend to migrate {schema!r}, set "
            f"{ALLOW_PROTECTED_SCHEMA_ENV_VAR}=1 in the environment and re-run."
        )


def run_migrations_offline() -> None:
    raise NotImplementedError(
        "Offline mode ('alembic upgrade head --sql') is not supported by this "
        "project's env.py: the target schema is resolved at connection time via "
        "SET search_path, so generating SQL without a live connection would emit "
        "DDL for the wrong schema (or no schema at all) while only the "
        "alembic_version stamp would be qualified correctly. Run migrations "
        "online (the default) instead."
    )


def run_migrations_online() -> None:
    schema = _schema()
    _guard_against_protected_schema(schema)
    engine = create_engine(_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()

            connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
            connection.commit()
            try:
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    version_table_schema=schema,
                    include_schemas=False,
                    compare_type=True,
                )
                with context.begin_transaction():
                    context.run_migrations()
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
                )
                connection.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
