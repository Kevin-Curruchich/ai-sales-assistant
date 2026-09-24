# Superficie de servicios para el agente — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir la capa de servicios que el agente Revenew va a importar en proceso: partir `sale_service.py` por las costuras que marcan sus nueve skills, crear el libro de caja que no existe, y reemplazar el promedio simple de la proyección por EWMA.

**Architecture:** Un paquete `app/services/sales/` con un módulo por concern (FIFO, precios, proyección, reportes) y un orquestador que los compone. El `__init__.py` re-exporta `SaleService`, de modo que los cinco importadores actuales no se tocan. La caja entra como servicio y repositorio nuevos. Dos migraciones: `avg_interval_days` a decimal y los `CHECK` de `payment_method`.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0.41, Alembic 1.14, PostgreSQL 16, pytest.

**Spec:** `docs/superpowers/specs/2026-09-22-superficie-servicios-agente-design.md`

## Global Constraints

- **Solo services y repositorios.** Ningún endpoint nuevo, ningún schema Pydantic nuevo. Única excepción: el campo `is_habitual_price: bool = False` en `SaleItemPreview`.
- **Nunca escribir en `db_dev`.** Es el respaldo vivo con los datos reales. `alembic/env.py` lo rechaza sin `REVENEW_ALLOW_DB_DEV=1`; no la exportes.
- **Los tests nunca tocan Railway.** Corren contra la Postgres desechable de `docker-compose.test.yml` en el puerto 55432. `tests/conftest.py` aborta si `TEST_DATABASE_URL` nombra un host de Railway.
- **`venv/bin/pip` está roto** (resuelve a una instalación Python 3.14 vacía). Usar siempre `venv/bin/python -m pip`, `-m pytest`, `-m alembic`.
- **Decimales:** cantidades `Numeric(10,4)`, dinero `Numeric(14,2)`, porcentajes `Numeric(5,2)`.
- **Ninguna migración emite `schema=`.** El schema se resuelve por `search_path`.
- **Nombres de constraints** según la convención del repo: `ix_`, `uq_`, `ck_`, `fk_`, `pk_`.
- **EWMA con α = 0.3.** Croston queda fuera; `projection_method` guarda `"ewma"`.
- **`projection_confidence`:** `insufficient` (<2 compras, no se proyecta), `low` (2–3), `medium` (4–7), `high` (8+).
- **`PaymentMethod`:** `efectivo` y `transferencia`, como enum de Python con `native_enum=False` y `length=50`.
- Un commit por task, al final de la task.

## Review Focus

Cinco clases de entrada que el spec implica y que ningún test obvio ejercita. Cada una tiene su test asignado a la task dueña del código.

1. **Dos ventas al mismo cliente y producto el mismo día** → intervalo de 0 días. EWMA lo propagaría y proyectaría "hoy" para siempre. El código actual se protege con `max(int(avg), 1)`; el nuevo debe conservar un piso de 1 día. → Task 6.
2. **Movimientos de caja con el mismo instante** → una carga en lote los produce, y `SUM() OVER (ORDER BY ...)` sin desempate deja el orden indefinido entre empates, así que el saldo por fila cambia entre corridas. Peor: el marco por defecto de una ventana es `RANGE`, no `ROWS`, y con `RANGE` **todas las filas empatadas reciben el acumulado del grupo entero** — tres movimientos del mismo día mostrarían el mismo saldo. El total final coincide igual, así que hay que asertar los intermedios. → Task 2.
3. **`CHECK` sobre una columna nullable con las 194 filas en NULL.** Un `CHECK` pasa con NULL en SQL, pero si se escribe mal la migración falla contra datos reales. → Task 1.
4. **Cantidad pedida mayor que todos los lotes juntos.** Hoy `_allocate_fifo_lots` levanta un 409; al volverse función pura sin HTTP, ese contrato tiene que sobrevivir de alguna forma explícita. → Task 4.
5. **Tercera venta con descuento puntual.** `detectar-precio-habitual` mira las últimas 3 ventas; un descuento aislado no debe convertirse en el patrón del cliente. → Task 5.

---

### Task 1: `PaymentMethod` y migración `005`

**Files:**
- Create: `app/models/payment_method.py`
- Modify: `app/models/sale.py`, `app/models/purchase.py`
- Create: `alembic/versions/<rev>_payment_method_check.py` (generada)
- Create: `tests/test_payment_method.py`

**Interfaces:**
- Produces: `app.models.payment_method.PaymentMethod` — enum `str` con `EFECTIVO = "efectivo"` y `TRANSFERENCIA = "transferencia"`, y `PAYMENT_METHOD_COLUMN` como el tipo SQLAlchemy reutilizable.

Vive en su propio módulo porque lo usan tres modelos; ponerlo en cualquiera de ellos crearía un import circular.

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_payment_method.py`:

```python
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.payment_method import PaymentMethod


def test_payment_method_values():
    assert {m.value for m in PaymentMethod} == {"efectivo", "transferencia"}


def test_columns_are_varchar_50_not_a_native_enum(test_engine, migrated_schema):
    """Un enum nativo haria que agregar un medio de pago pelee con Alembic."""
    with test_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, data_type, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = :s AND column_name = 'payment_method'"
            ),
            {"s": migrated_schema},
        ).all()
    assert {r[0] for r in rows} == {"sales", "purchases", "cash_movements"}
    for _, data_type, length in rows:
        assert data_type == "character varying"
        assert length == 50


def test_check_accepts_the_two_values_and_null(test_engine, migrated_schema):
    """NULL debe pasar: hoy las 194 filas de produccion lo tienen en NULL."""
    with test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        for value in ("'efectivo'", "'transferencia'", "NULL"):
            conn.execute(
                text(
                    "INSERT INTO cash_movements (movement_date, type, amount, payment_method) "
                    f"VALUES ('2026-09-23', CAST('entrada' AS cash_movement_type_enum), 10, {value})"
                )
            )
        total = conn.execute(text("SELECT count(*) FROM cash_movements")).scalar()
    assert total == 3


def test_check_rejects_anything_else(test_engine, migrated_schema):
    with test_engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{migrated_schema}"'))
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO cash_movements (movement_date, type, amount, payment_method) "
                    "VALUES ('2026-09-23', CAST('entrada' AS cash_movement_type_enum), 10, 'Efectivo')"
                )
            )
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `venv/bin/python -m pytest tests/test_payment_method.py -v`
Expected: FAIL con `ModuleNotFoundError: app.models.payment_method`.

- [ ] **Step 3: Crear el enum**

`app/models/payment_method.py`:

```python
from enum import Enum as PyEnum

from sqlalchemy import Enum as SQLEnum


class PaymentMethod(str, PyEnum):
    """Medios de pago aceptados.

    Se renderiza como VARCHAR + CHECK, no como enum nativo de Postgres: este
    vocabulario crece (manana tarjeta, deposito), y ALTER TYPE ADD VALUE pelea
    con el DDL transaccional de Alembic, mientras que quitar un valor de un enum
    nativo es practicamente imposible.  Con un CHECK, cambiar la lista es soltar
    y recrear la constraint.
    """

    EFECTIVO = "efectivo"
    TRANSFERENCIA = "transferencia"


# length=50 a proposito: un medio de pago con nombre largo no debe obligar a
# redimensionar la columna ademas de tocar el CHECK.
PAYMENT_METHOD_COLUMN = SQLEnum(
    PaymentMethod,
    name="payment_method",
    native_enum=False,
    length=50,
    validate_strings=True,
    values_callable=lambda x: [e.value for e in x],
)
```

- [ ] **Step 4: Aplicarlo a los tres modelos**

En `app/models/sale.py` y `app/models/purchase.py` (clase `Purchase`), reemplazar la columna existente:

```python
    payment_method: Mapped[Optional[PaymentMethod]] = mapped_column(
        PAYMENT_METHOD_COLUMN, nullable=True
    )
```

Lo mismo en `app/models/cash_movement.py`. Añadir en cada archivo:

```python
from app.models.payment_method import PAYMENT_METHOD_COLUMN, PaymentMethod
```

- [ ] **Step 5: Generar la revisión**

```bash
docker compose -f docker-compose.test.yml up -d
TEST_DATABASE_URL=postgresql://revenew@localhost:55432/revenew_test \
POSTGRES_SCHEMA=gen_tmp \
DATABASE_URL=postgresql://revenew@localhost:55432/revenew_test \
  venv/bin/python -m alembic revision --autogenerate -m "payment method check constraint"
```

- [ ] **Step 6: Revisar la revisión a mano**

1. Ninguna operación lleva `schema=`.
2. Las tres tablas reciben su `CHECK`, con nombre de la convención (`ck_sales_payment_method`, etc.).
3. El `downgrade` los borra.
4. **No debe haber `ALTER TYPE` ni `CREATE TYPE`** — si aparecen, `native_enum=False` no se aplicó.
5. Si el autogenerate no detecta el `CHECK` (SQLAlchemy a veces no lo compara), escribir las seis operaciones a mano con `op.create_check_constraint` / `op.drop_constraint`.

