# Runtime conversacional del agente Revenew — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un agente LangGraph que conversa, consulta el negocio y registra ventas, compras y movimientos de caja contra Postgres, deteniéndose a pedir confirmación humana antes de cada escritura.

**Architecture:** El grafo vive en `app/agent/` dentro del mismo repo e importa los services en proceso. Arranca como un segundo proceso (`langgraph dev` local, segundo servicio en Railway) para que `useStream` del SDK de LangGraph funcione sin adaptadores. Las escrituras se suspenden con `interrupt()` y recalculan el FIFO dentro de la transacción que escribe.

**Tech Stack:** Python 3.13, LangGraph 1.0.3, langchain-anthropic, langgraph-checkpoint-postgres, SQLAlchemy 2.0, Alembic, Pydantic v2, pytest, Firebase Admin.

**Spec:** `docs/superpowers/specs/2026-09-24-agente-runtime-conversacional-design.md`

## Global Constraints

- **Nunca escribir en `db_dev`.** Es el respaldo congelado. `alembic/env.py`, `scripts/copy_schema_data.py` y `scripts/backfill_projections.py` lo rechazan sin `REVENEW_ALLOW_DB_DEV=1`. **Nunca setear esa variable.**
- **Nunca conectar tests ni desarrollo a un host `*.rlwy.net`.** `tests/conftest.py` aborta si `TEST_DATABASE_URL` nombra Railway.
- **Base de desarrollo:** `db_local` en `localhost:55433` (`docker-compose.dev.yml`). Base de tests: `localhost:55432` (`docker-compose.test.yml`), efímera. Ambas Postgres 17.
- **`venv/bin/pip` está roto** (resuelve a un Python 3.14 vacío). Usar siempre `venv/bin/python -m pip`.
- **Modelo:** `claude-sonnet-5`. Agregar `langchain-anthropic` y `langgraph-checkpoint-postgres`; quitar `openai` y `langchain-openai`.
- **Las tablas de LangGraph van al schema `agent`**, nunca al schema de negocio: el `autogenerate` de Alembic las leería como deriva.
- **Dinero es `Decimal`**, cuantizado a 2 decimales con `_money`. Acumular y redondear **una sola vez al final**, nunca por iteración.
- **Las cantidades son fraccionarias** (medios cartones). Nada puede asumir enteros.
- **Todo test de base corre contra el Postgres desechable del 55432**, con schema desechable por test.

## Review Focus

Cinco fallas que el spec implica y que ninguna tarea ejercitaría por defecto. Cada una tiene su test asignado a la tarea que posee el código.

1. **Una venta confirmada cuyos lotes fueron consumidos entre el preview y la aprobación** debe re-pedir confirmación, no escribir con el costo viejo → Task 8.
2. **Un movimiento de caja con monto cero o negativo** debe ser rechazado antes de llegar a la base → Task 2.
3. **Una compra que se confirma dos veces** no debe generar dos `salida` → Task 5.
4. **Un token de Firebase inválido o vencido** debe cerrar el hilo con un error claro, no escribir con un `user_id` nulo → Task 6.
5. **`buscar_cliente` con dos clientes de nombre parecido** debe devolver ambos y no dejar que el agente elija → Task 7.

---

## Estructura de archivos

| Archivo | Responsabilidad |
|---|---|
| `alembic/versions/<rev>_cash_movement_saldo_inicial.py` | Quinto valor del CHECK de `cash_movements.type` |
| `app/models/cash_movement.py` | Enum, `CASH_OUTFLOW_TYPES` (modificar) |
| `app/services/cash_service.py` | `record(..., commit=True)` (modificar) |
| `app/schemas/sale.py` | `SaleCreate` gana `medioPago`, `fechaPago`, `occurredAt` |
| `app/schemas/purchase.py` | `PurchaseCreate` gana `medioPago` |
| `app/services/sales/orchestrator.py` | `create()` escribe los campos nuevos y compone la entrada de caja |
| `app/services/purchase_service.py` | `confirm()` compone la salida de caja |
| `app/agent/__init__.py` | Paquete del agente |
| `app/agent/auth.py` | Token de Firebase → `User`, sin FastAPI |
| `app/agent/session.py` | Sesión de base por invocación de herramienta |
| `app/agent/tools/read.py` | Cuatro herramientas de consulta |
| `app/agent/tools/write.py` | Tres herramientas de escritura con `interrupt()` |
| `app/agent/prompt.py` | Prompt de sistema armado desde las skills |
| `app/agent/graph.py` | El grafo, el modelo y el checkpointer |
| `langgraph.json` | Entrypoint del servidor LangGraph |

---

## Task 1: El quinto tipo de movimiento de caja

`saldo_inicial` no es una venta ni una deuda con el socio. Va en el mismo commit que `CASH_OUTFLOW_TYPES`, el `case()` del repositorio y el vocabulario de los tests: el propio código advierte que agregarlo de un lado y no del otro desincroniza el ledger del `running_balance`.

**Files:**
- Create: `alembic/versions/<rev>_cash_movement_saldo_inicial.py`
- Modify: `app/models/cash_movement.py`
- Test: `tests/test_cash_movements.py`, `tests/test_cash_service.py`

