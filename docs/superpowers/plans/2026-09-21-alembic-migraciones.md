# Adopción de Alembic — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reemplazar `create_all()` y el runner de migraciones hecho a mano por Alembic, migrar los datos a un schema `db_v2` limpio, y añadir los campos y el libro de caja que faltan.

**Architecture:** Las migraciones son agnósticas del schema: el metadata no lleva schema y `env.py` fija el `search_path`, de modo que la misma revisión corre contra `db_dev`, `db_v2`, `public` o un schema desechable de test. Tres revisiones: `001` reproduce el esquema actual exacto para que la copia de datos sea columna por columna, `002` añade campos y `cash_movements`, `003` unifica tipos numéricos.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0.41, Alembic, PostgreSQL 16, pytest, Docker (solo para la base de tests).

**Spec:** `docs/superpowers/specs/2026-09-21-alembic-migraciones-design.md`

## Global Constraints

- **Nunca escribir en `db_dev`.** Es la única copia de los datos reales (156 ventas, Q9,954.49). Todo el plan lee de ahí y escribe en otro lado.
- **Los tests nunca tocan Railway.** Corren contra la Postgres desechable de `docker-compose.test.yml`. `tests/conftest.py` lleva un guard que aborta si `TEST_DATABASE_URL` apunta a Railway.
- **Ninguna operación de migración emite `schema=`.** El schema se resuelve por `search_path`. Una revisión con `schema='db_dev'` incrustada es un bug.
- **`alembic_version` vive dentro de cada schema** (`version_table_schema`), no en `public`.
- **Migraciones autogeneradas se revisan a mano.** El autogenerate no es fiable con `earning_mode_enum` (enum nativo) ni con `server_default=func.gen_random_uuid()`.
- **Nombres de constraints** según la convención definida en la Task 3. Ninguna constraint nueva sin nombre.
- **Decimales:** cantidades `Numeric(10,4)`, dinero `Numeric(14,2)`, porcentajes `Numeric(5,2)`.
- Un commit por task, al final de la task.

**Prerrequisito:** Docker Desktop corriendo. Comprobar con `docker info`.

---

### Task 1: Infraestructura de tests

No existe suite de tests en el repo. Esta task la crea, con una Postgres desechable en Docker para no tocar nunca la base real.

**Files:**
- Create: `docker-compose.test.yml`
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py` (vacío)
- Create: `tests/conftest.py`
- Create: `tests/test_infrastructure.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: fixture `test_engine` (session scope, `sqlalchemy.Engine`), fixture `throwaway_schema` (function scope, `str` con el nombre de un schema vacío que se destruye al terminar), constante `TEST_DATABASE_URL: str`.

- [ ] **Step 1: Crear la base de datos desechable**

`docker-compose.test.yml`:

```yaml
services:
  test-db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: revenew
      POSTGRES_PASSWORD: revenew
      POSTGRES_DB: revenew_test
    ports:
      - "55432:5432"
    tmpfs:
      - /var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U revenew -d revenew_test"]
      interval: 2s
      timeout: 3s
      retries: 15
```

El puerto es 55432 para no chocar con una Postgres local. `tmpfs` hace que los datos vivan en RAM: arranca rápido y no deja nada en disco.

- [ ] **Step 2: Levantarla y verificar**

Run: `docker compose -f docker-compose.test.yml up -d && docker compose -f docker-compose.test.yml ps`
Expected: el servicio `test-db` en estado `healthy`.

- [ ] **Step 3: Declarar las dependencias de desarrollo**

`requirements-dev.txt`:

```
-r requirements.txt
pytest==8.3.4
```

Run: `venv/bin/pip install -r requirements-dev.txt`

- [ ] **Step 4: Escribir conftest.py con el guard de seguridad**

`tests/conftest.py`:

```python
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://revenew:revenew@localhost:55432/revenew_test",
)

# Los tests crean y destruyen schemas.  Apuntarlos a Railway destruiria datos reales.
FORBIDDEN_HOST_MARKERS = ("rlwy.net", "railway", "proxy.rlwy")


def pytest_configure(config):
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
```

- [ ] **Step 5: Escribir el test que falla**

`tests/test_infrastructure.py`:

```python
from sqlalchemy import text


def test_throwaway_schema_exists_and_is_empty(test_engine, throwaway_schema):
    with test_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :s"),
            {"s": throwaway_schema},
        ).scalar()
        tables = conn.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = :s"),
            {"s": throwaway_schema},
        ).scalar()
    assert exists == 1
    assert tables == 0


def test_throwaway_schema_is_dropped_afterwards(test_engine, throwaway_schema):
    # Guardamos el nombre; la verificacion real la hace el test siguiente.
    assert throwaway_schema.startswith("test_")
```

- [ ] **Step 6: Correr el test**

Run: `venv/bin/pytest tests/test_infrastructure.py -v`
Expected: PASS (2 tests). Si falla con `UsageError`, revisa `TEST_DATABASE_URL`.

- [ ] **Step 7: Ignorar artefactos de test**

Añadir a `.gitignore`:

```
# Test artifacts
.pytest_cache/
```

(El patrón actual es `*.pytest_cache/`; añadir la forma sin asterisco es inofensivo y más explícito.)

- [ ] **Step 8: Commit**

```bash
git add docker-compose.test.yml requirements-dev.txt tests/ .gitignore
git commit -m "test: Add pytest setup with a disposable Postgres for schema tests"
```

---