- [ ] **Step 7: Correr toda la suite**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/models/ alembic/versions/ tests/test_payment_method.py
git commit -m "feat: Constrain payment_method to a checked vocabulary"
```

---

### Task 2: Libro de caja

Primera pieza de valor propio: la tabla existe desde la migración `002` y nada la usa.

**Files:**
- Modify: `app/models/cash_movement.py` (`movement_date` → `occurred_at`)
- Create: `alembic/versions/<rev>_cash_occurred_at.py` (generada)
- Create: `app/repositories/cash_movement_repository.py`
- Create: `app/services/cash_service.py`
- Create: `tests/test_cash_service.py`

**La tabla nace con el instante, no con la fecha.** `cash_movements` está vacía, así que
cambiar `movement_date: Date` por `occurred_at: timestamptz` no cuesta nada ahora y evita
construir el repositorio y el servicio contra una columna que habría que reemplazar. Un
instante también hace que el orden del libro casi nunca empate, en vez de depender del
desempate como mecanismo principal.

**Interfaces:**
- Consumes: `PaymentMethod` (Task 1).
- Produces:
  - `CashMovementRepository(db)` con `create(movement) -> CashMovement`, `get_all(start=None, end=None, limit=100, offset=0) -> list[CashMovement]`, `get_running_balance(as_of=None) -> Decimal`, `get_owner_balance() -> Decimal`.
  - `CashService(db)` con `record(occurred_at, type, amount, payment_method=None, sale_id=None, purchase_id=None, note=None) -> CashMovement`, `running_balance(as_of=None) -> Decimal`, `owner_balance() -> Decimal`, `ledger(start=None, end=None) -> list[tuple[CashMovement, Decimal]]`.
  - `occurred_at` y los parámetros `as_of`, `start` y `end` son `datetime` con zona.

`ledger` devuelve cada movimiento con su saldo acumulado calculado, sin almacenarlo.

- [ ] **Step 1: Cambiar el modelo y generar la migración**

En `app/models/cash_movement.py`, reemplazar `movement_date` por:

```python
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
```

```bash
docker compose -f docker-compose.test.yml up -d
TEST_DATABASE_URL=postgresql://revenew@localhost:55432/revenew_test \
POSTGRES_SCHEMA=gen_tmp0 \
DATABASE_URL=postgresql://revenew@localhost:55432/revenew_test \
  venv/bin/python -m alembic revision --autogenerate -m "cash movements occurred at"
```

Revisar que borre `movement_date` y cree `occurred_at` como NOT NULL **sin**
`server_default`: la tabla está vacía, no hay filas que rellenar.

- [ ] **Step 2: Escribir los tests que fallan**

`tests/test_cash_service.py`:

```python
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import CashMovementType
from app.services.cash_service import CashService
from tests.conftest import TEST_DATABASE_URL


def _session_for(schema: str):
    engine = create_engine(TEST_DATABASE_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()
        dbapi_connection.commit()

    return sessionmaker(bind=engine)(), engine


@pytest.fixture
def cash(migrated_schema):
    session, engine = _session_for(migrated_schema)
    try:
        yield CashService(session)
    finally:
        session.close()
        engine.dispose()


def test_running_balance_adds_entries_and_subtracts_exits(cash):
    cash.record(datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc), CashMovementType.ENTRADA, Decimal("115.00"))
    cash.record(datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc), CashMovementType.ENTRADA, Decimal("37.00"))
    cash.record(datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc), CashMovementType.SALIDA, Decimal("285.00"))
    assert cash.running_balance() == Decimal("-133.00")


def test_owner_balance_is_contributions_minus_withdrawals(cash):
    cash.record(datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc), CashMovementType.APORTE_SOCIO, Decimal("95.00"))
    cash.record(datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc), CashMovementType.APORTE_SOCIO, Decimal("125.02"))
    cash.record(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc), CashMovementType.RETIRO_SOCIO, Decimal("100.00"))
    assert cash.owner_balance() == Decimal("120.02")


def test_partner_contribution_raises_the_business_balance(cash):
    """aporte_socio es dinero que entra a la caja del negocio."""
    cash.record(datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc), CashMovementType.APORTE_SOCIO, Decimal("95.00"))
    assert cash.running_balance() == Decimal("95.00")


def test_ledger_running_balance_is_stable_with_identical_instants(cash):
    """Tres movimientos con el MISMO instante deben dar un acumulado estable.

    Con occurred_at los empates son raros, pero una carga en lote los produce.
    Sin desempate por created_at e id, el orden queda indefinido y el saldo por
    fila cambia entre corridas.  El total final coincide igual, asi que hay que
    asertar los intermedios: es lo unico que distingue lo correcto de lo roto.
    """
    for amount in ("10.00", "20.00", "30.00"):
        cash.record(datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc), CashMovementType.ENTRADA, Decimal(amount))

    primera = [balance for _, balance in cash.ledger()]
    segunda = [balance for _, balance in cash.ledger()]
    assert primera == segunda
    assert primera == [Decimal("10.00"), Decimal("30.00"), Decimal("60.00")]


def test_ledger_is_empty_before_anything_is_recorded(cash):
    assert cash.ledger() == []
    assert cash.running_balance() == Decimal("0.00")
    assert cash.owner_balance() == Decimal("0.00")
```

- [ ] **Step 3: Correr los tests para verificar que fallan**

Run: `venv/bin/python -m pytest tests/test_cash_service.py -v`
Expected: FAIL con `ModuleNotFoundError: app.services.cash_service`.

- [ ] **Step 4: Escribir el repositorio**

`app/repositories/cash_movement_repository.py`:

```python
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import CashMovement, CashMovementType


def _signed_amount():
    """Las salidas y los retiros restan; las entradas y los aportes suman."""
    return case(
        (
            CashMovement.type.in_(
                [CashMovementType.SALIDA, CashMovementType.RETIRO_SOCIO]
            ),
            -CashMovement.amount,
        ),
        else_=CashMovement.amount,
    )


class CashMovementRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, movement: CashMovement) -> CashMovement:
        self.db.add(movement)
        self.db.flush()
        self.db.refresh(movement)
        return movement

    def get_all(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CashMovement]:
        stmt = select(CashMovement)
        if start is not None:
            stmt = stmt.where(CashMovement.occurred_at >= start)
        if end is not None:
            stmt = stmt.where(CashMovement.occurred_at <= end)
        # created_at e id desempatan.  Con occurred_at los empates son raros,
        # pero una carga en lote los produce, y sin desempate el orden queda
        # indefinido y el saldo acumulado deja de ser reproducible.
        stmt = stmt.order_by(
            CashMovement.occurred_at, CashMovement.created_at, CashMovement.id
        ).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def get_running_balance(self, as_of: Optional[datetime] = None) -> Decimal:
        stmt = select(func.coalesce(func.sum(_signed_amount()), 0))
        if as_of is not None:
            stmt = stmt.where(CashMovement.occurred_at <= as_of)
        return Decimal(str(self.db.execute(stmt).scalar_one()))

    def get_owner_balance(self) -> Decimal:
        contributed = func.coalesce(
            func.sum(
                case(
                    (CashMovement.type == CashMovementType.APORTE_SOCIO, CashMovement.amount),
                    else_=0,
                )
            ),
            0,
        )
        withdrawn = func.coalesce(
            func.sum(
                case(
                    (CashMovement.type == CashMovementType.RETIRO_SOCIO, CashMovement.amount),
                    else_=0,
                )
            ),
            0,
        )
        return Decimal(str(self.db.execute(select(contributed - withdrawn)).scalar_one()))
```

- [ ] **Step 5: Escribir el servicio**

`app/services/cash_service.py`:

```python
import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models import CashMovement, CashMovementType
from app.models.payment_method import PaymentMethod
from app.repositories.cash_movement_repository import CashMovementRepository

MONEY = Decimal("0.01")
OUTFLOWS = {CashMovementType.SALIDA, CashMovementType.RETIRO_SOCIO}


class CashService:
    """Libro de caja: dinero que realmente se movio, no ganancia contable.

    El saldo acumulado NO se almacena.  Se calcula al leer.  Guardarlo como
    columna fue la fuente de desincronizacion en la hoja de calculo de la que
    viene este modelo.
    """

    def __init__(self, db: Session):
        self.db = db
        self.repo = CashMovementRepository(db)

    @staticmethod
    def _money(value: Decimal) -> Decimal:
        return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)

    def record(
        self,
        occurred_at: datetime,
        type: CashMovementType,
        amount: Decimal,
        payment_method: Optional[PaymentMethod] = None,
        sale_id: Optional[uuid.UUID] = None,
        purchase_id: Optional[uuid.UUID] = None,
        note: Optional[str] = None,
    ) -> CashMovement:
        amount = self._money(amount)
        if amount <= 0:
            raise ValueError("El monto de un movimiento de caja debe ser mayor a 0")

        movement = CashMovement(
            occurred_at=occurred_at,
            type=type,
            amount=amount,
            payment_method=payment_method,
            sale_id=sale_id,
            purchase_id=purchase_id,
            note=note,
        )
        created = self.repo.create(movement)
        self.db.commit()
        return created

    def running_balance(self, as_of: Optional[datetime] = None) -> Decimal:
        return self._money(self.repo.get_running_balance(as_of=as_of))

    def owner_balance(self) -> Decimal:
        """Lo que el negocio le debe al socio: aportes menos retiros."""
        return self._money(self.repo.get_owner_balance())

    def ledger(
        self, start: Optional[datetime] = None, end: Optional[datetime] = None
    ) -> list[tuple[CashMovement, Decimal]]:
        movements = self.repo.get_all(start=start, end=end, limit=10_000, offset=0)
        balance = Decimal("0.00")
        out: list[tuple[CashMovement, Decimal]] = []
        for m in movements:
            balance += -m.amount if m.type in OUTFLOWS else m.amount
            out.append((m, self._money(balance)))
        return out