**Interfaces:**
- Produces: `CashMovementType.SALDO_INICIAL = "saldo_inicial"`. **No** entra en `CASH_OUTFLOW_TYPES`: es una entrada.

- [ ] **Step 1: Write the failing test**

En `tests/test_cash_movements.py`:

```python
def test_saldo_inicial_is_accepted_by_the_check_constraint(migrated_schema):
    engine, schema = migrated_schema
    with engine.begin() as conn:
        conn.execute(text(f"SET search_path TO {schema}"))
        conn.execute(
            text(
                "INSERT INTO cash_movements (id, occurred_at, type, amount) "
                "VALUES (gen_random_uuid(), now(), 'saldo_inicial', 500.00)"
            )
        )
        count = conn.execute(
            text("SELECT count(*) FROM cash_movements WHERE type = 'saldo_inicial'")
        ).scalar()
    assert count == 1
```

En `tests/test_cash_service.py`:

```python
def test_saldo_inicial_adds_to_the_running_balance(cash):
    # `cash` entrega un CashService pelado, no una tupla.
    cash.record(
        occurred_at=datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
        type=CashMovementType.SALDO_INICIAL,
        amount=Decimal("500.00"),
        note="efectivo al momento del corte",
    )
    assert cash.running_balance() == Decimal("500.00")


def test_saldo_inicial_does_not_count_as_owner_contribution(cash):
    cash.record(
        occurred_at=datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
        type=CashMovementType.SALDO_INICIAL,
        amount=Decimal("500.00"),
    )
    assert cash.owner_balance() == Decimal("0.00")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_cash_movements.py::test_saldo_inicial_is_accepted_by_the_check_constraint tests/test_cash_service.py -k saldo_inicial -v`
Expected: FAIL — el CHECK rechaza el valor y `CashMovementType.SALDO_INICIAL` no existe.

- [ ] **Step 3: Add the enum value**

En `app/models/cash_movement.py`:

```python
class CashMovementType(str, PyEnum):
    ENTRADA = "entrada"
    SALIDA = "salida"
    APORTE_SOCIO = "aporte_socio"
    RETIRO_SOCIO = "retiro_socio"
    SALDO_INICIAL = "saldo_inicial"
```

`CASH_OUTFLOW_TYPES` **no cambia**: `saldo_inicial` suma, no resta.

- [ ] **Step 4: Generate the migration**

```bash
POSTGRES_SCHEMA=db_local venv/bin/alembic revision -m "cash movement saldo inicial"
```

El `autogenerate` **no** detecta cambios de CHECK sobre un enum no nativo (Postgres refleja `IN (...)` como `= ANY (ARRAY[...])`), así que la migración se escribe a mano:

```python
def upgrade() -> None:
    op.drop_constraint("ck_cash_movements_cash_movement_type_enum", "cash_movements", type_="check")
    op.create_check_constraint(
        "ck_cash_movements_cash_movement_type_enum",
        "cash_movements",
        "type IN ('entrada', 'salida', 'aporte_socio', 'retiro_socio', 'saldo_inicial')",
    )


def downgrade() -> None:
    # Las filas saldo_inicial violarian el CHECK viejo: se borran primero.
    op.execute("DELETE FROM cash_movements WHERE type = 'saldo_inicial'")
    op.drop_constraint("ck_cash_movements_cash_movement_type_enum", "cash_movements", type_="check")
    op.create_check_constraint(
        "ck_cash_movements_cash_movement_type_enum",
        "cash_movements",
        "type IN ('entrada', 'salida', 'aporte_socio', 'retiro_socio')",
    )
```

Confirmá el nombre real del constraint antes de escribirlo:

```bash
docker exec revenew-dev-db psql -U revenew -d revenew_dev -c \
  "SET search_path TO db_local; SELECT conname FROM pg_constraint WHERE conrelid = 'cash_movements'::regclass AND contype = 'c';"
```

- [ ] **Step 5: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: PASS. El test existente que compara el último saldo del ledger contra `running_balance` sigue verde — es el que atrapa la desincronización.

- [ ] **Step 6: Commit**

```bash
git add app/models/cash_movement.py alembic/versions/ tests/test_cash_movements.py tests/test_cash_service.py
git commit -m "feat: Add saldo_inicial as a fifth cash movement type"
```

---

## Task 2: `record()` con control de transacción

**Files:**
- Modify: `app/services/cash_service.py`
- Test: `tests/test_cash_service.py`

**Interfaces:**
- Produces: `CashService.record(occurred_at, type, amount, payment_method=None, sale_id=None, purchase_id=None, note=None, commit=True) -> CashMovement`

- [ ] **Step 1: Write the failing test**