### Task 2: Scaffolding de Alembic con env.py agnóstico del schema

**Files:**
- Modify: `requirements.txt`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/.gitkeep`
- Create: `tests/test_alembic_env.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `throwaway_schema`, `test_engine`, `TEST_DATABASE_URL` (Task 1).
- Produces: fixture `alembic_config(schema: str) -> alembic.config.Config` — devuelve una config apuntada al schema dado y a `TEST_DATABASE_URL`. `env.py` lee `config.attributes["target_schema"]` y `config.attributes["sqlalchemy_url"]`, con fallback a `app.core.config.settings`.

- [ ] **Step 1: Instalar Alembic**

Añadir al final de `requirements.txt`:

```
alembic==1.14.0
```

Run: `venv/bin/pip install -r requirements.txt`

- [ ] **Step 2: Generar el scaffolding**

Run: `venv/bin/alembic init alembic`
Expected: crea `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/`.

- [ ] **Step 3: Reemplazar alembic.ini**

Sustituir el generado por este. **Sin `sqlalchemy.url`**: las credenciales viven solo en `settings`.

```ini
[alembic]
script_location = alembic
prepend_sys_path = .
file_template = %%(rev)s_%%(slug)s
# sqlalchemy.url se omite a proposito — env.py la lee de app.core.config.settings

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 4: Escribir env.py**

Reemplazar `alembic/env.py` completo:

```python
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
```

- [ ] **Step 5: Añadir la fixture de config a conftest.py**

Añadir al final de `tests/conftest.py`:

```python
from alembic.config import Config as AlembicConfig


@pytest.fixture
def alembic_config(throwaway_schema):
    """Config de Alembic apuntada al schema desechable del test."""
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    cfg.attributes["sqlalchemy_url"] = TEST_DATABASE_URL
    cfg.attributes["target_schema"] = throwaway_schema
    return cfg
```

- [ ] **Step 6: Escribir el test que falla**

`tests/test_alembic_env.py`:

```python
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
```

- [ ] **Step 7: Correr el test**

Run: `venv/bin/pytest tests/test_alembic_env.py -v`
Expected: por ahora puede fallar `test_version_table_lives_inside_the_target_schema` porque sin revisiones Alembic no crea `alembic_version`. Si es así, marca ese test con `@pytest.mark.xfail(reason="sin revisiones todavia; se resuelve en Task 4", strict=False)` y quítale el marcador en la Task 4.

- [ ] **Step 8: Commit**

```bash
git add requirements.txt alembic.ini alembic/ tests/
git commit -m "feat: Add Alembic with a schema-agnostic env.py"
```

---

### Task 3: Convención de nombres y metadata sin schema

Este es el cambio que hace portables a las migraciones. Hoy el schema está declarado en tres sitios: el metadata, el enum de `product.py`, y el `search_path` del engine. Se queda solo el último.

**Files:**
- Modify: `app/core/database.py:23-25` (definición de `Base`)
- Modify: `app/models/product.py:6,26-35` (el `SQLEnum`)
- Create: `tests/test_metadata.py`

**Interfaces:**
- Produces: `app.core.database.NAMING_CONVENTION: dict[str, str]`. `Base.metadata.schema` pasa a ser `None`.

- [ ] **Step 1: Escribir el test que falla**

`tests/test_metadata.py`:

```python
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.core.database import Base
import app.models  # noqa: F401


def test_metadata_carries_no_hardcoded_schema():
    assert Base.metadata.schema is None
    assert all(t.schema is None for t in Base.metadata.sorted_tables)


def test_enum_type_is_not_bound_to_a_schema():
    col = Base.metadata.tables["products"].c.earning_mode
    assert col.type.schema is None


def test_foreign_keys_follow_the_naming_convention():
    ddl = str(
        CreateTable(Base.metadata.tables["sales"]).compile(dialect=postgresql.dialect())
    )
    assert "fk_sales_customer_id_customers" in ddl
    assert "pk_sales" in ddl


def test_every_table_is_reachable_without_a_schema_prefix():
    expected = {
        "users", "customers", "products", "purchases", "purchase_items",
        "sales", "sale_items", "sale_item_lot_allocations", "customer_product_cycles",
    }
    assert expected <= set(Base.metadata.tables)
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `venv/bin/pytest tests/test_metadata.py -v`
Expected: FAIL — `Base.metadata.schema` es `'db_dev'`, no `None`.

- [ ] **Step 3: Cambiar la definición de Base**

En `app/core/database.py`, reemplazar el bloque de `Base`:

```python
# Convencion de nombres para constraints e indices.  Sin ella, Postgres asigna
# nombres por defecto que el autogenerate de Alembic no puede alterar ni borrar
# de forma fiable mas adelante.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# Base declarativa.  El metadata NO lleva schema: las tablas se resuelven por el
# search_path que fija set_search_path() en cada conexion, de modo que el mismo
# modelo y las mismas migraciones sirven para db_dev, db_v2, public o un schema
# de test.
class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

`SCHEMA` y el listener `set_search_path` se quedan como están — son el mecanismo que hace funcionar todo esto.

- [ ] **Step 4: Quitar el schema del enum**

En `app/models/product.py`, cambiar el import de la línea 9:

```python
from app.core.database import Base
```

y el `mapped_column` de `earning_mode`:

```python
    earning_mode: Mapped[EarningMode] = mapped_column(
        SQLEnum(
            EarningMode,
            name="earning_mode_enum",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=EarningMode.PERCENT,
        server_default=text("'percent'"),
    )
```

- [ ] **Step 5: Verificar que no queda ninguna referencia a SCHEMA en los modelos**

Run: `grep -rn "SCHEMA" app/models/`
Expected: sin resultados.

- [ ] **Step 6: Correr los tests**

Run: `venv/bin/pytest tests/test_metadata.py -v`
Expected: PASS (4 tests).

- [ ] **Step 7: Commit**

```bash
git add app/core/database.py app/models/product.py tests/test_metadata.py
git commit -m "refactor: Drop the hardcoded schema from metadata and add a naming convention"
```

---

### Task 4: Migración `001` — el esquema actual, exacto

Reproduce lo que hoy existe en `db_dev`, **incluyendo** los tres parches que aplica `ensure_schema_compatibility()`. El objetivo es que la copia de datos de la Task 6 sea columna por columna.

**Files:**
- Create: `alembic/versions/<rev>_initial_schema.py` (generada)
- Create: `tests/test_migrations.py`
- Modify: `tests/conftest.py` (fixture `migrated_schema`)
- Modify: `tests/test_alembic_env.py` (quitar el `xfail` de la Task 2)

**Interfaces:**
- Consumes: `alembic_config`, `throwaway_schema`, `test_engine`.
- Produces: fixture `migrated_schema(alembic_config) -> str` — nombre de un schema desechable ya migrado a `head`.

- [ ] **Step 1: Añadir la fixture `migrated_schema` a conftest.py**

```python
from alembic import command


@pytest.fixture
def migrated_schema(alembic_config):
    """Schema desechable ya migrado a head."""
    command.upgrade(alembic_config, "head")
    return alembic_config.attributes["target_schema"]
```

- [ ] **Step 2: Escribir los tests que fallan**

`tests/test_migrations.py`:

```python
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
                "version_table_schema": migrated_schema,
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


def test_migration_emits_no_hardcoded_schema():
    """Una revision con schema='db_dev' incrustado no sirve para db_v2."""
    from pathlib import Path

    for path in Path("alembic/versions").glob("*.py"):
        source = path.read_text()
        assert "schema='db_dev'" not in source, f"{path} lleva el schema incrustado"
        assert 'schema="db_dev"' not in source, f"{path} lleva el schema incrustado"
```

- [ ] **Step 3: Correr los tests para verificar que fallan**

Run: `venv/bin/pytest tests/test_migrations.py -v`
Expected: FAIL — no hay ninguna revisión, así que no se crea ninguna tabla.

- [ ] **Step 4: Generar la revisión inicial**

Contra un schema vacío y desechable, nunca contra `db_dev`:

```bash
docker compose -f docker-compose.test.yml up -d
TEST_DATABASE_URL=postgresql://revenew:revenew@localhost:55432/revenew_test \
POSTGRES_SCHEMA=gen_tmp \
DATABASE_URL=postgresql://revenew:revenew@localhost:55432/revenew_test \
  venv/bin/alembic revision --autogenerate -m "initial schema"
```

- [ ] **Step 5: Revisar la revisión generada a mano**

Esta revisión **no se acepta tal cual**. Comprobar una por una:

1. **Ninguna operación lleva `schema=`.** Si aparece, el metadata todavía tiene schema — vuelve a la Task 3.
2. **`earning_mode_enum`** se crea con los labels en minúscula (`'percent'`, `'fee'`) y **sin** `schema=`. El autogenerate suele omitir el `sa.Enum` o duplicar el `CREATE TYPE`; verifica que `op.create_table("products", ...)` lo referencia correctamente y que el `downgrade()` incluye `sa.Enum(name="earning_mode_enum").drop(op.get_bind(), checkfirst=False)`.
3. **`server_default=sa.text("gen_random_uuid()")`** presente en cada PK UUID. El autogenerate a veces lo pierde.
4. **Los tres parches de `ensure_schema_compatibility()` están incluidos:**
   - `purchase_items.remaining_quantity` — `Numeric(10,4)`, `NOT NULL`, default `0`
   - `sale_items.discount_percent` — `Numeric(5,2)`, nullable
   - `sale_items.discount_amount` — `Numeric(14,2)`, nullable
   - `sales.is_payment_pending` — `Boolean`, `NOT NULL`, server default `false`
5. **Los tipos decimales** ya promovidos: `sale_items.quantity`, `sale_item_lot_allocations.quantity_allocated`, `purchase_items.remaining_quantity`, `products.stock`, `customer_product_cycles.last_quantity` todos `Numeric(10,4)`.
6. **`downgrade()` borra las 9 tablas** en orden inverso de FK.
7. **Nombres de constraints** según la convención: `pk_sales`, `fk_sales_customer_id_customers`, `uq_customer_product` (este último tiene nombre explícito en el modelo y se conserva).

- [ ] **Step 6: Correr los tests**

Run: `venv/bin/pytest tests/test_migrations.py tests/test_alembic_env.py -v`
Expected: PASS. Si `test_models_and_migrations_do_not_drift` falla, el diff que imprime dice exactamente qué corregir en la revisión.

- [ ] **Step 7: Quitar el xfail de la Task 2**

Eliminar el marcador `@pytest.mark.xfail` de `test_version_table_lives_inside_the_target_schema` si se añadió.

Run: `venv/bin/pytest tests/ -v`
Expected: PASS, todo.

- [ ] **Step 8: Commit**

```bash
git add alembic/versions/ tests/
git commit -m "feat: Add the initial Alembic revision reproducing the current schema"
```

---

### Task 5: Retirar `create_all()` y las funciones de bootstrap

Con Alembic dueño del esquema, `create_all` crea tablas que Alembic cree que nunca existieron. Los dos tienen que convivir nunca.

**Files:**
- Modify: `app/main.py:18-40` (el `lifespan`)
- Modify: `app/core/database.py:30-101` (borrar las dos funciones)
- Create: `tests/test_startup.py`

**Interfaces:**
- Produces: `app.core.database` deja de exportar `prepare_schema_bootstrap` y `ensure_schema_compatibility`.

- [ ] **Step 1: Escribir el test que falla**

`tests/test_startup.py`:

```python
from pathlib import Path

import app.core.database as database
import app.main as main


def test_schema_bootstrap_helpers_are_gone():
    """Alembic es el unico dueno del esquema."""
    assert not hasattr(database, "prepare_schema_bootstrap")
    assert not hasattr(database, "ensure_schema_compatibility")


def test_startup_does_not_create_tables():
    source = Path(main.__file__).read_text()
    assert "create_all" not in source, "El arranque no debe crear tablas; eso es de Alembic"
    assert "prepare_schema_bootstrap" not in source
    assert "ensure_schema_compatibility" not in source


def test_health_endpoint_still_responds():
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "degraded"}
```

`TestClient` necesita `httpx`, que ya está en `requirements.txt`.

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `venv/bin/pytest tests/test_startup.py -v`
Expected: FAIL en los dos primeros tests.

- [ ] **Step 3: Simplificar el lifespan**

En `app/main.py`, reemplazar el `lifespan` y sus imports:

```python
from app.core.database import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    # El esquema lo gestiona Alembic desde entrypoint.sh, no el arranque.
    startup_issues.clear()

    try:
        initialize_firebase()
    except Exception:
        logger.exception("Firebase initialization failed during startup")
        startup_issues.append("firebase_init_failed")

    yield
    engine.dispose()