```

- [ ] **Step 6: Correr los tests**

Run: `venv/bin/python -m pytest tests/test_cash_service.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Actualizar el test de la Task 1**

`tests/test_payment_method.py` inserta en `cash_movements` usando `movement_date`, que esta
task acaba de borrar. Reemplazar en sus dos inserts:

```python
                    "INSERT INTO cash_movements (occurred_at, type, amount, payment_method) "
                    f"VALUES ('2026-09-23T09:00:00Z', CAST('entrada' AS cash_movement_type_enum), 10, {value})"
```

Es el precio normal de cambiar un esquema: los tests que lo tocan se actualizan con él.

- [ ] **Step 8: Correr toda la suite**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/models/cash_movement.py alembic/versions/ app/repositories/ app/services/cash_service.py tests/
git commit -m "feat: Add the cash book service the agent's skills depend on"
```

---

### Task 3: Crear el paquete y mover `SaleService`

Mudanza pura: ni una línea de lógica cambia. Va sola porque es la task con más riesgo de imports y la que un revisor puede aprobar o rechazar sin mirar el resto.

**Files:**
- Create: `app/services/sales/__init__.py`
- Create: `app/services/sales/orchestrator.py` (el contenido actual de `sale_service.py`)
- Delete: `app/services/sale_service.py`
- Modify: `app/api/v1/endpoints/sales.py:15`, `app/api/v1/endpoints/follow_ups.py:5`, `app/api/v1/endpoints/dashboard.py:6`, `app/api/v1/endpoints/calendar.py:6`, `app/services/purchase_service.py:10`
- Create: `tests/test_sales_package.py`

**Interfaces:**
- Produces: `from app.services.sales import SaleService` como import canónico.

**Desviación deliberada del spec:** el spec decía que los cinco importadores no se tocarían, apoyándose en un shim en `sale_service.py`. Un shim permanente deja **dos rutas de import para la misma clase**, y el agente de la pieza 2 tendría que elegir una. Se editan las cinco líneas, que es más barato que convivir con la ambigüedad.

- [ ] **Step 1: Escribir el test que falla**

`tests/test_sales_package.py`:

```python
from pathlib import Path


def test_sale_service_is_importable_from_the_package():
    from app.services.sales import SaleService

    assert SaleService.__name__ == "SaleService"


def test_the_old_module_is_gone():
    """Un shim dejaria dos rutas de import para la misma clase."""
    repo_root = Path(__file__).resolve().parent.parent
    assert not (repo_root / "app" / "services" / "sale_service.py").exists()


def test_nothing_imports_the_old_path():
    repo_root = Path(__file__).resolve().parent.parent
    offenders = [
        str(path.relative_to(repo_root))
        for path in (repo_root / "app").rglob("*.py")
        if "from app.services.sale_service import" in path.read_text()
    ]
    assert offenders == [], f"Todavia importan la ruta vieja: {offenders}"
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `venv/bin/python -m pytest tests/test_sales_package.py -v`
Expected: FAIL — el paquete no existe y `sale_service.py` sí.

- [ ] **Step 3: Mover el archivo conservando la historia**

```bash
mkdir -p app/services/sales
git mv app/services/sale_service.py app/services/sales/orchestrator.py
```

- [ ] **Step 4: Escribir el `__init__.py`**

`app/services/sales/__init__.py`:

```python
"""Servicios del dominio de ventas.

Un modulo por concern, con los mismos limites que marcan las nueve skills del
agente Revenew: FIFO, precios, proyeccion y reportes.  `SaleService` los compone.
"""

from app.services.sales.orchestrator import SaleService

__all__ = ["SaleService"]
```

- [ ] **Step 5: Actualizar los cinco importadores**

En `app/api/v1/endpoints/sales.py`, `follow_ups.py`, `dashboard.py`, `calendar.py` y en `app/services/purchase_service.py`, reemplazar:

```python
from app.services.sale_service import SaleService
```

por:

```python
from app.services.sales import SaleService
```

- [ ] **Step 6: Verificar que la app arranca y la suite pasa**

Run: `venv/bin/python -c "import app.main; print('imports OK')"`
Expected: `imports OK`.

Run: `venv/bin/python -m pytest tests/ -v`
Expected: PASS. Ninguna prueba debería cambiar de resultado — es una mudanza.

- [ ] **Step 7: Commit**

```bash
git add -A app/services app/api tests/test_sales_package.py
git commit -m "refactor: Move SaleService into a sales package"
```

---

### Task 4: `fifo.py`

**Files:**
- Create: `app/services/sales/fifo.py`
- Modify: `app/services/sales/orchestrator.py` (quitar `_allocate_fifo_lots`, delegar)
- Create: `tests/test_sales_fifo.py`

**Interfaces:**
- Produces:
  - `InsufficientLots(Exception)` con atributos `requested: Decimal` y `available: Decimal`.
  - `allocate_fifo(lots, quantity) -> tuple[list[tuple[PurchaseItem, Decimal]], Decimal]` — **función pura**, recibe los lotes ya ordenados, no abre sesión.
  - `check_availability(lots, stock_actual, requested) -> tuple[bool, Optional[str]]`.

**Por qué función pura:** la skill `consumir-lote-fifo` dice *"separada de registrar_venta para poder probarla y depurarla de forma aislada"*. Hoy `_allocate_fifo_lots` consulta el repositorio y levanta `HTTPException`, así que no se puede probar sin base de datos ni fuera de FastAPI.

**El contrato del 409 se conserva:** la función pura levanta `InsufficientLots`; el orquestador la traduce a `HTTPException(409)` con el mismo mensaje que hoy, de modo que la API no cambia.

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_sales_fifo.py`:

```python
from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.services.sales.fifo import InsufficientLots, allocate_fifo, check_availability


@dataclass
class FakeLot:
    """Basta con remaining_quantity y unit_cost: allocate_fifo no toca nada mas."""

    remaining_quantity: Decimal
    unit_cost: Decimal


def test_consumes_the_oldest_lot_first():
    lots = [FakeLot(Decimal("6"), Decimal("33.50")), FakeLot(Decimal("6"), Decimal("35.00"))]
    allocations, cost_basis = allocate_fifo(lots, Decimal("4"))
    assert [(id(l), q) for l, q in allocations] == [(id(lots[0]), Decimal("4"))]
    assert cost_basis == Decimal("33.50")


def test_spanning_two_lots_gives_a_weighted_cost():
    """2 a Q33.50 y 2 a Q35.00 -> (67.00 + 70.00) / 4 = 34.25"""
    lots = [FakeLot(Decimal("2"), Decimal("33.50")), FakeLot(Decimal("6"), Decimal("35.00"))]
    allocations, cost_basis = allocate_fifo(lots, Decimal("4"))
    assert [q for _, q in allocations] == [Decimal("2"), Decimal("2")]
    assert cost_basis == Decimal("34.25")


def test_half_carton_takes_from_a_single_lot():
    lots = [FakeLot(Decimal("6"), Decimal("34.17"))]
    allocations, cost_basis = allocate_fifo(lots, Decimal("0.5"))
    assert [q for _, q in allocations] == [Decimal("0.5")]
    assert cost_basis == Decimal("34.17")


def test_requesting_more_than_every_lot_combined_raises():
    """Contrato que hoy vive como HTTPException 409 y debe sobrevivir."""
    lots = [FakeLot(Decimal("2"), Decimal("95.00"))]
    with pytest.raises(InsufficientLots) as excinfo:
        allocate_fifo(lots, Decimal("100"))
    assert excinfo.value.requested == Decimal("100")
    assert excinfo.value.available == Decimal("2")


def test_lots_with_nothing_left_are_skipped():
    lots = [FakeLot(Decimal("0"), Decimal("33.50")), FakeLot(Decimal("3"), Decimal("35.00"))]
    allocations, cost_basis = allocate_fifo(lots, Decimal("1"))
    assert [q for _, q in allocations] == [Decimal("1")]
    assert cost_basis == Decimal("35.00")


def test_stock_without_lots_is_reported_not_invented():
    """La skill verificar-lote-disponible lo exige explicitamente."""
    ok, problem = check_availability(lots=[], stock_actual=Decimal("5"), requested=Decimal("1"))
    assert ok is False
    assert "sin lotes" in problem.lower()