```python
def test_record_without_commit_leaves_the_row_rollbackable(cash_session):
    # Fixture NUEVA de esta task: `cash` no expone la sesion y este test la
    # necesita para hacer rollback.  `cash` se deja como esta -- cambiarla a
    # tupla obligaria a tocar sus cinco call sites existentes.
    service, session = cash_session
    service.record(
        occurred_at=datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
        type=CashMovementType.ENTRADA,
        amount=Decimal("10.00"),
        commit=False,
    )
    session.rollback()
    assert service.running_balance() == Decimal("0.00")


def test_record_rejects_a_zero_or_negative_amount(cash):
    for bad in (Decimal("0"), Decimal("-5.00")):
        with pytest.raises(ValueError):
            cash.record(
                occurred_at=datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
                type=CashMovementType.ENTRADA,
                amount=bad,
            )
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `venv/bin/python -m pytest tests/test_cash_service.py -k "rollbackable or zero_or_negative" -v`
Expected: el primero FALLA (`record()` no acepta `commit`); el segundo ya pasa — el guard existe y queda como red.

- [ ] **Step 3: Add the parameter**

En `app/services/cash_service.py`, agregar `commit: bool = True` al final de la firma y reemplazar el commit incondicional:

```python
        created = self.repo.create(movement)
        if commit:
            self.db.commit()
        return created
```

Revisá `CashMovementRepository.create`: si commitea por dentro, el `commit=False` no sirve. En ese caso el repositorio hace `add` + `flush` y el commit queda del lado del service.

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_cash_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/cash_service.py tests/test_cash_service.py
git commit -m "feat: Let CashService.record defer its commit to the caller"
```

---

## Task 3: Los campos de pago en los schemas de entrada

Las columnas existen desde la pieza 1 y ninguna ruta de código las escribe.

**Files:**
- Modify: `app/schemas/sale.py`, `app/schemas/purchase.py`, `app/services/sales/orchestrator.py`, `app/services/purchase_service.py`
- Create: `tests/fixtures_domain.py`
- Test: `tests/test_sale_payment_fields.py` (crear)

**Interfaces:**
- Produces: **los fixtures de dominio que usan las Tasks 4, 5, 7 y 8**, en
  `tests/fixtures_domain.py`, registrados en `tests/conftest.py`: `db_session`
  (sesion contra un schema migrado desechable), `seeded_user`, `seeded_customer`,
  `seeded_product_with_lot` (producto con un lote de 6 unidades a Q33.33),
  `seeded_product_with_one_lot` (un solo lote, para agotarlo en los tests de la T8)
  y `seeded_purchase_draft`. Hoy no existe ninguno: `tests/conftest.py` solo tiene
  `test_engine`, `throwaway_schema`, `alembic_config`, `migrated_schema` y
  `second_migrated_schema`. Segui el patron de `_session_for` en
  `tests/test_cash_service.py`, que fija el `search_path` por conexion.
- Produces: `SaleCreate.medioPago: Optional[PaymentMethod]`, `SaleCreate.fechaPago: Optional[date]`, `SaleCreate.occurredAt: Optional[datetime]`, `PurchaseCreate.medioPago: Optional[PaymentMethod]`

- [ ] **Step 1: Write the failing test**

`tests/test_sale_payment_fields.py`:

```python
"""Las columnas de pago existen desde la pieza 1 y nada las escribia."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.payment_method import PaymentMethod
from app.schemas.sale import SaleCreate, SaleItemCreate


def test_a_sale_defaults_to_paid_on_its_own_date():
    data = SaleCreate(
        customerId=uuid.uuid4(),
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=uuid.uuid4(), quantity=Decimal("1"))],
    )
    assert data.isPaymentPending is False
    assert data.medioPago == PaymentMethod.EFECTIVO
    assert data.fechaPago == date(2026, 9, 24)


def test_a_pending_sale_carries_no_payment_date():
    data = SaleCreate(
        customerId=uuid.uuid4(),
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=uuid.uuid4(), quantity=Decimal("1"))],
        isPaymentPending=True,
    )
    assert data.fechaPago is None


def test_occurred_at_defaults_to_the_sale_date_at_midnight_business_time():
    data = SaleCreate(
        customerId=uuid.uuid4(),
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=uuid.uuid4(), quantity=Decimal("1"))],
    )
    assert data.occurredAt is not None
    assert data.occurredAt.tzinfo is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_sale_payment_fields.py -v`
Expected: FAIL — `SaleCreate` no tiene esos campos.

- [ ] **Step 3: Add the fields**

En `app/schemas/sale.py`:

```python
from app.models.payment_method import PaymentMethod


class SaleCreate(BaseModel):
    customerId: uuid.UUID
    date: date
    items: list[SaleItemCreate]
    isPaymentPending: bool = False
    medioPago: Optional[PaymentMethod] = None
    fechaPago: Optional[date] = None
    occurredAt: Optional[datetime] = None

    @model_validator(mode="after")
    def default_payment_from_the_sale(self):
        # Regla del negocio: la venta se cobra al momento salvo que se diga lo
        # contrario.  Una venta pendiente no tiene fecha de pago todavia.
        if self.isPaymentPending:
            self.fechaPago = None
            self.medioPago = None
        else:
            if self.medioPago is None:
                self.medioPago = PaymentMethod.EFECTIVO
            if self.fechaPago is None:
                self.fechaPago = self.date
        if self.occurredAt is None:
            self.occurredAt = business_midnight(self.date)
        return self
```

`business_midnight` va en `app/core/datetime_utils.py`, junto a `to_business_tz`:

```python
def business_midnight(day: date) -> datetime:
    """El instante en que empieza `day` en la zona del negocio."""
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(settings.BUSINESS_TIMEZONE))
```

En `app/schemas/purchase.py`, `PurchaseCreate` gana `medioPago: Optional[PaymentMethod] = None` sin validador: la compra puede quedar a medio pagar y no se asume nada.