```

Ajustar el import de la línea 7 para que deje de traer `Base`, `prepare_schema_bootstrap` y `ensure_schema_compatibility`.

- [ ] **Step 4: Borrar las funciones**

En `app/core/database.py`, eliminar `prepare_schema_bootstrap()` y `ensure_schema_compatibility()` completas (~72 líneas), y el import de `text` si queda sin uso.

Run: `grep -n "text" app/core/database.py`
Expected: sin resultados salvo dentro del listener `set_search_path`, que usa el cursor DBAPI y no `text`.

- [ ] **Step 5: Correr los tests**

Run: `venv/bin/pytest tests/ -v`
Expected: PASS.

- [ ] **Step 6: Verificar que la app arranca de verdad**

```bash
venv/bin/uvicorn app.main:app --port 8001 &
sleep 3 && curl -s http://127.0.0.1:8001/health && kill %1
```

Expected: `{"status":"ok"}`. Nota: apunta a `db_dev`, que ya tiene las tablas.

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/core/database.py tests/test_startup.py
git commit -m "refactor: Hand schema ownership to Alembic and drop the bootstrap helpers"
```

---

### Task 6: Script de copia `db_dev` → `db_v2`

**Files:**
- Create: `scripts/__init__.py` (vacío)
- Create: `scripts/copy_schema_data.py`
- Create: `tests/test_copy_schema_data.py`

**Interfaces:**
- Produces: `scripts.copy_schema_data.copy_schema_data(engine, source: str, target: str, dry_run: bool = False) -> dict[str, int]` — devuelve `{tabla: filas_copiadas}`. Lanza `SchemaMismatch` si el origen tiene columnas que el destino no tiene.
- Produces: `scripts.copy_schema_data.TABLE_ORDER: list[str]`, `scripts.copy_schema_data.SchemaMismatch(Exception)`.

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_copy_schema_data.py`:

```python
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.models import Customer, Product, Sale, SaleItem, User
from scripts.copy_schema_data import SchemaMismatch, TABLE_ORDER, copy_schema_data
from tests.conftest import TEST_DATABASE_URL


def _session_for(schema: str):
    engine = create_engine(TEST_DATABASE_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()
        dbapi_connection.commit()

    return sessionmaker(bind=engine)()


def _seed(schema: str) -> None:
    session = _session_for(schema)
    user = User(email=f"{uuid.uuid4().hex}@example.com", role="admin")
    customer = Customer(name="Aurita")
    product = Product(sku=f"SKU-{uuid.uuid4().hex[:6]}", name="Cartón de huevos")
    session.add_all([user, customer, product])
    session.flush()

    sale = Sale(
        customer_id=customer.id,
        user_id=user.id,
        date=date(2026, 8, 5),
        total=Decimal("18.00"),
    )
    session.add(sale)
    session.flush()

    session.add(
        SaleItem(
            sale_id=sale.id,
            product_id=product.id,
            quantity=Decimal("0.5"),
            unit_price=Decimal("36.00"),
            subtotal=Decimal("18.00"),
        )
    )
    session.commit()
    session.close()


def test_table_order_respects_foreign_keys():
    assert TABLE_ORDER.index("users") < TABLE_ORDER.index("sales")
    assert TABLE_ORDER.index("customers") < TABLE_ORDER.index("sales")
    assert TABLE_ORDER.index("sales") < TABLE_ORDER.index("sale_items")
    assert TABLE_ORDER.index("purchases") < TABLE_ORDER.index("purchase_items")
    assert TABLE_ORDER.index("purchase_items") < TABLE_ORDER.index(
        "sale_item_lot_allocations"
    )


def test_copy_preserves_row_counts_and_sums(
    test_engine, alembic_config, second_migrated_schema
):
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]
    target = second_migrated_schema
    _seed(source)

    copied = copy_schema_data(test_engine, source, target)

    assert copied["sales"] == 1
    assert copied["sale_items"] == 1
    with test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{target}"'))
        assert conn.execute(text("SELECT count(*) FROM sales")).scalar() == 1
        assert conn.execute(text("SELECT sum(total) FROM sales")).scalar() == Decimal(
            "18.00"
        )
        assert conn.execute(
            text("SELECT sum(quantity) FROM sale_items")
        ).scalar() == Decimal("0.5000")