def test_availability_passes_when_lots_cover_the_request():
    lots = [FakeLot(Decimal("3"), Decimal("95.00"))]
    ok, problem = check_availability(lots=lots, stock_actual=Decimal("3"), requested=Decimal("2"))
    assert ok is True
    assert problem is None
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `venv/bin/python -m pytest tests/test_sales_fifo.py -v`
Expected: FAIL con `ModuleNotFoundError: app.services.sales.fifo`.

- [ ] **Step 3: Escribir el módulo**

`app/services/sales/fifo.py`:

```python
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Protocol

MONEY = Decimal("0.01")


class Lot(Protocol):
    """Lo unico que allocate_fifo necesita de un lote."""

    remaining_quantity: Decimal
    unit_cost: Decimal


class InsufficientLots(Exception):
    """Los lotes disponibles no alcanzan para la cantidad pedida.

    Es un error de dominio, no HTTP: el orquestador lo traduce a 409 para la
    API, y el agente lo recibe como excepcion normal.
    """

    def __init__(self, requested: Decimal, available: Decimal):
        self.requested = requested
        self.available = available
        super().__init__(
            f"Lotes FIFO insuficientes: disponible={available}, pedido={requested}"
        )


def _money(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def allocate_fifo(
    lots: list[Lot], quantity: Decimal
) -> tuple[list[tuple[Lot, Decimal]], Decimal]:
    """Consume del lote mas antiguo con existencia.

    `lots` llega ya ordenado por fecha de compra ascendente — ordenarlo es
    responsabilidad del repositorio, no de este calculo.  Devuelve las
    asignaciones y el costo unitario ponderado por las cantidades tomadas.
    """
    to_consume = Decimal(str(quantity))
    allocations: list[tuple[Lot, Decimal]] = []
    total_cost = Decimal("0.00")

    for lot in lots:
        if to_consume <= 0:
            break
        take = min(lot.remaining_quantity, to_consume)
        if take <= 0:
            continue
        allocations.append((lot, take))
        total_cost = _money(total_cost + _money(lot.unit_cost) * take)
        to_consume -= take

    if to_consume > 0:
        raise InsufficientLots(requested=quantity, available=quantity - to_consume)

    return allocations, _money(total_cost / quantity)


def check_availability(
    lots: list[Lot], stock_actual: Decimal, requested: Decimal
) -> tuple[bool, Optional[str]]:
    """Paso previo obligatorio a registrar una venta.

    El caso que importa es stock sin lotes: existencia que nunca se migro a
    lotes.  Ahi no hay costo que aplicar, y inventarlo contaminaria el FIFO.
    """
    total = sum((lot.remaining_quantity for lot in lots), Decimal("0"))

    if not lots and stock_actual > 0:
        return False, (
            f"El producto tiene stock ({stock_actual}) pero esta sin lotes. "
            "Crea el lote correspondiente antes de vender; no se puede inventar un costo."
        )
    if total < requested:
        return False, f"Lotes insuficientes: disponible={total}, pedido={requested}"
    return True, None
```

- [ ] **Step 4: Delegar desde el orquestador**

En `app/services/sales/orchestrator.py`, reemplazar el cuerpo de `_allocate_fifo_lots` por una llamada al módulo, traduciendo la excepción de dominio al mismo 409 que hoy:

```python
    def _allocate_fifo_lots(
        self,
        product_id: uuid.UUID,
        quantity: Decimal,
        sale_date: date,
    ) -> tuple[list[tuple[PurchaseItem, Decimal]], Decimal]:
        lots = self.purchase_repo.get_fifo_available_lots(
            product_id=product_id,
            as_of_date=sale_date,
            lock_for_update=True,
        )
        try:
            return allocate_fifo(lots, quantity)
        except InsufficientLots as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Insufficient FIFO lots to price sale item. "
                    f"Available priced quantity={exc.available}, requested={exc.requested}."
                ),
            ) from exc
```

Añadir el import `from app.services.sales.fifo import InsufficientLots, allocate_fifo`.

- [ ] **Step 5: Correr los tests**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: PASS. El mensaje del 409 es idéntico al anterior, así que nada que dependa de la API cambia.

- [ ] **Step 6: Commit**

```bash
git add app/services/sales/fifo.py app/services/sales/orchestrator.py tests/test_sales_fifo.py
git commit -m "refactor: Extract FIFO allocation as a pure, testable function"
```

---

### Task 5: `pricing.py` y el precio habitual

**Files:**
- Create: `app/services/sales/pricing.py`
- Modify: `app/repositories/sale_repository.py` (método nuevo)
- Modify: `app/services/sales/orchestrator.py` (delegar y usar el patrón)
- Modify: `app/schemas/sale.py` (`SaleItemPreview`)
- Create: `tests/test_sales_pricing.py`

**Interfaces:**
- Consumes: nada de tasks anteriores.
- Produces:
  - `margins(unit_price, cost_basis, base_margin) -> tuple[Decimal, Decimal]` → `(margen_real, margen_extra)`.
  - `base_margin_for(product, cost_basis) -> Decimal`.
  - `suggested_unit_price(product, cost_basis) -> Decimal`.
  - `detect_habitual_margin(observed_margins, base_margin, tolerance=Decimal("2")) -> Optional[Decimal]` — **función pura**.
  - `SaleRepository.get_recent_items_for_customer_product(customer_id, product_id, limit=3) -> list[SaleItem]`, más reciente primero.
  - `SaleItemPreview.is_habitual_price: bool = False`.

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_sales_pricing.py`:

```python
from decimal import Decimal

from app.services.sales.pricing import detect_habitual_margin, margins


def test_margins_separates_real_from_extra():
    """Aurita: cobrado 36, costo 34.17, margen base 3.50."""
    real, extra = margins(
        unit_price=Decimal("36.00"),
        cost_basis=Decimal("34.17"),
        base_margin=Decimal("3.50"),
    )
    assert real == Decimal("1.83")
    assert extra == Decimal("-1.67")


def test_three_consistent_discounted_sales_are_a_pattern():
    """Margenes de 2.50, 2.50 y 2.50 contra un base de 3.50."""
    habitual = detect_habitual_margin(
        observed_margins=[Decimal("2.50"), Decimal("2.50"), Decimal("2.50")],
        base_margin=Decimal("3.50"),
    )
    assert habitual == Decimal("2.50")


def test_margins_within_tolerance_still_count_as_a_pattern():
    """La skill acepta hasta Q2 de diferencia entre ellas."""
    habitual = detect_habitual_margin(
        observed_margins=[Decimal("2.50"), Decimal("1.83"), Decimal("2.50")],
        base_margin=Decimal("3.50"),
    )
    assert habitual is not None


def test_a_one_off_discount_does_not_become_the_pattern():
    """Dos ventas al margen estandar y una rebaja puntual no son un patron."""
    habitual = detect_habitual_margin(
        observed_margins=[Decimal("0.50"), Decimal("3.50"), Decimal("3.50")],
        base_margin=Decimal("3.50"),
    )
    assert habitual is None


def test_margins_equal_to_the_standard_are_not_a_pattern():
    """Consistentes pero iguales al base: no hay nada que sugerir distinto."""
    habitual = detect_habitual_margin(
        observed_margins=[Decimal("3.50"), Decimal("3.50"), Decimal("3.50")],
        base_margin=Decimal("3.50"),
    )
    assert habitual is None


def test_fewer_than_three_sales_is_not_enough_history():
    assert detect_habitual_margin([Decimal("2.50"), Decimal("2.50")], Decimal("3.50")) is None
    assert detect_habitual_margin([], Decimal("3.50")) is None


def test_preview_schema_carries_the_habitual_flag():
    from app.schemas.sale import SaleItemPreview

    assert "is_habitual_price" in SaleItemPreview.model_fields
    assert SaleItemPreview.model_fields["is_habitual_price"].default is False
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `venv/bin/python -m pytest tests/test_sales_pricing.py -v`
Expected: FAIL con `ModuleNotFoundError: app.services.sales.pricing`.

- [ ] **Step 3: Escribir el módulo**

`app/services/sales/pricing.py`:

```python
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

MONEY = Decimal("0.01")
HUNDRED = Decimal("100")
DEFAULT_TOLERANCE = Decimal("2")
MIN_SALES_FOR_PATTERN = 3


def _money(value: Decimal | float | int | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def base_margin_for(product, cost_basis: Decimal) -> Decimal:
    """El margen estandar del producto, en quetzales por unidad."""
    mode = getattr(product.earning_mode, "value", product.earning_mode)
    if mode == "percent":
        percent = Decimal(str(product.earning_percent or 0))
        return _money(cost_basis * (percent / HUNDRED))
    return _money(product.earning_fee_amount or 0)


def suggested_unit_price(product, cost_basis: Decimal) -> Decimal:
    return _money(cost_basis + base_margin_for(product, cost_basis))


def margins(
    unit_price: Decimal, cost_basis: Decimal, base_margin: Decimal
) -> tuple[Decimal, Decimal]:
    """margen_real = precio - costo; margen_extra = real - base."""
    real = _money(unit_price - cost_basis)
    return real, _money(real - base_margin)


def detect_habitual_margin(
    observed_margins: list[Decimal],
    base_margin: Decimal,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> Optional[Decimal]:
    """El margen habitual del cliente, si lo tiene.

    Con menos de tres ventas no hay patron suficiente.  Si los tres margenes
    caben dentro de `tolerance` entre si pero difieren del estandar, ese es el
    precio habitual del cliente.

    Una rebaja puntual no reescribe el patron: rompe la consistencia entre los
    tres y el resultado es None, que es exactamente lo que pide la skill.

    `tolerance` mide solo la consistencia entre las tres ventas.  La comparacion
    contra el estandar es exacta.
    """
    if len(observed_margins) < MIN_SALES_FOR_PATTERN:
        return None

    recent = [_money(m) for m in observed_margins[:MIN_SALES_FOR_PATTERN]]
    if max(recent) - min(recent) > tolerance:
        return None

    habitual = _money(sum(recent) / len(recent))
    # Distinto del estandar significa distinto, no "distinto por mas de la
    # tolerancia".  Usar aqui los mismos Q2 que miden la consistencia entre si
    # se tragaria el caso que motiva esta funcion: un cliente que paga Q36 donde
    # el estandar son Q37 tiene un margen habitual de 2.50 contra 3.50 — una
    # diferencia de Q1 que quedaria dentro de la tolerancia y nunca se detectaria.
    if habitual == _money(base_margin):
        return None
    return habitual
```

- [ ] **Step 4: Añadir el método al repositorio**

En `app/repositories/sale_repository.py`:

```python
    def get_recent_items_for_customer_product(
        self, customer_id: uuid.UUID, product_id: uuid.UUID, limit: int = 3
    ) -> list[SaleItem]:
        """Los items mas recientes de ese par, el mas nuevo primero."""
        stmt = (
            select(SaleItem)
            .join(Sale, Sale.id == SaleItem.sale_id)
            .where(Sale.customer_id == customer_id, SaleItem.product_id == product_id)
            .order_by(Sale.date.desc(), Sale.created_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())
```

Añadir `SaleItem` al import de modelos del archivo si falta.

- [ ] **Step 5: Añadir el campo al schema**

En `app/schemas/sale.py`, clase `SaleItemPreview`, junto a `is_price_overridden`:

```python
    is_habitual_price: bool = False
```

- [ ] **Step 6: Usar el patrón en el orquestador**

En `app/services/sales/orchestrator.py`, `_suggested_unit_price` pasa a delegar en `pricing.suggested_unit_price`. En `_resolve_sale_item_pricing`, cuando `item_data.unitPrice` es `None` y no hay descuentos, consultar el patrón antes de quedarse con el sugerido estándar:

```python
        suggested = suggested_unit_price(product, cost_basis)
        is_habitual = False

        if item_data.unitPrice is None and item_data.discountPercent is None and item_data.discountAmount is None:
            recent = self.sale_repo.get_recent_items_for_customer_product(
                customer_id=customer_id, product_id=product.id
            )
            observed = [
                Decimal(str(i.unit_price)) - Decimal(str(i.cost_basis_unit))
                for i in recent
                if i.cost_basis_unit is not None
            ]
            habitual = detect_habitual_margin(
                observed_margins=observed,
                base_margin=base_margin_for(product, cost_basis),
            )
            if habitual is not None:
                suggested = self._money(cost_basis + habitual)
                is_habitual = True
```

`is_habitual` se propaga al `SaleItemPreview` correspondiente. `_resolve_sale_item_pricing` recibe `customer_id` como parámetro nuevo; actualizar sus llamadas en `preview_sale` y en `create`.

- [ ] **Step 7: Correr toda la suite**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/sales/pricing.py app/repositories/sale_repository.py app/schemas/sale.py app/services/sales/orchestrator.py tests/test_sales_pricing.py
git commit -m "feat: Suggest each customer's habitual price instead of the standard margin"
```

---

### Task 6: `projection.py` con EWMA y migración `004`

**Files:**
- Modify: `app/models/customer_product_cycle.py` (`avg_interval_days` a `Numeric`)
- Create: `alembic/versions/<rev>_numeric_avg_interval.py` (generada)
- Create: `app/services/sales/projection.py`
- Modify: `app/services/sales/orchestrator.py` (`_update_cycle` delega)
- Create: `tests/test_sales_projection.py`

**Interfaces:**
- Produces:
  - `ALPHA = Decimal("0.3")`.
  - `ewma_interval(intervals: list[int], alpha=ALPHA) -> Decimal`.
  - `confidence_for(total_purchases: int) -> str`.
  - `project(purchase_dates: list[date]) -> tuple[Optional[Decimal], Optional[date], str, str]` → `(intervalo, fecha_proyectada, metodo, confianza)`.

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_sales_projection.py`:

```python
from datetime import date
from decimal import Decimal

from app.services.sales.projection import (
    confidence_for,
    ewma_interval,
    project,
)


def test_ewma_matches_the_hand_computed_value_for_cecy():
    """Intervalos reales de Cecy con gas: el hueco inicial de 56 dias ya casi no pesa."""
    result = ewma_interval([4, 56, 17, 21, 19])
    assert result.quantize(Decimal("0.0001")) == Decimal("19.3318")


def test_ewma_matches_the_hand_computed_value_for_tia_lily():
    """Tia Lily se esta desacelerando; la media simple daria 11.8."""
    result = ewma_interval([5, 5, 7, 8, 8, 7, 22, 20, 10, 6, 9, 34])
    assert result.quantize(Decimal("0.01")) == Decimal("17.36")


def test_ewma_weights_the_most_recent_observation_most():
    subiendo = ewma_interval([2, 2, 30])
    bajando = ewma_interval([30, 30, 2])
    assert subiendo > bajando


def test_same_day_purchases_count_as_one_occasion():
    """Tres ventas al mismo cliente el mismo dia son UNA ocasion de compra.

    Sin deduplicar, los intervalos de 0 dias arrastrarian el EWMA a cero y el
    cliente quedaria proyectado para 'hoy' de forma permanente.  Con una sola
    fecha distinta no hay historial suficiente, que es la respuesta honesta.
    """
    interval, next_date, _, confidence = project(
        [date(2026, 9, 1), date(2026, 9, 1), date(2026, 9, 1)]
    )
    assert (interval, next_date, confidence) == (None, None, "insufficient")


def test_repeats_within_a_real_history_do_not_shrink_the_interval():
    """Dos ventas el dia 1 y una el 11: el intervalo es 10, no 5."""
    interval, next_date, _, _ = project(
        [date(2026, 9, 1), date(2026, 9, 1), date(2026, 9, 11)]
    )
    assert interval == Decimal("10")
    assert next_date == date(2026, 9, 21)


def test_a_single_purchase_is_not_projected():
    """El AGENTS.md lo dice: pedir un estimado inicial en vez de inventarlo."""
    interval, next_date, method, confidence = project([date(2026, 9, 1)])
    assert interval is None
    assert next_date is None
    assert confidence == "insufficient"


def test_no_purchases_is_also_insufficient():
    interval, next_date, _, confidence = project([])
    assert (interval, next_date, confidence) == (None, None, "insufficient")


def test_method_is_always_ewma_for_now():
    _, _, method, _ = project([date(2026, 9, 1), date(2026, 9, 8)])
    assert method == "ewma"


def test_confidence_thresholds():
    assert confidence_for(1) == "insufficient"
    assert confidence_for(2) == "low"
    assert confidence_for(3) == "low"
    assert confidence_for(4) == "medium"
    assert confidence_for(7) == "medium"
    assert confidence_for(8) == "high"


def test_projection_date_is_the_last_purchase_plus_the_interval():
    interval, next_date, _, _ = project([date(2026, 9, 1), date(2026, 9, 11)])
    assert interval == Decimal("10")
    assert next_date == date(2026, 9, 21)
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `venv/bin/python -m pytest tests/test_sales_projection.py -v`
Expected: FAIL con `ModuleNotFoundError: app.services.sales.projection`.

- [ ] **Step 3: Escribir el módulo**

`app/services/sales/projection.py`:

```python
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

ALPHA = Decimal("0.3")
METHOD_EWMA = "ewma"
FOUR_PLACES = Decimal("0.0001")


def ewma_interval(intervals: list[int], alpha: Decimal = ALPHA) -> Decimal:
    """Promedio movil exponencialmente ponderado sobre los intervalos.

    La observacion mas reciente pesa `alpha`; todo lo anterior se reparte el
    resto, decayendo geometricamente.  Con alpha=0.3 las ultimas ocho compras
    concentran el 94% del peso, a diferencia de la media simple, donde una
    compra de hace tres meses pesa igual que la de ayer.
    """
    if not intervals:
        raise ValueError("ewma_interval necesita al menos un intervalo")

    estimate = Decimal(intervals[0])
    for observed in intervals[1:]:
        estimate = alpha * Decimal(observed) + (Decimal("1") - alpha) * estimate
    return estimate


def confidence_for(total_purchases: int) -> str:
    if total_purchases < 2:
        return "insufficient"
    if total_purchases <= 3:
        return "low"
    if total_purchases <= 7:
        return "medium"
    return "high"