- [ ] **Step 4: Write the fields to the database**

En `app/services/sales/orchestrator.py`, dentro de `create()`, el `Sale(...)`:

```python
        sale = Sale(
            customer_id=data.customerId,
            user_id=user_id,
            date=data.date,
            total=total,
            is_payment_pending=data.isPaymentPending,
            payment_method=data.medioPago,
            payment_date=data.fechaPago,
            occurred_at=data.occurredAt,
            items=sale_items,
        )
```

En `app/services/purchase_service.py`, el `Purchase(...)` gana `payment_method=data.medioPago`.

- [ ] **Step 5: Add the persistence test**

```python
def test_a_created_sale_stores_its_payment_method(db_session, seeded_customer, seeded_product_with_lot):
    service = SaleService(db_session)
    data = SaleCreate(
        customerId=seeded_customer.id,
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=seeded_product_with_lot.id, quantity=Decimal("1"))],
        medioPago=PaymentMethod.TRANSFERENCIA,
    )
    sale = service.create(data, user_id=seeded_customer.id)
    db_session.refresh(sale)
    assert sale.payment_method == PaymentMethod.TRANSFERENCIA
    assert sale.payment_date == date(2026, 9, 24)
    assert sale.occurred_at is not None
```

- [ ] **Step 6: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/schemas/ app/services/ app/core/datetime_utils.py tests/test_sale_payment_fields.py
git commit -m "feat: Write the payment fields the schema already had columns for"
```

---

## Task 4: La venta pagada genera su entrada de caja

Va en `SaleService.create`, no en la herramienta: las mismas ventas deben dejar el mismo rastro se registren por el panel o por el agente. Es un cambio de comportamiento en un endpoint vivo.

**Files:**
- Modify: `app/services/sales/orchestrator.py`
- Test: `tests/test_sale_cash_hook.py` (crear)

**Interfaces:**
- Consumes: `CashService.record(..., commit=False)` de la Task 2; `data.occurredAt` y
  `data.medioPago` de la Task 3; los fixtures de dominio de la Task 3; `CashMovementType.ENTRADA`
- Produces: una venta con `is_payment_pending=False` deja exactamente un `CashMovement` de tipo `entrada`, con `sale_id` apuntándola y `amount` igual a `sale.total`

- [ ] **Step 1: Write the failing test**

`tests/test_sale_cash_hook.py`:

```python
"""Una venta pagada mueve la caja; una pendiente no."""

from datetime import date
from decimal import Decimal

from app.models.cash_movement import CashMovement, CashMovementType
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.sales import SaleService


def _sale(customer, product, pending=False):
    return SaleCreate(
        customerId=customer.id,
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=product.id, quantity=Decimal("1"))],
        isPaymentPending=pending,
    )


def test_a_paid_sale_records_one_cash_entry(db_session, seeded_customer, seeded_product_with_lot, seeded_user):
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot), user_id=seeded_user.id)

    movements = db_session.query(CashMovement).filter_by(sale_id=sale.id).all()
    assert len(movements) == 1
    assert movements[0].type == CashMovementType.ENTRADA
    assert movements[0].amount == sale.total


def test_a_pending_sale_records_no_cash_entry(db_session, seeded_customer, seeded_product_with_lot, seeded_user):
    service = SaleService(db_session)
    sale = service.create(
        _sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id
    )
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0


def test_the_cash_entry_shares_the_sale_transaction(db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch):
    """Si la caja falla, la venta no queda escrita a medias."""
    service = SaleService(db_session)

    def explode(*args, **kwargs):
        raise RuntimeError("caja caida")

    monkeypatch.setattr(service.cash_service, "record", explode)

    with pytest.raises(RuntimeError):
        service.create(_sale(seeded_customer, seeded_product_with_lot), user_id=seeded_user.id)

    db_session.rollback()
    assert db_session.query(Sale).count() == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_sale_cash_hook.py -v`
Expected: FAIL — hoy nada fuera de `cash_service.py` menciona `CashService`.

- [ ] **Step 3: Compose the cash entry inside create()**

En `SaleService.__init__`, agregar `self.cash_service = CashService(db)`.

En `create()`, **antes** del commit final y después de tener `sale.id`:

```python
        if not data.isPaymentPending:
            # commit=False: la entrada de caja y la venta son una sola
            # transaccion.  Si una falla, ninguna queda escrita.
            self.cash_service.record(
                occurred_at=data.occurredAt,
                type=CashMovementType.ENTRADA,
                amount=total,
                payment_method=data.medioPago,
                sale_id=sale.id,
                note=None,
                commit=False,
            )
