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


def _url() -> str:
    return config.attributes.get("sqlalchemy_url") or settings.SQLALCHEMY_DATABASE_URI


def _schema() -> str:
    return config.attributes.get("target_schema") or settings.POSTGRES_SCHEMA


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=_schema(),
        include_schemas=False,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    schema = _schema()
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