def project(
    purchase_dates: list[date],
) -> tuple[Optional[Decimal], Optional[date], str, str]:
    """Proyecta la proxima compra a partir de las fechas historicas del par.

    Con menos de dos compras NO proyecta: devuelve None y confianza
    `insufficient`.  Inventar un intervalo por defecto es justo lo que el
    AGENTS.md prohibe.
    """
    dates = sorted(set(purchase_dates))
    confidence = confidence_for(len(dates))

    if len(dates) < 2:
        return None, None, METHOD_EWMA, confidence

    intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    # No hace falta un piso de un dia: sorted(set(...)) garantiza fechas
    # distintas, asi que todo intervalo es >= 1 y el EWMA de valores >= 1
    # tambien lo es.  Un piso aqui seria una rama que ningun test puede alcanzar.
    estimate = ewma_interval(intervals).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    next_date = dates[-1] + timedelta(days=int(estimate.to_integral_value(ROUND_HALF_UP)))
    return estimate, next_date, METHOD_EWMA, confidence
```

Nota sobre `sorted(set(...))`: dos ventas del mismo par el mismo día son **una** ocasión de compra, no dos. Deduplicar antes de medir intervalos es lo que evita el intervalo 0 en el caso normal; el piso de `MIN_INTERVAL_DAYS` cubre el resto.

- [ ] **Step 4: Cambiar el modelo y generar la migración**

En `app/models/customer_product_cycle.py`:

```python
    avg_interval_days: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4)
    )  # None hasta tener >=2 compras
```

Añadir `Decimal` y `Numeric` a los imports; quitar `Integer` si queda sin uso.

```bash
TEST_DATABASE_URL=postgresql://revenew@localhost:55432/revenew_test \
POSTGRES_SCHEMA=gen_tmp2 \
DATABASE_URL=postgresql://revenew@localhost:55432/revenew_test \
  venv/bin/python -m alembic revision --autogenerate -m "numeric avg interval days"
```

Revisar que lleve `postgresql_using="avg_interval_days::numeric(10,4)"` en el `upgrade` y `::integer` en el `downgrade`, y documentar en la propia revisión que el downgrade **pierde decimales**.

- [ ] **Step 5: Hacer que el orquestador delegue**

En `_update_cycle`, reemplazar el bloque de cálculo (media simple y el default de 30 días) por:

```python
        interval, next_date, method, confidence = project(purchase_dates)