```

Verificá dónde commitea `create()` hoy — si el commit vive en `SaleRepository.create`, hay que moverlo al service para que la composición sea atómica. Ese cambio es parte de esta task.

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_sale_cash_hook.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: PASS. Prestá atención a los tests de ventas existentes: ahora cada venta pagada crea una fila más.

- [ ] **Step 6: Commit**

```bash
git add app/services/sales/orchestrator.py tests/test_sale_cash_hook.py
git commit -m "feat: Record a cash entry when a sale is paid"
```

---

## Task 5: La compra confirmada genera su salida de caja

La compra tiene ciclo de vida: `create()` deja `status="draft"` y `confirm()` la vuelve real. Un borrador no gastó nada, así que el movimiento va en `confirm()`.

**Files:**
- Modify: `app/services/purchase_service.py`
- Test: `tests/test_purchase_cash_hook.py` (crear)

**Interfaces:**
- Consumes: `CashService.record(..., commit=False)` de la Task 2; `purchase.payment_method`
  y `business_midnight()` de la Task 3; el fixture `seeded_purchase_draft` de la Task 3
- Produces: confirmar una compra deja exactamente un `CashMovement` de tipo `salida` con `purchase_id` apuntándola. Confirmar dos veces no duplica.

- [ ] **Step 1: Write the failing test**

```python
def test_confirming_a_purchase_records_one_cash_exit(db_session, seeded_purchase_draft):
    service = PurchaseService(db_session)
    service.confirm(seeded_purchase_draft.id)

    movements = db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).all()
    assert len(movements) == 1
    assert movements[0].type == CashMovementType.SALIDA


def test_a_draft_purchase_records_nothing(db_session, seeded_purchase_draft):
    assert db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).count() == 0


def test_confirming_twice_does_not_duplicate_the_cash_exit(db_session, seeded_purchase_draft):
    service = PurchaseService(db_session)
    service.confirm(seeded_purchase_draft.id)
    with pytest.raises(Exception):
        service.confirm(seeded_purchase_draft.id)

    assert db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).count() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_purchase_cash_hook.py -v`
Expected: FAIL

- [ ] **Step 3: Compose the cash exit inside confirm()**

`confirm()` ya rechaza confirmar dos veces (`if purchase.status == "confirmed"`), así que la guarda contra duplicados ya existe — el test la fija. Agregar, antes del commit:

```python
            self.cash_service.record(
                occurred_at=business_midnight(purchase.date),
                type=CashMovementType.SALIDA,
                amount=purchase.total,
                payment_method=purchase.payment_method,
                purchase_id=purchase.id,
                note=None,
                commit=False,
            )
```

El `aporte_socio` **no** es automático: sólo existe cuando el dinero lo pone un socio, y eso lo pregunta el agente antes de confirmar. Se registra con `registrar_movimiento_caja` (Task 8), no acá.

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_purchase_cash_hook.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite and commit**

```bash
venv/bin/python -m pytest -q
git add app/services/purchase_service.py tests/test_purchase_cash_hook.py
git commit -m "feat: Record a cash exit when a purchase is confirmed"
```

---

## Task 6: El paquete del agente, la autenticación y la sesión

El proceso del agente no tiene FastAPI, así que no puede usar `get_current_user` (está atado a `Depends`). Lo reutilizable son `verify_firebase_token` y `UserService.get_or_create_from_firebase`.

**Files:**
- Create: `app/agent/__init__.py`, `app/agent/auth.py`, `app/agent/session.py`
- Modify: `requirements.txt`
- Test: `tests/test_agent_auth.py` (crear)

**Interfaces:**
- Produces: `resolve_user(token: str, db: Session) -> User`, que levanta `AgentAuthError` si el token no sirve. `agent_session()` — context manager que abre y cierra una `Session`. `user_id_from_config(config: RunnableConfig) -> uuid.UUID`, que lee `config["configurable"]["user_id"]` y levanta `AgentAuthError` si falta — es como las herramientas de escritura de la Task 8 saben quien firma, sin que el modelo pueda inventarlo.

- [ ] **Step 1: Write the failing test**

```python
"""El proceso del agente valida Firebase sin depender de FastAPI."""

import pytest

from app.agent.auth import AgentAuthError, resolve_user


def test_an_invalid_token_raises_instead_of_returning_none(db_session, monkeypatch):
    def reject(_token):
        raise ValueError("token vencido")

    monkeypatch.setattr("app.agent.auth.verify_firebase_token", reject)

    with pytest.raises(AgentAuthError):
        resolve_user("lo-que-sea", db_session)


def test_a_valid_token_resolves_to_a_user_row(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.agent.auth.verify_firebase_token",
        lambda _token: {"uid": "firebase-abc", "email": "kevin@example.com"},
    )

    user = resolve_user("token-valido", db_session)
    assert user.id is not None


def test_the_agent_module_does_not_import_fastapi():
    import app.agent.auth as mod
    source = Path(mod.__file__).read_text()
    assert "fastapi" not in source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_agent_auth.py -v`
Expected: FAIL — `app.agent` no existe.

- [ ] **Step 3: Install the dependencies**

```bash
venv/bin/python -m pip install langchain-anthropic langgraph-checkpoint-postgres
venv/bin/python -m pip uninstall -y openai langchain-openai
```

Actualizar `requirements.txt`: agregar `langchain-anthropic` y `langgraph-checkpoint-postgres` con sus versiones resueltas; quitar las líneas `openai==2.8.1` y `langchain-openai==1.0.3`.

- [ ] **Step 4: Write the module**

`app/agent/auth.py`:

```python
"""Autenticacion para el proceso del agente.

No importa FastAPI: `get_current_user` esta atado a `Depends` y este proceso
no tiene aplicacion web.  Lo reutilizable son las dos piezas de abajo.
"""

from sqlalchemy.orm import Session

