import os
import uuid

import pytest
from sqlalchemy import create_engine, text

# Fixtures de dominio (sesion + datos sembrados) usadas por esta task y por
# las Tasks 4, 5, 7 y 8: `db_session`, `seeded_user`, `seeded_customer`,
# `seeded_product_with_lot`, `seeded_product_with_one_lot`,
# `seeded_purchase_draft`.
#
# Harness de tests HTTP autenticados (Task 2): `client`, `_auth`,
# `_create_thread_as`. La Task 6 tambien lo consume.
pytest_plugins = ["tests.fixtures_domain", "tests.fixtures_http"]

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://revenew@localhost:55432/revenew_test",
)

# Los tests crean y destruyen schemas.  Apuntarlos a Railway destruiria datos reales.
FORBIDDEN_HOST_MARKERS = ("rlwy.net", "railway", "proxy.rlwy")


# La huella de aprobacion se firma con un secreto del entorno y `firmar()`
# levanta si falta -- a proposito: un control que se apaga solo cuando falta su
# configuracion no es un control.  Los tests traen el suyo.
os.environ.setdefault("AGENT_HUELLA_SECRET", "secreto-de-prueba-no-usar-en-produccion")


def pytest_configure(config):
    if not TEST_DATABASE_URL.strip():
        raise pytest.UsageError(
            "TEST_DATABASE_URL esta vacia. Definila (o desdefinila para usar el "
            "default local) antes de correr los tests: crear/destruir schemas "
            "contra una URL vacia terminaria cayendo al fallback de settings, "
            "que puede apuntar a Railway."
        )
    for marker in FORBIDDEN_HOST_MARKERS:
        if marker in TEST_DATABASE_URL:
            raise pytest.UsageError(
                f"TEST_DATABASE_URL apunta a {marker!r}. Los tests crean y borran "
                "schemas: usa la base desechable de docker-compose.test.yml."
            )


@pytest.fixture(scope="session")
def test_engine():
    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def throwaway_schema(test_engine):
    """Un schema vacio, recien creado, que se destruye al terminar el test."""
    name = f"test_{uuid.uuid4().hex[:12]}"
    with test_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{name}"'))
    try:
        yield name
    finally:
        with test_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


from alembic.config import Config as AlembicConfig


@pytest.fixture
def alembic_config(throwaway_schema):
    """Config de Alembic apuntada al schema desechable del test."""
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    cfg.attributes["sqlalchemy_url"] = TEST_DATABASE_URL
    cfg.attributes["target_schema"] = throwaway_schema
    return cfg


from alembic import command


@pytest.fixture
def migrated_schema(alembic_config):
    """Schema desechable ya migrado a head."""
    command.upgrade(alembic_config, "head")
    return alembic_config.attributes["target_schema"]


@pytest.fixture
def second_migrated_schema(test_engine):
    """Un segundo schema desechable, ya migrado, para probar copias."""
    from alembic.config import Config as _Config

    name = f"test_{uuid.uuid4().hex[:12]}"
    cfg = _Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    cfg.attributes["sqlalchemy_url"] = TEST_DATABASE_URL
    cfg.attributes["target_schema"] = name
    command.upgrade(cfg, "head")
    try:
        yield name
    finally:
        with test_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