def test_copy_aborts_when_target_lacks_a_column(
    test_engine, alembic_config, second_migrated_schema
):
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]
    target = second_migrated_schema

    with test_engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{source}".customers ADD COLUMN nickname TEXT'))

    with pytest.raises(SchemaMismatch, match="nickname"):
        copy_schema_data(test_engine, source, target)


def test_dry_run_copies_nothing(test_engine, alembic_config, second_migrated_schema):
    from alembic import command

    command.upgrade(alembic_config, "head")
    source = alembic_config.attributes["target_schema"]
    target = second_migrated_schema
    _seed(source)

    copy_schema_data(test_engine, source, target, dry_run=True)

    with test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{target}"'))
        assert conn.execute(text("SELECT count(*) FROM sales")).scalar() == 0
```

- [ ] **Step 2: Añadir la fixture `second_migrated_schema` a conftest.py**

```python
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
```

- [ ] **Step 3: Correr los tests para verificar que fallan**

Run: `venv/bin/pytest tests/test_copy_schema_data.py -v`
Expected: FAIL con `ModuleNotFoundError: scripts.copy_schema_data`.

- [ ] **Step 4: Escribir el script**

`scripts/copy_schema_data.py`:

```python
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

        col_list = ", ".join(f'"{c}"' for c in src_cols)
        result = conn.execute(
            text(
                f'INSERT INTO "{target}"."{table}" ({col_list}) '
                f'SELECT {col_list} FROM "{source}"."{table}"'
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
```

- [ ] **Step 5: Correr los tests**

Run: `venv/bin/pytest tests/test_copy_schema_data.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add scripts/ tests/test_copy_schema_data.py tests/conftest.py
git commit -m "feat: Add a transactional schema-to-schema data copy script"
```

---

### Task 7: Migración `002` — campos nuevos y libro de caja

Los campos salen del diff contra la hoja de Revenew. `cash_movements` nace **vacía**: importar el Sheet está fuera de alcance.

**Files:**
- Modify: `app/models/purchase.py` (añadir `payment_method` a `Purchase`)
- Modify: `app/models/sale.py` (añadir `payment_date`, `payment_method`)
- Modify: `app/models/sale_item.py` (añadir `expected_unit_margin`)
- Modify: `app/models/customer_product_cycle.py` (3 campos de proyección)
- Create: `app/models/cash_movement.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/<rev>_add_payment_fields_and_cash_movements.py` (generada)
- Create: `tests/test_cash_movements.py`
- Modify: `tests/test_migrations.py` (ampliar `EXPECTED_TABLES`)

**Interfaces:**
- Produces: `app.models.CashMovement`, `app.models.CashMovementType` (enum con `ENTRADA`, `SALIDA`, `APORTE_SOCIO`, `RETIRO_SOCIO`; valores `"entrada"`, `"salida"`, `"aporte_socio"`, `"retiro_socio"`).

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_cash_movements.py`:

```python
from datetime import date
from decimal import Decimal

from sqlalchemy import text

from app.core.database import Base
from app.models import CashMovement, CashMovementType
import app.models  # noqa: F401


def test_cash_movement_type_values():
    assert {m.value for m in CashMovementType} == {
        "entrada",
        "salida",
        "aporte_socio",
        "retiro_socio",
    }


def test_new_payment_fields_exist_on_the_models():
    assert "payment_date" in Base.metadata.tables["sales"].c
    assert "payment_method" in Base.metadata.tables["sales"].c
    assert "payment_method" in Base.metadata.tables["purchases"].c
    assert "expected_unit_margin" in Base.metadata.tables["sale_items"].c
    cycles = Base.metadata.tables["customer_product_cycles"].c
    assert "projection_method" in cycles
    assert "projection_confidence" in cycles
    assert "calendar_event_id" in cycles


def test_payment_date_is_nullable():
    """VTA-046 del Sheet esta pagada sin fecha de pago: no puede ser obligatoria."""
    assert Base.metadata.tables["sales"].c.payment_date.nullable is True


def test_cash_movements_table_has_both_nullable_links(test_engine, migrated_schema):
    with test_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'cash_movements'"
            ),
            {"s": migrated_schema},
        )
        nullable = {r[0]: r[1] for r in rows}
    assert nullable["sale_id"] == "YES"
    assert nullable["purchase_id"] == "YES"
    assert nullable["amount"] == "NO"


def test_running_balance_is_not_stored(test_engine, migrated_schema):
    """El saldo acumulado se calcula al leer; en el Sheet la columna se desincronizo."""
    with test_engine.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'cash_movements'"
                ),
                {"s": migrated_schema},
            )
        }
    assert "running_balance" not in cols
    assert "saldo_acumulado" not in cols


def test_running_balance_can_be_computed(test_engine, migrated_schema):
    """SUM() OVER reproduce el saldo sin guardarlo."""
    with test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        for day, kind, amount in [
            (date(2026, 9, 3), "entrada", "115.00"),
            (date(2026, 9, 3), "entrada", "37.00"),
            (date(2026, 9, 5), "salida", "285.00"),
        ]:
            conn.execute(
                text(
                    "INSERT INTO cash_movements (movement_date, type, amount) "
                    "VALUES (:d, CAST(:t AS cash_movement_type_enum), :a)"
                ),
                {"d": day, "t": kind, "a": Decimal(amount)},
            )

    with test_engine.connect() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        balance = conn.execute(
            text(
                "SELECT SUM(CASE WHEN type = 'salida' THEN -amount ELSE amount END) "
                "OVER (ORDER BY movement_date, amount) "
                "FROM cash_movements ORDER BY movement_date, amount"
            )
        ).scalars().all()
    assert balance[-1] == Decimal("-133.00")
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `venv/bin/pytest tests/test_cash_movements.py -v`
Expected: FAIL con `ImportError: cannot import name 'CashMovement'`.

- [ ] **Step 3: Añadir `payment_method` a `Purchase`**

En `app/models/purchase.py`, después de `status` (línea 24):

```python
    payment_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
```

- [ ] **Step 4: Añadir los campos de pago a `Sale`**

En `app/models/sale.py`, después de `is_payment_pending` (línea 22):

```python
    # Nullable a proposito: en los datos reales hay ventas pagadas sin fecha
    # registrada, y alguna con fecha anterior a la venta.  Sin CHECK.
    payment_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    payment_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
```

Añadir `Optional` a los imports de `typing` si falta.

- [ ] **Step 5: Añadir `expected_unit_margin` a `SaleItem`**

En `app/models/sale_item.py`, después de `gross_profit_total` (línea 28):

```python
    # El margen estandar esperado POR UNIDAD, congelado al momento de la venta
    # igual que cost_basis_unit.  Sin el, cambiar el earning_percent de un producto
    # impide recalcular margen_extra = gross_profit_unit - expected_unit_margin
    # para ventas historicas.
    expected_unit_margin: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2), nullable=True
    )