from app.core.security import verify_firebase_token
from app.models.user import User
from app.services.user_service import UserService


class AgentAuthError(Exception):
    """El token no sirve.  El hilo se cierra con un error claro, no escribe."""


def resolve_user(token: str, db: Session) -> User:
    try:
        decoded = verify_firebase_token(token)
    except Exception as exc:
        raise AgentAuthError("El token de Firebase no es valido o vencio") from exc
    return UserService(db).get_or_create_from_firebase(decoded)
```

`app/agent/session.py`:

```python
"""Una sesion de base por invocacion de herramienta.

El servidor LangGraph es un proceso aparte del de FastAPI: no hay `Depends`
que abra y cierre la sesion, asi que cada herramienta la administra.
"""

from contextlib import contextmanager

from app.core.database import SessionLocal


@contextmanager
def agent_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 5: Run tests and commit**

```bash
venv/bin/python -m pytest tests/test_agent_auth.py -v
git add app/agent/ requirements.txt tests/test_agent_auth.py
git commit -m "feat: Add the agent package with Firebase auth and session handling"
```

---

## Task 7: Las cuatro herramientas de consulta

De solo lectura. Ninguna puede escribir.

**Files:**
- Create: `app/agent/tools/__init__.py`, `app/agent/tools/read.py`
- Test: `tests/test_agent_read_tools.py` (crear)

**Interfaces:**
- Produces: `buscar_cliente`, `previsualizar_venta`, `consultar_seguimiento`, `consultar_caja` — todas decoradas con `@tool` de `langchain_core.tools`, todas devolviendo dicts serializables (nunca objetos de SQLAlchemy, que mueren al cerrar la sesión).

- [ ] **Step 1: Write the failing test**

```python
"""Las herramientas de consulta no pueden escribir y no eligen por el usuario."""

def test_buscar_cliente_returns_every_match_without_choosing(db_session, two_similar_customers):
    result = buscar_cliente.invoke({"nombre": "Gonzalez"})
    assert len(result["clientes"]) == 2
    # No hay campo que elija uno: la desambiguacion es del agente, hablando.
    assert "seleccionado" not in result


def test_buscar_cliente_with_no_match_says_so(db_session):
    result = buscar_cliente.invoke({"nombre": "nadie-con-este-nombre"})
    assert result["clientes"] == []


def test_previsualizar_venta_returns_lots_and_cost(db_session, seeded_customer, seeded_product_with_lot):
    result = previsualizar_venta.invoke({
        "cliente_id": str(seeded_customer.id),
        "items": [{"producto_id": str(seeded_product_with_lot.id), "cantidad": "0.5"}],
        "fecha": "2026-09-24",
    })
    item = result["items"][0]
    assert item["cost_basis_unit"] is not None
    assert item["lotes"]
    assert "is_habitual_price" in item


def test_previsualizar_venta_writes_nothing(db_session, seeded_customer, seeded_product_with_lot):
    before = db_session.query(Sale).count()
    previsualizar_venta.invoke({
        "cliente_id": str(seeded_customer.id),
        "items": [{"producto_id": str(seeded_product_with_lot.id), "cantidad": "0.5"}],
        "fecha": "2026-09-24",
    })
    assert db_session.query(Sale).count() == before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_agent_read_tools.py -v`
Expected: FAIL — el módulo no existe.

- [ ] **Step 3: Write the tools**

`app/agent/tools/read.py`:

```python
"""Herramientas de consulta.  Ninguna escribe.

Todas devuelven dicts planos: un objeto de SQLAlchemy se vuelve inutilizable
cuando la sesion se cierra, y la sesion vive menos que la conversacion.
"""

from datetime import date as date_type
from decimal import Decimal

from langchain_core.tools import tool

from app.agent.session import agent_session
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.customer_service import CustomerService
from app.services.sales import SaleService


@tool
def buscar_cliente(nombre: str) -> dict:
    """Busca clientes por nombre, empresa o correo.

    Devuelve TODOS los que coinciden.  Si hay mas de uno, pregunta cual antes
    de seguir: nunca elijas por el usuario.
    """
    with agent_session() as db:
        found = CustomerService(db).get_all(search=nombre, limit=10)
        return {
            "clientes": [
                {"id": str(c.id), "nombre": c.name, "empresa": c.company, "email": c.email}
                for c in found
            ]
        }


@tool
def previsualizar_venta(cliente_id: str, items: list[dict], fecha: str) -> dict:
    """Calcula una venta sin registrarla.

    Devuelve, por item: de que lotes sale y cuanto de cada uno, el costo
    unitario, el precio sugerido, si ese precio es el habitual del cliente, el
    margen, y advertencias si los lotes no alcanzan.

    Usala SIEMPRE antes de registrar_venta.  `items` es una lista de
    {"producto_id": str, "cantidad": str, "precio_unitario": str opcional}.
    """
    with agent_session() as db:
        data = SaleCreate(
            customerId=cliente_id,
            date=date_type.fromisoformat(fecha),
            items=[
                SaleItemCreate(
                    productId=i["producto_id"],
                    quantity=Decimal(str(i["cantidad"])),
                    unitPrice=Decimal(str(i["precio_unitario"])) if i.get("precio_unitario") else None,
                )
                for i in items
            ],
        )
        preview = SaleService(db).preview_sale(data)
        return preview.model_dump(mode="json")
```

