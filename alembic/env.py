import os
import time
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
# Se combina con hashtext(schema) (la forma de dos enteros de
# pg_advisory_lock) para que migrar db_v2 no bloquee una migracion
# concurrente de public, de un schema de test, o de un futuro db_v3 en la
# misma base: cada schema tiene su propio lock.
MIGRATION_LOCK_KEY = 0x5245564E  # "REVN"

# Cuanto esperar, en total, a que otra sesion suelte el advisory lock antes
# de rendirse con un error explicito. Sin este limite, `alembic upgrade
# head` bloquea indefinidamente y no imprime nada: bajo `set -e` en un
# entrypoint de contenedor eso no es un fallo ruidoso, es un boot que
# cuelga hasta que el healthcheck de la plataforma lo mata, y vuelve a
# colgar en cada reintento sin ninguna linea de log que explique por que.
MIGRATION_LOCK_TIMEOUT_SECONDS = float(
    os.environ.get("REVENEW_MIGRATION_LOCK_TIMEOUT_SECONDS", "30")
)
MIGRATION_LOCK_POLL_INTERVAL_SECONDS = 0.2

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


def _acquire_migration_lock(connection, schema: str) -> None:
    """Adquiere el advisory lock para `schema`, con espera acotada.

    Usa la forma de dos enteros de pg_try_advisory_lock: la clave fija
    MIGRATION_LOCK_KEY combinada con hashtext(schema), asi que el lock esta
    "particionado" por schema y una migracion de db_v2 no bloquea una de
    public, de un schema de test, o de un futuro db_v3 contra la misma
    base.

    pg_advisory_lock (la version bloqueante) fue el bug que el reviewer
    encontro: con el lock tomado por otra sesion, colgaba para siempre y no
    imprimia nada. En su lugar, se sondea pg_try_advisory_lock en un loop
    acotado por MIGRATION_LOCK_TIMEOUT_SECONDS y, si se agota el tiempo, se
    levanta un error explicito en vez de seguir esperando en silencio.
    """
    deadline = time.monotonic() + MIGRATION_LOCK_TIMEOUT_SECONDS
    while True:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:key, hashtext(:schema))"),
            {"key": MIGRATION_LOCK_KEY, "schema": schema},
        ).scalar()
        if acquired:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"No se pudo obtener el advisory lock de migraciones para el "
                f"schema {schema!r} despues de esperar "
                f"{MIGRATION_LOCK_TIMEOUT_SECONDS:.0f}s. Otra migracion "
                f"contra {schema!r} sigue en curso (o quedo colgada) y "
                "sostiene el lock. Esperá a que termine y reintentá; si no "
                "hay ninguna migracion legitima corriendo, encontrá y "
                "cerrá la sesion que quedo con el lock tomado "
                "(pg_locks / pg_stat_activity) antes de reintentar."
            )
        time.sleep(MIGRATION_LOCK_POLL_INTERVAL_SECONDS)


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

            _acquire_migration_lock(connection, schema)
            connection.commit()
            try:
                # version_table_schema=None es DELIBERADO, no un olvido.  El
                # SET search_path de arriba ya apunta a `schema`, asi que
                # alembic_version se crea dentro del schema destino igual que
                # cualquier otra tabla (lo garantiza
                # test_version_table_lives_inside_the_target_schema): la misma
                # filosofia que el resto del plan, el schema se resuelve por
                # search_path y nunca se cualifica explicitamente.
                #
                # Poner aqui el nombre real ROMPE el autogenerate.  Alembic
                # excluye su propia tabla de version comparando (schema, nombre)
                # de forma literal (autogenerate/compare.py, _autogen_for_tables),
                # y con include_schemas=False refleja el schema por defecto de la
                # conexion, que identifica como None.  "db_v2" != None, asi que
                # no reconoce alembic_version y cada `alembic revision
                # --autogenerate` emite un op.drop_table('alembic_version') que,
                # de colarse en una revision, borraria el stamp de versiones al
                # migrar.
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    version_table_schema=None,
                    include_schemas=False,
                    compare_type=True,
                )
                with context.begin_transaction():
                    context.run_migrations()
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key, hashtext(:schema))"),
                    {"key": MIGRATION_LOCK_KEY, "schema": schema},
                )
                connection.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