```

- [ ] **Step 6: Añadir los campos de proyección a `CustomerProductCycle`**

En `app/models/customer_product_cycle.py`, después de `total_purchases` (línea 34):

```python
    projection_method: Mapped[Optional[str]] = mapped_column(String(20))  # "ewma" | "croston"
    projection_confidence: Mapped[Optional[str]] = mapped_column(String(20))
    calendar_event_id: Mapped[Optional[str]] = mapped_column(String(255))
```

Añadir `String` a los imports de `sqlalchemy`.

- [ ] **Step 7: Crear el modelo `CashMovement`**

`app/models/cash_movement.py`:

```python
import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    Date,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CashMovementType(str, PyEnum):
    ENTRADA = "entrada"
    SALIDA = "salida"
    APORTE_SOCIO = "aporte_socio"
    RETIRO_SOCIO = "retiro_socio"


class CashMovement(Base):
    """Libro de caja.

    El saldo acumulado NO se guarda: se calcula al leer con
    SUM(...) OVER (ORDER BY movement_date).  Guardarlo como columna fue la
    fuente de desincronizacion en la hoja de calculo de la que viene este modelo.

    Limitacion conocida: un movimiento apunta a una sola venta.  Un cobro que
    salda varias se registra como varios movimientos.
    """

    __tablename__ = "cash_movements"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    movement_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    type: Mapped[CashMovementType] = mapped_column(
        SQLEnum(
            CashMovementType,
            name="cash_movement_type_enum",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payment_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    sale_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("sales.id", ondelete="SET NULL"), nullable=True, index=True
    )
    purchase_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("purchases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<CashMovement(id={self.id}, type={self.type}, amount={self.amount})>"
```

- [ ] **Step 8: Exportar el modelo nuevo**

En `app/models/__init__.py`, añadir el import y ampliar `__all__`:

```python
from app.models.cash_movement import CashMovement, CashMovementType
```

```python
__all__ = [
    "User", "Customer", "Product", "Sale", "SaleItem", "SaleItemLotAllocation",
    "CustomerProductCycle", "Purchase", "PurchaseItem",
    "CashMovement", "CashMovementType",
]
```

- [ ] **Step 9: Ampliar `EXPECTED_TABLES`**

En `tests/test_migrations.py`, añadir `"cash_movements"` al set.

- [ ] **Step 10: Generar la revisión**

```bash
TEST_DATABASE_URL=postgresql://revenew:revenew@localhost:55432/revenew_test \
POSTGRES_SCHEMA=gen_tmp2 \
DATABASE_URL=postgresql://revenew:revenew@localhost:55432/revenew_test \
  venv/bin/alembic revision --autogenerate -m "add payment fields and cash movements"
```

- [ ] **Step 11: Revisar la revisión a mano**

1. Ninguna operación lleva `schema=`.
2. `cash_movement_type_enum` se crea en `upgrade()` y se borra en `downgrade()`:
   `sa.Enum(name="cash_movement_type_enum").drop(op.get_bind(), checkfirst=False)`.
3. Las 7 columnas nuevas son **todas nullable** — se añaden sobre tablas con datos.
4. `downgrade()` borra `cash_movements` y las 7 columnas.
5. Los índices de `cash_movements` siguen la convención: `ix_cash_movements_sale_id`, etc.

- [ ] **Step 12: Correr todos los tests**

Run: `venv/bin/pytest tests/ -v`
Expected: PASS. En particular `test_models_and_migrations_do_not_drift`.

- [ ] **Step 13: Commit**

```bash
git add app/models/ alembic/versions/ tests/
git commit -m "feat: Add payment fields, projection metadata and the cash movements book"
```

---

### Task 8: Migración `003` — consistencia de tipos numéricos

Dos columnas quedaron enteras mientras sus vecinas ya son decimales.

**Files:**
- Modify: `app/models/product.py` (`min_stock`)
- Modify: `app/models/purchase.py` (`PurchaseItem.quantity`)
- Create: `alembic/versions/<rev>_numeric_consistency.py` (generada)
- Create: `tests/test_numeric_types.py`

- [ ] **Step 1: Escribir el test que falla**

`tests/test_numeric_types.py`:

```python
from decimal import Decimal

from sqlalchemy import Numeric

from app.core.database import Base
import app.models  # noqa: F401

DECIMAL_QUANTITY_COLUMNS = [
    ("products", "stock"),
    ("products", "min_stock"),
    ("purchase_items", "quantity"),
    ("purchase_items", "remaining_quantity"),
    ("sale_items", "quantity"),
    ("sale_item_lot_allocations", "quantity_allocated"),
    ("customer_product_cycles", "last_quantity"),
]


def test_quantity_columns_are_all_numeric_10_4():
    """Vender medio carton debe funcionar en cualquier columna de cantidad."""
    for table, column in DECIMAL_QUANTITY_COLUMNS:
        col = Base.metadata.tables[table].c[column]
        assert isinstance(col.type, Numeric), f"{table}.{column} no es Numeric"
        assert (col.type.precision, col.type.scale) == (10, 4), (
            f"{table}.{column} es Numeric({col.type.precision},{col.type.scale})"
        )
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `venv/bin/pytest tests/test_numeric_types.py -v`
Expected: FAIL — `products.min_stock` y `purchase_items.quantity` son `Integer`.

- [ ] **Step 3: Cambiar los modelos**

En `app/models/product.py`:

```python
    min_stock: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=0
    )  # Reorder point
```

En `app/models/purchase.py` (clase `PurchaseItem`):

```python
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
```

Añadir `Decimal` a los imports donde falte y quitar `Integer` si queda sin uso.

- [ ] **Step 4: Generar la revisión**

```bash
TEST_DATABASE_URL=postgresql://revenew:revenew@localhost:55432/revenew_test \
POSTGRES_SCHEMA=gen_tmp3 \
DATABASE_URL=postgresql://revenew:revenew@localhost:55432/revenew_test \
  venv/bin/alembic revision --autogenerate -m "numeric consistency for quantity columns"
```

- [ ] **Step 5: Revisar que el ALTER conserva los datos**

El `upgrade()` debe llevar `postgresql_using` para no perder valores:

```python
op.alter_column(
    "products", "min_stock",
    type_=sa.Numeric(10, 4),
    existing_nullable=False,
    postgresql_using="min_stock::numeric(10,4)",
)
op.alter_column(
    "purchase_items", "quantity",
    type_=sa.Numeric(10, 4),
    existing_nullable=False,
    postgresql_using="quantity::numeric(10,4)",
)
```

El `downgrade()` vuelve a `sa.Integer` con `postgresql_using="min_stock::integer"` — **pierde decimales**, documentarlo en un comentario dentro de la revisión.

- [ ] **Step 6: Correr todos los tests**

Run: `venv/bin/pytest tests/ -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/models/ alembic/versions/ tests/test_numeric_types.py
git commit -m "feat: Promote min_stock and purchase quantity to Numeric(10,4)"
```

---

### Task 9: Migración automática en el deploy

**Files:**
- Modify: `entrypoint.sh`
- Modify: `Dockerfile` (verificar que `alembic/` llega a la imagen)
- Create: `tests/test_entrypoint.py`

- [ ] **Step 1: Escribir el test que falla**

`tests/test_entrypoint.py`:

```python
from pathlib import Path


def test_entrypoint_migrates_before_serving():
    script = Path("entrypoint.sh").read_text()
    assert "alembic upgrade head" in script
    upgrade_at = script.index("alembic upgrade head")
    serve_at = script.index("hypercorn")
    assert upgrade_at < serve_at, "Las migraciones van antes de levantar la app"
    assert "set -e" in script, "Un fallo de migracion debe abortar el arranque"


def test_alembic_is_a_declared_dependency():
    assert "alembic" in Path("requirements.txt").read_text().lower()


def test_dockerignore_does_not_exclude_migrations():
    ignored = Path(".dockerignore").read_text().splitlines()
    assert "alembic/" not in ignored
    assert "alembic" not in ignored
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `venv/bin/pytest tests/test_entrypoint.py -v`
Expected: FAIL en el primer test.

- [ ] **Step 3: Añadir la migración al entrypoint**

`entrypoint.sh`:

```sh
#!/usr/bin/env sh
set -e

# Decode Google credentials at runtime when provided as base64.
if [ -n "${GOOGLE_CREDENTIALS_BASE64:-}" ]; then
  echo "$GOOGLE_CREDENTIALS_BASE64" | base64 -d > /tmp/gcp-credentials.json
  export GOOGLE_APPLICATION_CREDENTIALS=/tmp/gcp-credentials.json
fi

# Bring the schema up to date before serving.  With set -e, a failed migration
# aborts the boot instead of serving against a stale schema.
alembic upgrade head

exec hypercorn app.main:app --bind "0.0.0.0:${PORT:-8000}"
```

- [ ] **Step 4: Correr los tests**

Run: `venv/bin/pytest tests/test_entrypoint.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Verificar que la imagen construye e incluye las migraciones**

```bash
docker build -t revenew-test .
docker run --rm revenew-test ls alembic/versions
```

Expected: las tres revisiones listadas.

- [ ] **Step 6: Commit**

```bash
git add entrypoint.sh tests/test_entrypoint.py
git commit -m "feat: Run alembic upgrade head before starting the server"
```

---

### Task 10: Runbook de cutover contra Railway

**Esta task no se automatiza.** Son los pasos manuales contra la base real, con verificación entre cada uno. Se ejecuta cuando las Tasks 1-9 están hechas y en verde.

**Files:**
- Create: `docs/runbooks/2026-09-21-cutover-db-v2.md`

**Precondición:** nadie usando la app mientras corre la copia.

- [ ] **Step 1: Escribir el runbook**

`docs/runbooks/2026-09-21-cutover-db-v2.md`, con este contenido y los huecos de resultados a rellenar durante la ejecución:

````markdown
# Cutover a db_v2

## 0. Censo previo (antes de tocar nada)

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings
e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
with e.connect() as c:
    for t in ["users","customers","products","purchases","purchase_items",
              "sales","sale_items","sale_item_lot_allocations","customer_product_cycles"]:
        print(f"{t:<32}{c.execute(text(f'SELECT count(*) FROM db_dev.{t}')).scalar():>6}")
    print("SUM(sales.total) =", c.execute(text("SELECT sum(total) FROM db_dev.sales")).scalar())
PY
```

Valores esperados hoy: `users 2, customers 21, products 2, purchases 38,
purchase_items 38, sales 156, sale_items 156, sale_item_lot_allocations 155,
customer_product_cycles 26`, `SUM(sales.total) = 9954.49`.

Si no coinciden, **para**: alguien escribió en la base desde el 2026-09-21.

## 1. Crear db_v2 y migrarlo a 001

```bash
POSTGRES_SCHEMA=db_v2 venv/bin/alembic upgrade <rev_001>
```

Verifica: 9 tablas en `db_v2`, todas vacías, más `alembic_version`.

## 2. Diff de esquemas antes de copiar

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings
from scripts.copy_schema_data import TABLE_ORDER
e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
q = ("SELECT column_name, data_type FROM information_schema.columns "
     "WHERE table_schema=:s AND table_name=:t ORDER BY column_name")
with e.connect() as c:
    for t in TABLE_ORDER:
        a = {r[0]: r[1] for r in c.execute(text(q), {"s":"db_dev","t":t})}
        b = {r[0]: r[1] for r in c.execute(text(q), {"s":"db_v2","t":t})}
        if a != b:
            print(f"--- {t}")
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    print(f"    {k}: db_dev={a.get(k)}  db_v2={b.get(k)}")
PY
```

Sin salida = sin drift. Con salida, resolver antes de seguir.

## 3. Copia en seco

```bash
venv/bin/python -m scripts.copy_schema_data --source db_dev --target db_v2 --dry-run
```

## 4. Copia real

```bash
venv/bin/python -m scripts.copy_schema_data --source db_dev --target db_v2
```

## 5. Verificar conteos y checksums

Repite el censo del paso 0 contra `db_v2`. Deben coincidir exactamente,
incluido `SUM(sales.total) = 9954.49`.

## 6. Aplicar 002 y 003

```bash
POSTGRES_SCHEMA=db_v2 venv/bin/alembic upgrade head
```

Vuelve a verificar los conteos: no deben haber cambiado.

## 7. Probar la app contra db_v2

```bash
POSTGRES_SCHEMA=db_v2 venv/bin/uvicorn app.main:app --port 8002
curl -s http://127.0.0.1:8002/health
```

Y pegarle a los endpoints reales con un token válido: `/api/v1/sales`,
`/api/v1/products`, `/api/v1/dashboard/summary`.

## 8. Cambiar el entorno

En Railway, `POSTGRES_SCHEMA=db_v2`. Redeploy.

## Rollback

`POSTGRES_SCHEMA=db_dev` y redeploy. `db_dev` queda intacto con sus 156 ventas.
Ningún paso de este runbook lo modifica.

## Limpieza (semanas después, no ahora)

Cuando `db_v2` lleve tiempo estable: borrar las 4 tablas vacías de `public` y,
solo entonces, considerar `db_dev`.
````

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/
git commit -m "docs: Add the db_v2 cutover runbook"
```

---

## Notas de ejecución

- **Tasks 1-9 son de código** y se pueden ejecutar seguidas. La **Task 10 es manual** contra la base real y la ejecutas tú.
- Las revisiones `001`, `002` y `003` reciben hashes de Alembic al generarse. Anota el de `001` — el runbook lo necesita en el paso 1.
- Si `test_models_and_migrations_do_not_drift` falla en cualquier momento, no sigas: los modelos y las migraciones divergieron y todo lo que venga después hereda el problema.