`consultar_seguimiento` envuelve `SaleService.get_follow_ups(filter_type, limit, offset)` — recordá que devuelve una **tupla** `(items, total)`. `consultar_caja` envuelve `running_balance()`, `owner_balance()` y `ledger()`.

- [ ] **Step 4: Run tests to verify they pass, then commit**

```bash
venv/bin/python -m pytest tests/test_agent_read_tools.py -v
git add app/agent/tools/ tests/test_agent_read_tools.py
git commit -m "feat: Add the agent's four read-only tools"
```

---

## Task 8: Las tres herramientas de escritura, con confirmación

La task más delicada del plan. Cada herramienta arma la operación, la suspende con `interrupt()`, y al reanudar **recalcula** antes de escribir.

**Files:**
- Create: `app/agent/tools/write.py`
- Test: `tests/test_agent_write_tools.py` (crear)

**Interfaces:**
- Consumes: `CashService.record(..., commit=False)` (Task 2), los campos de pago (Task 3), el enganche de caja (Tasks 4 y 5)
- Produces: `registrar_venta`, `registrar_compra`, `registrar_movimiento_caja`

- [ ] **Step 1: Write the failing test — el recálculo al escribir**

Es el test que justifica la task entera:

```python
"""Confirmar no es escribir: entre una cosa y la otra el inventario cambia."""

def test_a_sale_whose_lots_were_consumed_asks_again_instead_of_writing(
    db_session, seeded_customer, seeded_product_with_one_lot, seeded_user, monkeypatch
):
    """El preview mostro un lote a Q33.33.  Antes de aprobar, otra venta se lo
    llevo.  Escribir con el costo viejo seria registrar algo que no paso."""
    payload = _preview_payload(seeded_customer, seeded_product_with_one_lot)

    # Otra venta consume el lote entre el preview y la aprobacion.
    _consume_the_lot(db_session, seeded_product_with_one_lot)

    approvals = iter([{"accion": "aprobar"}])
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: next(approvals))

    result = registrar_venta.invoke(payload)

    assert result["estado"] == "recalculado"
    assert db_session.query(Sale).count() == 0
    assert "difiere" in result["mensaje"].lower()


def test_an_approved_sale_writes_exactly_what_was_shown(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})
    result = registrar_venta.invoke(_preview_payload(seeded_customer, seeded_product_with_lot))

    sale = db_session.query(Sale).one()
    assert result["estado"] == "registrado"
    assert sale.items[0].cost_basis_unit == Decimal(result["preview"]["items"][0]["cost_basis_unit"])


def test_a_cancelled_sale_writes_nothing(db_session, seeded_customer, seeded_product_with_lot, monkeypatch):
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "cancelar"})
    result = registrar_venta.invoke(_preview_payload(seeded_customer, seeded_product_with_lot))
    assert result["estado"] == "cancelado"
    assert db_session.query(Sale).count() == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_agent_write_tools.py -v`
Expected: FAIL

- [ ] **Step 3: Write registrar_venta**

```python
"""Herramientas de escritura.  Las tres se detienen antes de tocar la base.

`interrupt()` congela el grafo, guarda el estado en el checkpointer y devuelve
el payload al panel.  Al reanudar, la ejecucion sigue DESDE ACA, no desde el
principio de la conversacion.
"""

from langgraph.types import interrupt

from app.agent.session import agent_session


@tool
def registrar_venta(cliente_id: str, items: list[dict], fecha: str,
                    config: RunnableConfig,
                    medio_pago: str = "efectivo", pago_pendiente: bool = False) -> dict:
    """Registra una venta.  Se detiene a pedir confirmacion antes de escribir.

    Llama primero a previsualizar_venta y mostrale al usuario lo que devuelve.
    """
    # `config` lo inyecta LangGraph: el modelo no lo ve ni lo puede inventar.
    # El user_id lo puso el grafo al arrancar la corrida, resolviendo el token
    # de Firebase que mando el panel (Task 6).  La fila la firma esa persona.
    with agent_session() as db:
        service = SaleService(db)
        data = _build_sale_create(cliente_id, items, fecha, medio_pago, pago_pendiente)
        preview = service.preview_sale(data)

        decision = interrupt({
            "tipo": "confirmar_venta",
            "preview": preview.model_dump(mode="json"),
        })

        if decision.get("accion") == "cancelar":
            return {"estado": "cancelado"}

        if decision.get("accion") == "corregir":
            # Corregir no vuelve a la conversacion: rearma y vuelve a confirmar,
            # porque cambiar la cantidad cambia de que lotes sale y cuanto cuesta.
            return registrar_venta.invoke({**decision["valores"]})

        # Recalcular DENTRO de la transaccion que escribe: entre el preview y
        # esta linea pudo entrar otra venta que consuma los mismos lotes.
        recalculado = service.preview_sale(data)
        if not _same_costs(preview, recalculado):
            return {
                "estado": "recalculado",
                "mensaje": "El inventario cambio y el costo difiere de lo que aprobaste.",
                "anterior": preview.model_dump(mode="json"),
                "actual": recalculado.model_dump(mode="json"),
            }

        sale = service.create(data, user_id=user_id_from_config(config))
        return {
            "estado": "registrado",
            "venta_id": str(sale.id),
            "preview": recalculado.model_dump(mode="json"),
        }
```