```

y escribir `avg_interval_days=interval`, `estimated_next_purchase=next_date`,
`projection_method=method`, `projection_confidence=confidence` tanto en la rama de
actualización como en la de creación del ciclo.

- [ ] **Step 6: Escribir el test de que los ciclos sin proyección no desaparecen**

Al dejar de inventar fechas, los 7 ciclos de una sola compra se perderían **dos veces**:
`cycle_repo.get_all_with_estimation()` filtra `WHERE estimated_next_purchase IS NOT NULL`, y
`get_follow_ups` hace `if worst_days is None: continue`. El cliente se cae del seguimiento y
del calendario sin que nadie lo note.

Añadir a `tests/test_sales_projection.py`:

```python
def test_a_customer_without_a_projection_still_appears_in_follow_ups(
    test_engine, migrated_schema
):
    """El AGENTS.md pide un estimado inicial, no que el cliente desaparezca."""
    import uuid as _uuid
    from datetime import date as _date
    from decimal import Decimal as _Decimal

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from app.models import Customer, CustomerProductCycle, Product, User
    from app.services.sales import SaleService
    from tests.conftest import TEST_DATABASE_URL

    engine = create_engine(TEST_DATABASE_URL)

    @event.listens_for(engine, "connect")
    def _sp(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute(f'SET search_path TO "{migrated_schema}"')
        cur.close()
        dbapi_connection.commit()

    session = sessionmaker(bind=engine)()
    try:
        customer = Customer(name="Cliente de una sola compra")
        product = Product(sku=f"SKU-{_uuid.uuid4().hex[:6]}", name="Cilindro de gas")
        session.add_all([customer, product])
        session.flush()
        session.add(
            CustomerProductCycle(
                customer_id=customer.id, product_id=product.id,
                avg_interval_days=None, estimated_next_purchase=None,
                last_purchase_date=_date(2026, 8, 1), last_quantity=_Decimal("1"),
                total_purchases=1, projection_method="ewma",
                projection_confidence="insufficient",
            )
        )
        session.commit()

        follow_ups, total = SaleService(session).get_follow_ups(filter_type="all")
        nombres = [f.customer for f in follow_ups]
        assert "Cliente de una sola compra" in nombres
        encontrado = next(f for f in follow_ups if f.customer == "Cliente de una sola compra")
        assert encontrado.status == "needs_estimate"
    finally:
        session.close()
        engine.dispose()
```

- [ ] **Step 7: Hacer que los ciclos sin proyección lleguen al seguimiento**

En `app/repositories/customer_product_cycle_repository.py`, añadir junto a
`get_all_with_estimation`:

```python
    def get_all_for_follow_ups(self) -> list[CustomerProductCycle]:
        """Todos los ciclos, con y sin proyeccion.

        Los que no tienen fecha estimada son justamente los que hay que pedirle
        a Kevin, asi que no pueden filtrarse fuera del seguimiento.
        """
        stmt = (
            select(CustomerProductCycle)
            .options(
                joinedload(CustomerProductCycle.customer),
                joinedload(CustomerProductCycle.product),
            )
            .order_by(CustomerProductCycle.estimated_next_purchase.asc().nullslast())
        )
        return list(self.db.execute(stmt).unique().scalars().all())
```

En `get_follow_ups` del orquestador, usar ese método y reemplazar el descarte:

```python
        cycles = self.cycle_repo.get_all_for_follow_ups()
```

```python
            if worst_days is None:
                # Ningun producto de este cliente tiene proyeccion: hace falta un
                # estimado inicial.  Antes se descartaba en silencio.
                fu_status = "needs_estimate"
            elif worst_days < 0:
                fu_status = "overdue"
            elif worst_days <= 7:
                fu_status = "urgent"
            elif worst_days <= 14:
                fu_status = "upcoming"
            else:
                fu_status = "normal"

            if filter_type != "all" and fu_status == "needs_estimate":
                continue
```

Los filtros por días siguen excluyéndolos: sin fecha no hay días que comparar. Solo aparecen
en `filter_type="all"`.

Actualizar el comentario de `FollowUpResponse.status` en `app/schemas/sale.py:190` para
incluir `needs_estimate`. Es un comentario sobre un `str` — no hay schema nuevo.

- [ ] **Step 8: Correr toda la suite**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: PASS, incluido `test_models_and_migrations_do_not_drift`.

- [ ] **Step 9: Commit**

```bash
git add app/models/ app/repositories/ app/services/sales/ app/schemas/sale.py alembic/versions/ tests/test_sales_projection.py
git commit -m "feat: Replace the projection's simple mean with EWMA"
```

---

### Task 7: Backfill de los 26 ciclos

Va aparte de la Task 6 porque es un script de operador contra datos reales, y un revisor puede rechazar el backfill aprobando el servicio.

**Files:**
- Create: `scripts/backfill_projections.py`
- Create: `tests/test_backfill_projections.py`

**Interfaces:**
- Consumes: `project` de `app.services.sales.projection` (Task 6).
- Produces: `backfill_projections(session, dry_run=False) -> dict[str, int]` con las claves `updated`, `cleared`, `unchanged`.

`cleared` cuenta los ciclos que pierden su fecha inventada por tener una sola compra.

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_backfill_projections.py`:

```python
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.models import Customer, CustomerProductCycle, Product, Sale, SaleItem, User
from scripts.backfill_projections import backfill_projections
from tests.conftest import TEST_DATABASE_URL


def _session_for(schema: str):
    engine = create_engine(TEST_DATABASE_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()
        dbapi_connection.commit()

    return sessionmaker(bind=engine)(), engine


def _seed_pair(session, sale_dates: list[date]):
    """Un cliente, un producto y una venta por fecha."""
    user = User(email=f"{uuid.uuid4().hex}@example.com", role="admin")
    customer = Customer(name="Cecy")
    product = Product(sku=f"SKU-{uuid.uuid4().hex[:6]}", name="Cilindro de gas")
    session.add_all([user, customer, product])
    session.flush()

    for d in sale_dates:
        sale = Sale(customer_id=customer.id, user_id=user.id, date=d, total=Decimal("115.00"))
        session.add(sale)
        session.flush()
        session.add(
            SaleItem(
                sale_id=sale.id, product_id=product.id, quantity=Decimal("1"),
                unit_price=Decimal("115.00"), subtotal=Decimal("115.00"),
            )
        )

    cycle = CustomerProductCycle(
        customer_id=customer.id, product_id=product.id,
        avg_interval_days=Decimal("30"),          # el valor inventado que hay que corregir
        estimated_next_purchase=date(2099, 1, 1),
        last_purchase_date=sale_dates[-1], last_quantity=Decimal("1"),
        total_purchases=len(sale_dates),
    )
    session.add(cycle)
    session.commit()
    return cycle


@pytest.fixture
def session(migrated_schema):
    s, engine = _session_for(migrated_schema)
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_recomputes_the_interval_from_history(session):
    cycle = _seed_pair(session, [date(2026, 8, 1), date(2026, 8, 11), date(2026, 8, 21)])
    backfill_projections(session)
    session.refresh(cycle)
    assert cycle.avg_interval_days == Decimal("10.0000")
    assert cycle.estimated_next_purchase == date(2026, 8, 31)
    assert cycle.projection_method == "ewma"
    assert cycle.projection_confidence == "low"


def test_clears_the_invented_date_for_a_single_purchase(session):
    cycle = _seed_pair(session, [date(2026, 8, 1)])
    result = backfill_projections(session)
    session.refresh(cycle)
    assert cycle.estimated_next_purchase is None
    assert cycle.avg_interval_days is None
    assert cycle.projection_confidence == "insufficient"
    assert result["cleared"] == 1


def test_is_idempotent(session):
    cycle = _seed_pair(session, [date(2026, 8, 1), date(2026, 8, 11), date(2026, 8, 21)])
    backfill_projections(session)
    session.refresh(cycle)
    first = (cycle.avg_interval_days, cycle.estimated_next_purchase)

    backfill_projections(session)
    session.refresh(cycle)
    assert (cycle.avg_interval_days, cycle.estimated_next_purchase) == first


def test_dry_run_writes_nothing(session):
    cycle = _seed_pair(session, [date(2026, 8, 1), date(2026, 8, 11), date(2026, 8, 21)])
    before = cycle.avg_interval_days

    result = backfill_projections(session, dry_run=True)
    session.refresh(cycle)

    assert cycle.avg_interval_days == before
    assert result["updated"] == 1
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `venv/bin/python -m pytest tests/test_backfill_projections.py -v`
Expected: FAIL con `ModuleNotFoundError: scripts.backfill_projections`.

- [ ] **Step 3: Escribir el script**

`scripts/backfill_projections.py`:

```python
"""Recalcula las proyecciones de todos los ciclos desde el historial de ventas.

Uso:
    POSTGRES_SCHEMA=db_v2 venv/bin/python -m scripts.backfill_projections --dry-run
    POSTGRES_SCHEMA=db_v2 venv/bin/python -m scripts.backfill_projections

Es idempotente: reproduce las ventas a traves de EWMA cada vez, no acumula.
Los ciclos con una sola compra pierden su fecha inventada y quedan marcados
`insufficient`, que es lo que pide el AGENTS.md en vez de adivinar.
"""

from __future__ import annotations

import argparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CustomerProductCycle, Sale, SaleItem
from app.services.sales.projection import project


def _purchase_dates(session: Session, customer_id, product_id) -> list:
    stmt = (
        select(Sale.date)
        .join(SaleItem, SaleItem.sale_id == Sale.id)
        .where(Sale.customer_id == customer_id, SaleItem.product_id == product_id)
        .order_by(Sale.date)
    )
    return [row[0] for row in session.execute(stmt).all()]


def backfill_projections(session: Session, dry_run: bool = False) -> dict[str, int]:
    cycles = list(session.execute(select(CustomerProductCycle)).scalars().all())
    counts = {"updated": 0, "cleared": 0, "unchanged": 0}

    for cycle in cycles:
        dates = _purchase_dates(session, cycle.customer_id, cycle.product_id)
        interval, next_date, method, confidence = project(dates)

        unchanged = (
            cycle.avg_interval_days == interval
            and cycle.estimated_next_purchase == next_date
            and cycle.projection_method == method
            and cycle.projection_confidence == confidence
        )
        if unchanged:
            counts["unchanged"] += 1
            continue

        if next_date is None and cycle.estimated_next_purchase is not None:
            counts["cleared"] += 1
        else:
            counts["updated"] += 1

        if not dry_run:
            cycle.avg_interval_days = interval
            cycle.estimated_next_purchase = next_date
            cycle.projection_method = method
            cycle.projection_confidence = confidence
            cycle.total_purchases = len(set(dates))

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return counts


def main() -> None:
    from app.core.database import SessionLocal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        counts = backfill_projections(session, dry_run=args.dry_run)
    finally:
        session.close()

    for key, value in counts.items():
        print(f"  {key:<12} {value:>4}")
    if args.dry_run:
        print("\n  (dry-run: no se escribio nada)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Correr los tests**

Run: `venv/bin/python -m pytest tests/test_backfill_projections.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Correr toda la suite**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/backfill_projections.py tests/test_backfill_projections.py
git commit -m "feat: Add an idempotent projection backfill"
```

> **Ejecución contra producción:** este script escribe en el schema que nombre
> `POSTGRES_SCHEMA`, que hoy es `db_v2` en vivo. Correrlo es un paso de operador,
> no parte de esta task: primero `--dry-run`, revisar los conteos (se esperan 19
> actualizados y 7 limpiados), y recién entonces la corrida real.

---

### Task 8: `reporting.py`

**Files:**
- Create: `app/services/sales/reporting.py`
- Modify: `app/services/sales/orchestrator.py` (delegar)
- Create: `tests/test_sales_reporting.py`

**Interfaces:**
- Consumes: nada de tasks anteriores. `build_profit_rows` lee `item.gross_profit_total`, que
  ya viene calculado en la venta; no recalcula márgenes.
- Produces: `build_profit_rows(sales, group_by) -> list[ProfitReportRow]`, función pura sobre ventas ya cargadas.

- [ ] **Step 1: Escribir el test que falla**

`tests/test_sales_reporting.py`:

```python
from decimal import Decimal
from types import SimpleNamespace

from app.services.sales.reporting import build_profit_rows


def _item(product_id, name, quantity, subtotal, gross_profit_total):
    return SimpleNamespace(
        product_id=product_id,
        product=SimpleNamespace(name=name, sku=name[:4]),
        quantity=Decimal(quantity),
        subtotal=Decimal(subtotal),
        gross_profit_total=Decimal(gross_profit_total),
    )


def test_groups_by_product_and_sums_fractional_quantities():
    """Medio carton mas un carton son 1.5 unidades, no un error."""
    sales = [
        SimpleNamespace(items=[_item("p1", "Carton de huevos", "0.5", "18.00", "0.92")]),
        SimpleNamespace(items=[_item("p1", "Carton de huevos", "1", "37.00", "3.50")]),
    ]
    rows = build_profit_rows(sales, group_by="product")
    assert len(rows) == 1
    assert rows[0].quantity == Decimal("1.5")
    assert rows[0].revenue == Decimal("55.00")
    assert rows[0].gross_profit == Decimal("4.42")


def test_separate_products_get_separate_rows():
    sales = [
        SimpleNamespace(items=[
            _item("p1", "Carton de huevos", "1", "37.00", "3.50"),
            _item("p2", "Cilindro de gas", "1", "115.00", "20.00"),
        ])
    ]
    rows = build_profit_rows(sales, group_by="product")
    assert {r.quantity for r in rows} == {Decimal("1")}
    assert len(rows) == 2


def test_no_sales_gives_no_rows():
    assert build_profit_rows([], group_by="product") == []
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `venv/bin/python -m pytest tests/test_sales_reporting.py -v`
Expected: FAIL con `ModuleNotFoundError: app.services.sales.reporting`.

- [ ] **Step 3: Escribir el módulo**

`app/services/sales/reporting.py`:

```python
from decimal import ROUND_HALF_UP, Decimal

from app.schemas.sale import ProfitReportRow

MONEY = Decimal("0.01")


def _money(value: Decimal | float | int | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def build_profit_rows(sales, group_by: str = "product") -> list[ProfitReportRow]:
    """Agrega ingreso y ganancia por producto.

    `quantity` es Decimal, no int: el negocio vende medios cartones y sumarlos
    en un entero fue lo que devolvia 500 en el reporte.
    """
    rows: dict[str, ProfitReportRow] = {}

    for sale in sales:
        for item in sale.items:
            key = str(item.product_id)
            label = getattr(item.product, "name", key)

            if key not in rows:
                rows[key] = ProfitReportRow(
                    key=key,
                    label=label,
                    quantity=Decimal("0"),
                    revenue=Decimal("0.00"),
                    gross_profit=Decimal("0.00"),
                )

            rows[key].quantity += Decimal(str(item.quantity))
            rows[key].revenue = _money(rows[key].revenue + _money(item.subtotal))
            rows[key].gross_profit = _money(
                rows[key].gross_profit + _money(item.gross_profit_total)
            )

    return list(rows.values())
```

- [ ] **Step 4: Hacer que el orquestador delegue**

En `get_profit_report`, reemplazar el bucle de agregación por
`rows = build_profit_rows(sales, group_by=group_by)` conservando el filtrado por fechas, el
`limit` y el envoltorio `ProfitReportResponse` que ya tiene.

- [ ] **Step 5: Correr toda la suite**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/sales/reporting.py app/services/sales/orchestrator.py tests/test_sales_reporting.py
git commit -m "refactor: Extract profit report aggregation"
```

---

## Notas de ejecución

- **Las tasks 1 y 2 son independientes** entre sí y del resto; las 3 a 8 van en orden, porque cada extracción vacía un poco más el orquestador.
- **Dos migraciones**, cada una con su asunto: `payment_method` (Task 1) y `avg_interval_days` (Task 6). Anotar los hashes: el backfill de la Task 7 corre después de la segunda.
- **El backfill contra `db_v2` no es parte del plan.** Es un paso de operador, con `--dry-run` primero.
- Si `test_models_and_migrations_do_not_drift` falla en cualquier momento, parar: los modelos y las migraciones divergieron y todo lo que venga después hereda el problema.

---

### Task 9: Instantes y zona de negocio

Task añadida después de escribir el plan. Nace de una pregunta sobre cómo guardar la hora de una venta, y al medirlo apareció un bug vivo: **106 filas muestran el día equivocado** en el panel.

**Files:**
- Modify: `app/core/config.py` (constante de zona)
- Create: `app/core/datetime_utils.py`
- Modify: `app/models/sale.py`, `app/models/purchase.py`, `app/models/cash_movement.py`
- Modify: `app/schemas/sale.py`, `app/schemas/purchase.py`, `app/schemas/product.py`
- Modify: `app/services/customer_service.py`, `app/services/product_service.py`, `app/services/purchase_service.py`, `app/services/sales/orchestrator.py`
- Create: `alembic/versions/<rev>_occurred_at.py` (generada) — tercera migración del plan
- Create: `tests/test_business_timezone.py`

**Interfaces:**
- Produces:
  - `settings.BUSINESS_TIMEZONE: str = "America/Guatemala"`.
  - `to_business_tz(value: datetime) -> datetime`.
  - `format_business_date(value) -> str` → `"%d/%m/%Y"` en zona de negocio.
  - `format_business_datetime(value) -> str` → `"%d/%m/%Y %H:%M"` en zona de negocio.
  - `sales.occurred_at`, `purchases.occurred_at` — `timestamptz` nullable.

`cash_movements` ya nació con `occurred_at` en la Task 2; esta task no lo toca.

**El bug, medido:** los `_formatted` se generan con `strftime` sobre un datetime en UTC, sin convertir. Guatemala está a UTC−6, así que toda fila registrada entre 00:00 y 06:00 UTC muestra el día siguiente. Afecta a `created_at_formatted` y `updated_at_formatted`; **no** a `date_formatted` de las ventas, porque `sale.date` es una columna `Date` sin hora.

**Lo que NO cambia:** los campos `_formatted` se quedan. El panel los usa para mostrar y romperlos no aporta nada. `created_at` y `updated_at` sí pasan de `str` truncado a `datetime` real — confirmado con el usuario que el panel no los lee directamente.

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_business_timezone.py`:

```python
from datetime import date, datetime, timezone
from decimal import Decimal

from app.core.config import settings
from app.core.datetime_utils import (
    format_business_date,
    format_business_datetime,
    to_business_tz,
)


def test_business_timezone_is_configured():
    assert settings.BUSINESS_TIMEZONE == "America/Guatemala"


def test_a_late_night_sale_keeps_its_own_day():
    """Caso real: 2026-04-07 03:48 UTC son las 21:48 del 6 en Guatemala.

    El panel venia mostrando 07/04 para 78 de 156 ventas.
    """
    utc = datetime(2026, 4, 7, 3, 48, 44, tzinfo=timezone.utc)
    assert format_business_date(utc) == "06/04/2026"
    assert to_business_tz(utc).date() == date(2026, 4, 6)


def test_a_midday_timestamp_is_unaffected():
    utc = datetime(2026, 8, 27, 17, 26, 44, tzinfo=timezone.utc)
    assert format_business_date(utc) == "27/08/2026"


def test_datetime_format_shows_the_local_hour():
    utc = datetime(2026, 4, 7, 3, 48, 44, tzinfo=timezone.utc)
    assert format_business_datetime(utc) == "06/04/2026 21:48"


def test_a_naive_datetime_is_assumed_utc_not_silently_local():
    """Un naive que se interprete en la zona del servidor es como nacen los
    desfases de seis horas."""
    naive = datetime(2026, 4, 7, 3, 48, 44)
    assert format_business_date(naive) == "06/04/2026"


def test_none_formats_as_empty_not_as_a_crash():
    assert format_business_date(None) == ""
    assert format_business_datetime(None) == ""


def test_occurred_at_exists_and_is_timestamptz(test_engine, migrated_schema):
    from sqlalchemy import text

    with test_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, is_nullable, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = :s AND column_name = 'occurred_at'"
            ),
            {"s": migrated_schema},
        ).all()

    by_table = {r[0]: (r[1], r[2]) for r in rows}
    assert by_table["sales"] == ("YES", "timestamp with time zone")
    assert by_table["purchases"] == ("YES", "timestamp with time zone")
    # La caja nace con el instante en la Task 2: NOT NULL, sin filas historicas.
    assert by_table["cash_movements"] == ("NO", "timestamp with time zone")
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `venv/bin/python -m pytest tests/test_business_timezone.py -v`
Expected: FAIL con `ModuleNotFoundError: app.core.datetime_utils`.

- [ ] **Step 3: Añadir la zona a la configuración**

En `app/core/config.py`, dentro de `Settings`:

```python
    # Zona del negocio.  Los reportes agrupan por dia de negocio y las fechas se
    # muestran en esta zona para todo el mundo: una venta ocurrio en Guatemala,
    # y verla como otro dia desde otra zona rompe el vinculo con el hecho real.
    BUSINESS_TIMEZONE: str = "America/Guatemala"
```

- [ ] **Step 4: Escribir el módulo de fechas**

`app/core/datetime_utils.py`:

```python
from datetime import date, datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

from app.core.config import settings

DATE_FORMAT = "%d/%m/%Y"
DATETIME_FORMAT = "%d/%m/%Y %H:%M"


def business_tz() -> ZoneInfo:
    return ZoneInfo(settings.BUSINESS_TIMEZONE)


def to_business_tz(value: datetime) -> datetime:
    """Proyecta un instante a la zona del negocio.

    Un datetime naive se asume UTC en vez de interpretarse en la zona del
    proceso: el servidor corre en UTC y el escritorio no, y esa diferencia
    silenciosa es de donde salen los desfases de seis horas.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(business_tz())


def format_business_date(value: Optional[Union[datetime, date]]) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return to_business_tz(value).strftime(DATE_FORMAT)
    # Un date puro no tiene hora: no hay nada que convertir.
    return value.strftime(DATE_FORMAT)


def format_business_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    return to_business_tz(value).strftime(DATETIME_FORMAT)
```

- [ ] **Step 5: Añadir `occurred_at` a los modelos**

En `app/models/sale.py` y `app/models/purchase.py` (clase `Purchase`):

```python
    # El instante exacto de la venta, cuando se conoce.  NULL en las filas
    # historicas: no sabemos a que hora fue, e inventarlo seria peor.
    occurred_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

`cash_movements` no se toca: la Task 2 ya la creó con `occurred_at`.

- [ ] **Step 6: Generar y revisar la migración**

```bash
TEST_DATABASE_URL=postgresql://revenew@localhost:55432/revenew_test \
POSTGRES_SCHEMA=gen_tmp3 \
DATABASE_URL=postgresql://revenew@localhost:55432/revenew_test \
  venv/bin/python -m alembic revision --autogenerate -m "occurred at instants"
```

Revisar: las dos columnas nuevas son nullable, no llevan `server_default`, y ninguna operación emite `schema=`. **No debe aparecer nada de `cash_movements`** — si sale, es que la Task 2 no se aplicó.

- [ ] **Step 7: Corregir el formateo en los cuatro servicios**

Reemplazar cada `strftime` de fechas por los helpers. En `app/services/customer_service.py:68-79` y sus equivalentes en `product_service.py`, `purchase_service.py` y `sales/orchestrator.py`:

```python
    customer_dict["created_at"] = customer.created_at          # datetime, sin truncar
    customer_dict["updated_at"] = customer.updated_at
    customer_dict["created_at_formatted"] = format_business_datetime(customer.created_at)
    customer_dict["updated_at_formatted"] = format_business_datetime(customer.updated_at)
```

`date_formatted` de las ventas sigue usando `format_business_date(sale.date)`: `sale.date` es un `date` puro y el helper lo pasa tal cual, sin conversión.

- [ ] **Step 8: Cambiar el tipo en los tres schemas**

En `app/schemas/sale.py:155-156`, `app/schemas/purchase.py:75-76` y `app/schemas/product.py:119-120`:

```python
    created_at: datetime
    updated_at: datetime
```

Los cuatro campos `_formatted` **se quedan**: el panel los usa para mostrar. Añadir `from datetime import datetime` donde falte.

- [ ] **Step 9: Correr toda la suite**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/core/ app/models/ app/schemas/ app/services/ app/repositories/ alembic/versions/ tests/test_business_timezone.py
git commit -m "fix: Format dates in the business timezone and record exact instants"
```

> **Nota para el operador:** esta migración corre contra `db_v2` en vivo. Las dos
> columnas nuevas son nullable, así que no reescribe ni una fila existente. El efecto visible es que 106 filas dejan de
> mostrar el día equivocado en el panel.