`_same_costs` compara `cost_basis_unit` y `subtotal` de cada ítem — los dos números que la confirmación mostró.

`registrar_compra` crea **y confirma** en la misma operación: un borrador que el agente deja colgado es peor que no haberlo creado. `registrar_movimiento_caja` envuelve `CashService.record(commit=True)`, que es su propia transacción.

- [ ] **Step 4: Run tests, full suite, commit**

```bash
venv/bin/python -m pytest tests/test_agent_write_tools.py -v
venv/bin/python -m pytest -q
git add app/agent/tools/write.py tests/test_agent_write_tools.py
git commit -m "feat: Add the agent's three write tools with human confirmation"
```

---

## Task 9: El grafo, el prompt y el checkpointer

**Files:**
- Create: `app/agent/prompt.py`, `app/agent/graph.py`, `langgraph.json`
- Test: `tests/test_agent_graph.py` (crear)

**Interfaces:**
- Produces: `graph` — el objeto compilado que `langgraph.json` expone como assistant.

- [ ] **Step 1: Write the failing test**

```python
"""El grafo suspende de verdad y el estado sobrevive."""

def test_the_graph_interrupts_before_writing():
    """Con un modelo falso que pide registrar una venta, el grafo se detiene."""
    graph = build_graph(model=FakeToolCallingModel(), checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t1"}}

    result = graph.invoke({"messages": [("user", "vendi un carton a Aurita")]}, config)

    assert "__interrupt__" in result


def test_resuming_after_the_interrupt_continues_from_inside_the_tool():
    graph = build_graph(model=FakeToolCallingModel(), checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "t2"}}
    graph.invoke({"messages": [("user", "vendi un carton a Aurita")]}, config)

    final = graph.invoke(Command(resume={"accion": "cancelar"}), config)

    assert "__interrupt__" not in final


def test_the_checkpointer_targets_the_agent_schema():
    """Las tablas de LangGraph no pueden vivir con las del negocio: Alembic
    las leeria como deriva y las borraria en cada migracion."""
    assert checkpointer_schema() == "agent"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_agent_graph.py -v`
Expected: FAIL

- [ ] **Step 3: Write the prompt**

`app/agent/prompt.py` arma el prompt de sistema desde `Revenew/AGENTS.md` y las nueve skills. Ya no son procedimientos a ejecutar: son documentación de dominio. El prompt debe decir explícitamente:

- Llamá a `previsualizar_venta` antes de `registrar_venta`, siempre.
- Con más de un cliente coincidente, preguntá cuál. Nunca elijas.
- Los precios y costos los calcula el sistema. No los estimes ni los redondees vos.
- Las cantidades pueden ser fraccionarias (medio cartón es `0.5`).

- [ ] **Step 4: Write the graph**

`app/agent/graph.py` usa `create_react_agent` de `langgraph.prebuilt` con el modelo, las siete herramientas y el checkpointer. El checkpointer en producción es `PostgresSaver` apuntado al schema `agent`:

```python
def checkpointer_schema() -> str:
    # NO el schema del negocio: el autogenerate de Alembic leeria las tablas
    # de LangGraph como deriva y emitiria drop_table en cada migracion.
    return "agent"
```

`langgraph.json`:

```json
{
  "dependencies": ["."],
  "graphs": {"revenew": "./app/agent/graph.py:graph"},
  "env": ".env"
}
```

- [ ] **Step 5: Run tests, then smoke-test by hand**

```bash
venv/bin/python -m pytest tests/test_agent_graph.py -v
langgraph dev
```

Contra `db_local`. Probá: «¿a quién tengo que perseguir?», «vendí medio cartón a Aurita», y cancelá la confirmación. Mirá el trace en LangSmith.

- [ ] **Step 6: Commit**

```bash
git add app/agent/prompt.py app/agent/graph.py langgraph.json tests/test_agent_graph.py
git commit -m "feat: Wire the agent graph with its prompt and Postgres checkpointer"
```

---

## Task 10: Despliegue y documentación

**Files:**
- Create: `docs/agente.md`
- Modify: `docs/desarrollo-local.md`, `.env.example`

- [ ] **Step 1: Document the second service**

`docs/agente.md`: cómo correr `langgraph dev` local, qué variables necesita (`ANTHROPIC_API_KEY`, `LANGSMITH_*`), cómo se despliega el segundo servicio en Railway apuntando al mismo repo con otro comando de arranque, y por qué el checkpointer vive en el schema `agent`.

- [ ] **Step 2: Add the deploy checklist**

El enganche de caja cambia el comportamiento de un endpoint vivo. Antes de desplegar:

1. Cargar el movimiento `saldo_inicial` con el efectivo real al momento del corte.
2. Verificar que `running_balance()` da ese número.
3. Recién entonces desplegar, porque desde ahí cada venta pagada suma.

- [ ] **Step 3: Update `.env.example`**

Agregar, comentadas: `ANTHROPIC_API_KEY`, `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`.

- [ ] **Step 4: Commit**

```bash
git add docs/ .env.example
git commit -m "docs: Document the agent service and its deploy checklist"
```
