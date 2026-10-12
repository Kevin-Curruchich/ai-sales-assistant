# Seguimientos con cierre y espejo en Google Calendar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convertir "Clientes Prioritarios" en seguimientos con ciclo de vida (se abren con la proyección, se cierran con una venta o a mano) y reflejar los abiertos en un Google Calendar del negocio.

**Architecture:** Tabla nueva `follow_ups` como fuente de verdad. `SaleService.create` cierra/abre seguimientos en la misma unidad de trabajo que la venta; `FollowUpService` hace posponer/descartar. Las lecturas (prioritarios, métricas, calendario) leen seguimientos abiertos. Un hilo daemon, despertado con `threading.Event` sólo cuando hay trabajo, sincroniza las filas `pending`/`failed` contra la API REST de Calendar con ids de evento deterministas.

**Tech Stack:** FastAPI, SQLAlchemy 2 (ORM), Alembic, Postgres, pytest, `google-auth` (`AuthorizedSession`) + `requests`.

**Spec:** `docs/superpowers/specs/2026-10-12-seguimientos-design.md`

## Global Constraints

- Rama: `feat/seguimientos`. Un commit por task; mensajes en inglés, imperativo, con `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Comentarios y docstrings en español sin tildes, como el resto de `app/` (p. ej. `app/models/cash_movement.py`).
- "Hoy" es siempre `business_today()` (`app/core/datetime_utils.py`), nunca `date.today()`.
- Columnas de estado: VARCHAR + CHECK, nunca enum nativo de Postgres.
- Sin dependencias nuevas: sólo `google-auth==2.60.0` y `requests==2.32.5`, que ya están instaladas (las trae `firebase-admin`) y se declaran explícitas.
- Timeout de cada llamada a Google: 5 s. Reintento de filas `failed`: cada 900 s.
- Variable nueva: `GOOGLE_CALENDAR_ID`. Sin ella el sincronizador no arranca.
- Id del evento en Google = `follow_up.id.hex`.
- Tests contra Postgres: antes de correr pytest, `docker compose -f docker-compose.test.yml up -d --wait`. Comando base: `venv/bin/python -m pytest -q`.
- Antes de cada commit: `venv/bin/ruff check app tests && venv/bin/ruff format app tests`.

## Review Focus

1. **Venta atrasada registrada después de otra más reciente.** `_update_cycle` hoy pisa `last_purchase_date` con la fecha de la venta atrasada; eso rompería la regla "sólo si es posterior a la última compra" en la venta siguiente. Esperado: `last_purchase_date` es siempre la compra más reciente. Test en Task 1.
2. **El mismo producto dos veces en una venta** (dos líneas). Esperado: un solo seguimiento abierto, ningún `IntegrityError` del índice único. Test en Task 3.
3. **Un seguimiento se cierra mientras el sincronizador está llamando a Google por él.** Esperado: la fila queda `pending` (no `synced`) para que la próxima pasada borre el evento. Test en Task 6.
4. **Alguien borra el evento a mano en Google.** Google responde 409 al reinsertar un id cancelado. Esperado: el `patch` incluye `status: confirmed` y el evento reaparece. Test en Task 6.
5. **Un cliente cuyo único seguimiento se descartó.** Esperado: no reaparece en el filtro `all` como `needs_estimate` (sí tiene proyección; simplemente no hay nada pendiente). Test en Task 4.

## File Structure

| Archivo | Responsabilidad |
|---|---|
| `app/models/follow_up.py` (nuevo) | Modelo `FollowUp` y constantes de estado |
| `alembic/versions/5c2e8f1a9b3d_follow_ups.py` (nuevo) | Tabla, índice parcial, carga inicial, drop de `calendar_event_id` |
| `app/repositories/follow_up_repository.py` (nuevo) | Consultas de seguimientos |
| `app/services/follow_up_service.py` (nuevo) | Abrir/cerrar, posponer, descartar |
| `app/services/calendar_sync.py` (nuevo) | Señal `request_calendar_sync`, `sync_pending_follow_ups`, hilo |
| `app/services/google_calendar.py` (nuevo) | Cliente REST mínimo de Calendar |
| `app/schemas/follow_up.py` (nuevo) | Body de posponer y respuesta |
| `app/services/sales/orchestrator.py` | `_update_cycle` (fix), `create` (hook), lecturas |
| `app/schemas/sale.py` | `follow_up_id`/`due_date` en item; `followUpId` en evento |
| `app/api/v1/endpoints/follow_ups.py` | Endpoints `postpone` y `dismiss` |
| `app/api/dependencies.py` | `FollowUpServiceDep` |
| `app/core/config.py`, `app/main.py` | `GOOGLE_CALENDAR_ID`, arranque del hilo |
| `app/models/customer_product_cycle.py`, `app/models/__init__.py` | Quitar `calendar_event_id`, exportar `FollowUp` |
| `API_CONTRACT.md`, `types.ts`, `.env.example`, `requirements.txt` | Contrato y configuración |

---

### Task 1: `last_purchase_date` es siempre la compra más reciente

**Files:**
- Modify: `app/services/sales/orchestrator.py` (método `_update_cycle`, bloque `if cycle:` ~línea 889)
- Test: `tests/test_cycle_last_purchase.py` (nuevo)

**Interfaces:**
- Consumes: `SaleService.create(data: SaleCreate, user_id) -> Sale`
- Produces: invariante `cycle.last_purchase_date == max(fechas de venta del par)`, de la que depende Task 3.

- [ ] **Step 1: Write the failing test**

```python
"""`last_purchase_date` es la compra mas reciente, no la ultima registrada.

Una venta olvidada que se registra tarde (fecha anterior a la ultima compra)
pisaba `last_purchase_date` con su fecha vieja. La regla de seguimientos
("una venta cierra el seguimiento solo si es posterior a la ultima compra")
depende de este campo.
"""

from datetime import date
from decimal import Decimal

from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.sales import SaleService


def _sell(service, customer, product, user, day, quantity="1"):
    return service.create(
        SaleCreate(
            customerId=customer.id,
            date=day,
            items=[SaleItemCreate(productId=product.id, quantity=Decimal(quantity))],
        ),
        user_id=user.id,
    )


def test_a_backdated_sale_does_not_move_last_purchase_back(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    _sell(service, seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 10))
    _sell(service, seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 17), "2")

    _sell(service, seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 12))

    cycle = service.cycle_repo.get_by_customer_and_product(
        seeded_customer.id, seeded_product_with_lot.id
    )
    assert cycle.last_purchase_date == date(2026, 9, 17)
    assert cycle.last_quantity == Decimal("2")
    assert cycle.total_purchases == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_cycle_last_purchase.py -v`
Expected: FAIL — `assert datetime.date(2026, 9, 12) == datetime.date(2026, 9, 17)`

- [ ] **Step 3: Write minimal implementation**

En `_update_cycle`, reemplazar las dos líneas del bloque `if cycle:`

```python
            cycle.last_purchase_date = sale_date
            cycle.last_quantity = quantity
```

por:

```python
            # Una venta atrasada recalcula la proyeccion (cambia el historial)
            # pero no es la compra mas reciente: no debe mover
            # `last_purchase_date` hacia atras. Los seguimientos deciden con
            # este campo si una venta es informacion nueva.
            if sale_date >= cycle.last_purchase_date:
                cycle.last_purchase_date = sale_date
                cycle.last_quantity = quantity
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_cycle_last_purchase.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/sales/orchestrator.py tests/test_cycle_last_purchase.py
git commit -m "fix: Keep last_purchase_date on the latest purchase when a sale is backdated"
```

---

### Task 2: Modelo `FollowUp` y migración con carga inicial

**Files:**
- Create: `app/models/follow_up.py`
- Create: `alembic/versions/5c2e8f1a9b3d_follow_ups.py`
- Modify: `app/models/__init__.py`, `app/models/customer_product_cycle.py` (quitar `calendar_event_id`)
- Test: `tests/test_follow_ups_migration.py` (nuevo)

**Interfaces:**
- Produces: `app.models.follow_up.FollowUp` con columnas `id, customer_id, product_id, due_date, status, close_reason, sale_id, closed_at, closed_by, sync_status, sync_error, created_at, updated_at` y relaciones `customer`, `product`. Constantes `OPEN = "open"`, `CLOSED = "closed"`, `SYNC_PENDING = "pending"`, `SYNC_SYNCED = "synced"`, `SYNC_FAILED = "failed"`, `SYNC_SKIPPED = "skipped"`. Revisión Alembic `5c2e8f1a9b3d` (down: `07ee91fc2b95`).

- [ ] **Step 1: Write the failing test**

```python
"""La migracion de seguimientos crea uno abierto por ciclo con fecha.

Los vencidos al migrar quedan `skipped` para no llenar Google de eventos en
el pasado; los ciclos sin proyeccion no generan nada.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.datetime_utils import business_today
from app.models import Customer, CustomerProductCycle, Product
from tests.fixtures_domain import _session_for


def _cycle(customer, product, next_purchase):
    return CustomerProductCycle(
        customer_id=customer.id,
        product_id=product.id,
        estimated_next_purchase=next_purchase,
        last_purchase_date=business_today() - timedelta(days=30),
        last_quantity=Decimal("1"),
        total_purchases=2,
    )


def test_backfill_opens_one_follow_up_per_projected_cycle(alembic_config):
    command.upgrade(alembic_config, "07ee91fc2b95")
    schema = alembic_config.attributes["target_schema"]
    session, engine = _session_for(schema)
    try:
        customer = Customer(name="Aurita")
        future, past, unprojected = (
            Product(sku=f"SKU-{n}", name=f"Producto {n}") for n in ("F", "P", "U")
        )
        session.add_all([customer, future, past, unprojected])
        session.flush()
        today = business_today()
        session.add_all(
            [
                _cycle(customer, future, today + timedelta(days=10)),
                _cycle(customer, past, today - timedelta(days=10)),
                _cycle(customer, unprojected, None),
            ]
        )
        session.commit()

        command.upgrade(alembic_config, "head")

        rows = session.execute(
            text(
                "SELECT product_id, due_date, status, sync_status FROM follow_ups"
            )
        ).all()
        by_product = {r.product_id: r for r in rows}
        assert set(by_product) == {future.id, past.id}
        assert by_product[future.id].due_date == today + timedelta(days=10)
        assert by_product[future.id].sync_status == "pending"
        assert by_product[past.id].sync_status == "skipped"
        assert {r.status for r in rows} == {"open"}
    finally:
        session.close()
        engine.dispose()


def test_only_one_open_follow_up_per_pair(migrated_schema):
    session, engine = _session_for(migrated_schema)
    try:
        customer = Customer(name="Aurita")
        product = Product(sku="SKU-X", name="Huevos")
        session.add_all([customer, product])
        session.commit()
        insert = text(
            "INSERT INTO follow_ups (customer_id, product_id, due_date, status, sync_status) "
            "VALUES (:c, :p, CURRENT_DATE, 'open', 'pending')"
        )
        session.execute(insert, {"c": customer.id, "p": product.id})
        session.commit()

        with pytest.raises(IntegrityError):
            session.execute(insert, {"c": customer.id, "p": product.id})
            session.commit()
    finally:
        session.close()
        engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_follow_ups_migration.py -v`
Expected: FAIL — `relation "follow_ups" does not exist`

- [ ] **Step 3: Write the model**

`app/models/follow_up.py`:

```python
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

OPEN = "open"
CLOSED = "closed"

REASON_SALE = "sale"
REASON_POSTPONED = "postponed"
REASON_DISMISSED = "dismissed"

SYNC_PENDING = "pending"
SYNC_SYNCED = "synced"
SYNC_FAILED = "failed"
SYNC_SKIPPED = "skipped"


class FollowUp(Base):
    """Un seguimiento de un cliente + producto: se abre con la proyeccion y se
    cierra con una venta o a mano (posponer / descartar).

    La base es la fuente de verdad; Google Calendar es un espejo que
    `app/services/calendar_sync.py` mantiene a partir de `sync_status`.
    """

    __tablename__ = "follow_ups"
    __table_args__ = (
        # A lo sumo un seguimiento abierto por par, en la base y no solo en Python.
        Index(
            "uq_follow_ups_open_pair",
            "customer_id",
            "product_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        CheckConstraint("status IN ('open', 'closed')", name="status"),
        CheckConstraint(
            "close_reason IS NULL OR close_reason IN ('sale', 'postponed', 'dismissed')",
            name="close_reason",
        ),
        CheckConstraint(
            "sync_status IN ('pending', 'synced', 'failed', 'skipped')",
            name="sync_status",
        ),
        CheckConstraint(
            "(status = 'open') = (close_reason IS NULL AND closed_at IS NULL)",
            name="closed_fields",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id"), nullable=False, index=True
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=OPEN)
    close_reason: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    sale_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("sales.id", ondelete="SET NULL"), nullable=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    sync_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=SYNC_PENDING
    )
    sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped["Customer"] = relationship("Customer")
    product: Mapped["Product"] = relationship("Product")

    def __repr__(self) -> str:
        return (
            f"<FollowUp(id={self.id}, customer_id={self.customer_id}, "
            f"product_id={self.product_id}, due_date={self.due_date}, status={self.status})>"
        )
```

En `app/models/__init__.py` agregar `from app.models.follow_up import FollowUp` (orden alfabético, después de `customer_product_cycle`) y `"FollowUp"` en `__all__`.

En `app/models/customer_product_cycle.py` borrar la línea
`calendar_event_id: Mapped[Optional[str]] = mapped_column(String(255))`
(si `String` queda sin uso, Ruff lo marca: quitarlo del import).

- [ ] **Step 4: Write the migration**

`alembic/versions/5c2e8f1a9b3d_follow_ups.py`:

```python
"""follow ups

Revision ID: 5c2e8f1a9b3d
Revises: 07ee91fc2b95
Create Date: 2026-10-12 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

# revision identifiers, used by Alembic.
revision: str = "5c2e8f1a9b3d"
down_revision: Union[str, None] = "07ee91fc2b95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "follow_ups",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("close_reason", sa.String(length=10), nullable=True),
        sa.Column("sale_id", sa.Uuid(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.Uuid(), nullable=True),
        sa.Column("sync_status", sa.String(length=10), nullable=False),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed')", name=op.f("ck_follow_ups_status")
        ),
        sa.CheckConstraint(
            "close_reason IS NULL OR close_reason IN ('sale', 'postponed', 'dismissed')",
            name=op.f("ck_follow_ups_close_reason"),
        ),
        sa.CheckConstraint(
            "sync_status IN ('pending', 'synced', 'failed', 'skipped')",
            name=op.f("ck_follow_ups_sync_status"),
        ),
        sa.CheckConstraint(
            "(status = 'open') = (close_reason IS NULL AND closed_at IS NULL)",
            name=op.f("ck_follow_ups_closed_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_follow_ups_customer_id_customers"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_follow_ups_product_id_products"),
        ),
        sa.ForeignKeyConstraint(
            ["sale_id"],
            ["sales.id"],
            name=op.f("fk_follow_ups_sale_id_sales"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["closed_by"], ["users.id"], name=op.f("fk_follow_ups_closed_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_follow_ups")),
    )
    op.create_index(
        op.f("ix_follow_ups_customer_id"), "follow_ups", ["customer_id"], unique=False
    )
    op.create_index(
        op.f("ix_follow_ups_product_id"), "follow_ups", ["product_id"], unique=False
    )
    op.create_index(
        "uq_follow_ups_open_pair",
        "follow_ups",
        ["customer_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

    # Carga inicial: un seguimiento abierto por ciclo con proyeccion. Los
    # vencidos al migrar quedan `skipped`: la app los muestra vencidos, pero
    # Google no se llena de eventos en fechas pasadas.
    op.execute(
        sa.text(
            """
            INSERT INTO follow_ups (customer_id, product_id, due_date, status, sync_status)
            SELECT customer_id, product_id, estimated_next_purchase, 'open',
                   CASE
                       WHEN estimated_next_purchase < (now() AT TIME ZONE :tz)::date
                       THEN 'skipped'
                       ELSE 'pending'
                   END
            FROM customer_product_cycles
            WHERE estimated_next_purchase IS NOT NULL
            """
        ).bindparams(tz=settings.BUSINESS_TIMEZONE)
    )

    # Nunca se escribio: el id del evento ahora es el id del seguimiento.
    op.drop_column("customer_product_cycles", "calendar_event_id")


def downgrade() -> None:
    op.add_column(
        "customer_product_cycles",
        sa.Column("calendar_event_id", sa.String(length=255), nullable=True),
    )
    op.drop_index("uq_follow_ups_open_pair", table_name="follow_ups")
    op.drop_index(op.f("ix_follow_ups_product_id"), table_name="follow_ups")
    op.drop_index(op.f("ix_follow_ups_customer_id"), table_name="follow_ups")
    op.drop_table("follow_ups")
```

- [ ] **Step 5: Run tests to verify they pass, and that the downgrade round-trips**

Run: `venv/bin/python -m pytest tests/test_follow_ups_migration.py -v`
Expected: PASS (2 tests)

Run: `venv/bin/python -m pytest tests -q -k "migration or alembic"`
Expected: PASS (los tests de migración existentes siguen verdes)

- [ ] **Step 6: Commit**

```bash
git add app/models/follow_up.py app/models/__init__.py app/models/customer_product_cycle.py \
  alembic/versions/5c2e8f1a9b3d_follow_ups.py tests/test_follow_ups_migration.py
git commit -m "feat: Add the follow_ups table, backfilled from projected cycles"
```

---

### Task 3: Abrir/cerrar con ventas, posponer y descartar

**Files:**
- Create: `app/repositories/follow_up_repository.py`
- Create: `app/services/follow_up_service.py`
- Create: `app/services/calendar_sync.py` (sólo la señal en esta task)
- Modify: `app/services/sales/orchestrator.py` (`__init__`, `create`)
- Test: `tests/test_follow_up_lifecycle.py` (nuevo)

**Interfaces:**
- Consumes: `FollowUp` y constantes (Task 2); invariante de Task 1.
- Produces:
  - `FollowUpRepository(db)`: `get_by_id(id) -> Optional[FollowUp]`, `get_open(customer_id, product_id) -> Optional[FollowUp]`, `get_open_with_relations() -> list[FollowUp]` (customer y product cargados, orden `due_date` asc), `get_to_sync() -> list[FollowUp]` (`sync_status IN ('pending','failed')`, customer y product cargados), `add(follow_up) -> None`.
  - `FollowUpService(db)`: `record_purchase(customer_id, product_id, sale_id, next_due: Optional[date]) -> None` (no hace commit), `postpone(follow_up_id, new_date: date, user_id) -> FollowUp`, `dismiss(follow_up_id, user_id) -> None`.
  - `app.services.calendar_sync.request_calendar_sync() -> None`. Los llamadores lo invocan como `calendar_sync.request_calendar_sync()` (atributo del módulo) para que los tests lo puedan reemplazar.
  - `SaleService.follow_up_service`, `SaleService.follow_up_repo`.

- [ ] **Step 1: Write the failing tests**

`tests/test_follow_up_lifecycle.py`:

```python
"""Ciclo de vida de un seguimiento: venta, posponer, descartar.

Caso que motivo la regla de fechas: ventas de huevos a Aurita el 10 y el 17;
seguimiento abierto en la proyeccion. Una venta olvidada registrada tarde
(fecha anterior a la ultima compra) no es informacion nueva: no cierra el
seguimiento ni le mueve la fecha.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

import app.services.calendar_sync as calendar_sync
from app.core.datetime_utils import business_today
from app.models.follow_up import FollowUp
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.follow_up_service import FollowUpService
from app.services.sales import SaleService


@pytest.fixture
def sync_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(calendar_sync, "request_calendar_sync", lambda: calls.append(1))
    return calls


def _sell(service, customer, product, user, day, lines=1):
    return service.create(
        SaleCreate(
            customerId=customer.id,
            date=day,
            items=[
                SaleItemCreate(productId=product.id, quantity=Decimal("1"))
                for _ in range(lines)
            ],
        ),
        user_id=user.id,
    )


def _follow_ups(db, customer, product):
    return (
        db.query(FollowUp)
        .filter_by(customer_id=customer.id, product_id=product.id)
        .order_by(FollowUp.created_at, FollowUp.due_date)
        .all()
    )


def _open(db, customer, product):
    return [f for f in _follow_ups(db, customer, product) if f.status == "open"]


@pytest.fixture
def two_sales(db_session, seeded_customer, seeded_product_with_lot, seeded_user, sync_requests):
    service = SaleService(db_session)
    _sell(service, seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 10))
    _sell(service, seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 17))
    return service


def test_a_first_sale_opens_nothing(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, sync_requests
):
    _sell(SaleService(db_session), seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 10))

    assert _follow_ups(db_session, seeded_customer, seeded_product_with_lot) == []


def test_a_second_sale_opens_one_at_the_projection(
    db_session, seeded_customer, seeded_product_with_lot, two_sales
):
    cycle = two_sales.cycle_repo.get_by_customer_and_product(
        seeded_customer.id, seeded_product_with_lot.id
    )
    [opened] = _open(db_session, seeded_customer, seeded_product_with_lot)
    assert opened.due_date == cycle.estimated_next_purchase
    assert opened.sync_status == "pending"


def test_a_newer_sale_closes_the_open_one_and_opens_the_next(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [before] = _open(db_session, seeded_customer, seeded_product_with_lot)

    sale = _sell(two_sales, seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 24))

    db_session.refresh(before)
    assert (before.status, before.close_reason, before.sale_id) == ("closed", "sale", sale.id)
    assert before.closed_at is not None and before.closed_by is None
    [after] = _open(db_session, seeded_customer, seeded_product_with_lot)
    assert after.id != before.id


def test_a_backdated_sale_leaves_follow_ups_alone(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [before] = _open(db_session, seeded_customer, seeded_product_with_lot)
    due = before.due_date

    _sell(two_sales, seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 12))

    [after] = _open(db_session, seeded_customer, seeded_product_with_lot)
    assert after.id == before.id and after.due_date == due


def test_a_second_sale_on_the_same_day_leaves_follow_ups_alone(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [before] = _open(db_session, seeded_customer, seeded_product_with_lot)

    _sell(two_sales, seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 17))

    [after] = _open(db_session, seeded_customer, seeded_product_with_lot)
    assert after.id == before.id


def test_the_same_product_twice_in_one_sale_opens_a_single_follow_up(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    _sell(two_sales, seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 24), lines=2)

    assert len(_open(db_session, seeded_customer, seeded_product_with_lot)) == 1


def test_a_sale_requests_a_calendar_sync(two_sales, sync_requests):
    assert len(sync_requests) == 2


def test_postpone_closes_and_reopens_on_the_chosen_date(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales, sync_requests
):
    [current] = _open(db_session, seeded_customer, seeded_product_with_lot)
    new_date = business_today() + timedelta(days=5)
    before_calls = len(sync_requests)

    new = FollowUpService(db_session).postpone(current.id, new_date, seeded_user.id)

    db_session.refresh(current)
    assert (current.status, current.close_reason, current.closed_by) == (
        "closed",
        "postponed",
        seeded_user.id,
    )
    assert (new.status, new.due_date) == ("open", new_date)
    assert len(sync_requests) == before_calls + 1


@pytest.mark.parametrize("days", [0, -1])
def test_postpone_rejects_a_date_that_is_not_in_the_future(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales, days
):
    [current] = _open(db_session, seeded_customer, seeded_product_with_lot)

    with pytest.raises(HTTPException) as exc:
        FollowUpService(db_session).postpone(
            current.id, business_today() + timedelta(days=days), seeded_user.id
        )
    assert exc.value.status_code == 422


def test_postpone_or_dismiss_a_closed_follow_up_is_a_conflict(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [current] = _open(db_session, seeded_customer, seeded_product_with_lot)
    service = FollowUpService(db_session)
    service.dismiss(current.id, seeded_user.id)

    with pytest.raises(HTTPException) as exc:
        service.dismiss(current.id, seeded_user.id)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        service.postpone(current.id, business_today() + timedelta(days=3), seeded_user.id)
    assert exc.value.status_code == 409


def test_an_unknown_follow_up_is_not_found(db_session, seeded_user):
    import uuid

    with pytest.raises(HTTPException) as exc:
        FollowUpService(db_session).dismiss(uuid.uuid4(), seeded_user.id)
    assert exc.value.status_code == 404


def test_dismiss_opens_nothing_until_the_next_sale(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [current] = _open(db_session, seeded_customer, seeded_product_with_lot)

    FollowUpService(db_session).dismiss(current.id, seeded_user.id)

    db_session.refresh(current)
    assert (current.status, current.close_reason) == ("closed", "dismissed")
    assert _open(db_session, seeded_customer, seeded_product_with_lot) == []

    _sell(two_sales, seeded_customer, seeded_product_with_lot, seeded_user, date(2026, 9, 24))

    assert len(_open(db_session, seeded_customer, seeded_product_with_lot)) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_follow_up_lifecycle.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.calendar_sync'`

- [ ] **Step 3: Write the signal module**

`app/services/calendar_sync.py`:

```python
"""Espejo de los seguimientos en Google Calendar.

Quien escribe seguimientos llama a `request_calendar_sync()` despues de su
commit; eso solo enciende una senal. El hilo que la atiende (mas abajo, Task 7)
duerme sin temporizador mientras no haya trabajo.
"""

import threading

_wake = threading.Event()


def request_calendar_sync() -> None:
    """Pide una pasada de sincronizacion. No bloquea ni falla."""
    _wake.set()
```

- [ ] **Step 4: Write the repository**

`app/repositories/follow_up_repository.py`:

```python
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.follow_up import OPEN, SYNC_FAILED, SYNC_PENDING, FollowUp


class FollowUpRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, follow_up_id: uuid.UUID) -> Optional[FollowUp]:
        return self.db.get(FollowUp, follow_up_id)

    def get_open(
        self, customer_id: uuid.UUID, product_id: uuid.UUID
    ) -> Optional[FollowUp]:
        stmt = select(FollowUp).where(
            FollowUp.customer_id == customer_id,
            FollowUp.product_id == product_id,
            FollowUp.status == OPEN,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_open_with_relations(self) -> list[FollowUp]:
        stmt = (
            select(FollowUp)
            .options(joinedload(FollowUp.customer), joinedload(FollowUp.product))
            .where(FollowUp.status == OPEN)
            .order_by(FollowUp.due_date.asc())
        )
        return list(self.db.execute(stmt).unique().scalars().all())

    def get_to_sync(self) -> list[FollowUp]:
        stmt = (
            select(FollowUp)
            .options(joinedload(FollowUp.customer), joinedload(FollowUp.product))
            .where(FollowUp.sync_status.in_([SYNC_PENDING, SYNC_FAILED]))
            .order_by(FollowUp.updated_at.asc())
        )
        return list(self.db.execute(stmt).unique().scalars().all())

    def add(self, follow_up: FollowUp) -> None:
        self.db.add(follow_up)
```

- [ ] **Step 5: Write the service**

`app/services/follow_up_service.py`:

```python
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.datetime_utils import business_today
from app.models.follow_up import (
    CLOSED,
    OPEN,
    REASON_DISMISSED,
    REASON_POSTPONED,
    REASON_SALE,
    SYNC_PENDING,
    FollowUp,
)
from app.repositories.follow_up_repository import FollowUpRepository
from app.services import calendar_sync


class FollowUpService:
    """Abre y cierra seguimientos. Toda escritura deja la fila `pending` para
    que el espejo de Google la recoja."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = FollowUpRepository(db)

    def record_purchase(
        self,
        customer_id: uuid.UUID,
        product_id: uuid.UUID,
        sale_id: uuid.UUID,
        next_due: Optional[date],
    ) -> None:
        """Una compra nueva del par: cierra el abierto y abre el siguiente.

        No hace commit: corre dentro de la transaccion de la venta. Quien
        decide si la venta es informacion nueva es `SaleService.create`.
        """
        current = self.repo.get_open(customer_id, product_id)
        if current is not None:
            self._close(current, REASON_SALE, sale_id=sale_id)
        if next_due is not None:
            self._open(customer_id, product_id, next_due)

    def postpone(
        self, follow_up_id: uuid.UUID, new_date: date, user_id: uuid.UUID
    ) -> FollowUp:
        current = self._get_open_or_raise(follow_up_id)
        if new_date <= business_today():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="La nueva fecha del seguimiento debe ser posterior a hoy",
            )
        self._close(current, REASON_POSTPONED, closed_by=user_id)
        new = self._open(current.customer_id, current.product_id, new_date)
        self.db.commit()
        self.db.refresh(new)
        calendar_sync.request_calendar_sync()
        return new

    def dismiss(self, follow_up_id: uuid.UUID, user_id: uuid.UUID) -> None:
        current = self._get_open_or_raise(follow_up_id)
        self._close(current, REASON_DISMISSED, closed_by=user_id)
        self.db.commit()
        calendar_sync.request_calendar_sync()

    def _get_open_or_raise(self, follow_up_id: uuid.UUID) -> FollowUp:
        follow_up = self.repo.get_by_id(follow_up_id)
        if follow_up is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Follow-up {follow_up_id} not found",
            )
        if follow_up.status != OPEN:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El seguimiento ya esta cerrado",
            )
        return follow_up

    def _close(
        self,
        follow_up: FollowUp,
        reason: str,
        *,
        sale_id: Optional[uuid.UUID] = None,
        closed_by: Optional[uuid.UUID] = None,
    ) -> None:
        follow_up.status = CLOSED
        follow_up.close_reason = reason
        follow_up.sale_id = sale_id
        follow_up.closed_by = closed_by
        follow_up.closed_at = datetime.now(timezone.utc)
        follow_up.sync_status = SYNC_PENDING
        follow_up.sync_error = None
        # El UPDATE tiene que llegar a la base antes del INSERT del siguiente:
        # si no, el indice unico parcial ve dos abiertos del mismo par.
        self.db.flush()

    def _open(
        self, customer_id: uuid.UUID, product_id: uuid.UUID, due_date: date
    ) -> FollowUp:
        follow_up = FollowUp(
            customer_id=customer_id,
            product_id=product_id,
            due_date=due_date,
            status=OPEN,
            sync_status=SYNC_PENDING,
        )
        self.repo.add(follow_up)
        self.db.flush()
        return follow_up
```

- [ ] **Step 6: Wire it into `SaleService`**

En `app/services/sales/orchestrator.py`:

Imports (junto a los demás de `app.repositories` / `app.services`):

```python
from app.repositories.follow_up_repository import FollowUpRepository
from app.services import calendar_sync
from app.services.follow_up_service import FollowUpService
```

En `__init__`, después de `self.cash_service = CashService(db)`:

```python
        self.follow_up_repo = FollowUpRepository(db)
        self.follow_up_service = FollowUpService(db)
```

En `create`, reemplazar:

```python
        for item_data in data.items:
            self._update_cycle(
                data.customerId, item_data.productId, data.date, item_data.quantity
            )

        self.db.commit()
        self.db.refresh(sale)

        return sale
```

por:

```python
        for item_data in data.items:
            previous = self.cycle_repo.get_by_customer_and_product(
                data.customerId, item_data.productId
            )
            # Copiar el valor ANTES de `_update_cycle`, que muta este mismo objeto.
            previous_last = previous.last_purchase_date if previous else None
            self._update_cycle(
                data.customerId, item_data.productId, data.date, item_data.quantity
            )
            # Solo una venta posterior a la ultima compra es informacion nueva.
            # Una atrasada, o una segunda del mismo dia (o la misma linea dos
            # veces en esta venta), no toca los seguimientos.
            if previous_last is None or data.date > previous_last:
                cycle = self.cycle_repo.get_by_customer_and_product(
                    data.customerId, item_data.productId
                )
                self.follow_up_service.record_purchase(
                    data.customerId,
                    item_data.productId,
                    sale.id,
                    cycle.estimated_next_purchase,
                )

        self.db.commit()
        self.db.refresh(sale)
        calendar_sync.request_calendar_sync()

        return sale
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_follow_up_lifecycle.py tests/test_cycle_last_purchase.py -v`
Expected: PASS

Run: `venv/bin/python -m pytest -q`
Expected: PASS (las lecturas todavía usan la proyección; cambian en Task 4).

- [ ] **Step 8: Commit**

```bash
git add app/repositories/follow_up_repository.py app/services/follow_up_service.py \
  app/services/calendar_sync.py app/services/sales/orchestrator.py tests/test_follow_up_lifecycle.py
git commit -m "feat: Close follow-ups with sales, and postpone or dismiss them by hand"
```

---

### Task 4: Lecturas sobre seguimientos abiertos

**Files:**
- Modify: `app/schemas/sale.py` (`FollowUpItemResponse`, `CalendarEvent`)
- Modify: `app/services/sales/orchestrator.py` (`get_follow_ups`, `get_follow_up_metrics`, `get_calendar_events`)
- Modify tests: `tests/test_business_today_everywhere.py`, `tests/test_follow_up_metrics.py`, `tests/test_sales_projection.py`, `tests/test_agent_read_tools.py`
- Test: `tests/test_follow_up_reads.py` (nuevo)

**Interfaces:**
- Consumes: `SaleService.follow_up_repo.get_open_with_relations()`, `SaleService.follow_up_service` (Task 3).
- Produces: `FollowUpItemResponse.follow_up_id: Optional[uuid.UUID]`, `FollowUpItemResponse.due_date: Optional[date]`, `CalendarEvent.followUpId: uuid.UUID`.

- [ ] **Step 1: Write the failing tests**

`tests/test_follow_up_reads.py`:

```python
"""Prioritarios, metricas y calendario leen seguimientos abiertos.

El caso de Aurita: un seguimiento descartado o pospuesto ya no la deja
"Urgente" en el dashboard.
"""

from datetime import timedelta
from decimal import Decimal

from app.core.datetime_utils import business_today
from app.models import CustomerProductCycle
from app.models.follow_up import FollowUp
from app.services.follow_up_service import FollowUpService
from app.services.sales import SaleService


def _cycle_and_follow_up(db, customer, product, days_until):
    due = business_today() + timedelta(days=days_until)
    db.add(
        CustomerProductCycle(
            customer_id=customer.id,
            product_id=product.id,
            avg_interval_days=Decimal("7"),
            estimated_next_purchase=due,
            last_purchase_date=business_today() - timedelta(days=5),
            last_quantity=Decimal("1"),
            total_purchases=3,
        )
    )
    follow_up = FollowUp(
        customer_id=customer.id, product_id=product.id, due_date=due, status="open"
    )
    db.add(follow_up)
    db.commit()
    return follow_up


def test_priority_list_shows_open_follow_ups_with_their_id_and_date(
    db_session, seeded_customer, seeded_product_with_lot
):
    follow_up = _cycle_and_follow_up(db_session, seeded_customer, seeded_product_with_lot, 2)

    [row], total = SaleService(db_session).get_follow_ups(filter_type="7_days")

    assert total == 1 and row.status == "urgent"
    assert row.items[0].follow_up_id == follow_up.id
    assert row.items[0].due_date == follow_up.due_date
    assert row.items[0].days_until == 2


def test_a_dismissed_follow_up_leaves_every_list(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    follow_up = _cycle_and_follow_up(db_session, seeded_customer, seeded_product_with_lot, 2)
    FollowUpService(db_session).dismiss(follow_up.id, seeded_user.id)
    service = SaleService(db_session)

    for filter_type in ("7_days", "all"):
        assert service.get_follow_ups(filter_type=filter_type) == ([], 0)
    assert service.get_follow_up_metrics().next7Days == 0
    calendar = service.get_calendar_events(
        business_today(), business_today() + timedelta(days=7)
    )
    assert calendar.summary.upcoming == 0


def test_a_postponed_follow_up_uses_the_new_date(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    follow_up = _cycle_and_follow_up(db_session, seeded_customer, seeded_product_with_lot, 2)
    new_date = business_today() + timedelta(days=20)
    new = FollowUpService(db_session).postpone(follow_up.id, new_date, seeded_user.id)
    service = SaleService(db_session)

    assert service.get_follow_ups(filter_type="7_days") == ([], 0)
    [row], _ = service.get_follow_ups(filter_type="30_days")
    assert (row.items[0].follow_up_id, row.items[0].due_date) == (new.id, new_date)
    calendar = service.get_calendar_events(new_date, new_date)
    [event] = calendar.dates[0].events
    assert event.followUpId == new.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_follow_up_reads.py -v`
Expected: FAIL — `AttributeError: 'FollowUpItemResponse' object has no attribute 'follow_up_id'`

- [ ] **Step 3: Extend the schemas**

En `app/schemas/sale.py`, en `FollowUpItemResponse`, agregar como primeros campos:

```python
    # Null solo para clientes `needs_estimate`, que no tienen seguimiento.
    follow_up_id: Optional[uuid.UUID] = None
    # Fecha del seguimiento: la proyeccion al abrirse o la elegida al posponer.
    # Es la que debe mostrarse como "Proxima compra".
    due_date: Optional[date] = None
```

En `CalendarEvent`, agregar después de `date: date`:

```python
    followUpId: uuid.UUID
```

- [ ] **Step 4: Rewrite the three reads**

En `app/services/sales/orchestrator.py`, reemplazar `get_follow_ups` completo por:

```python
    def _follow_up_item(
        self,
        product_id: uuid.UUID,
        product,
        cycle: Optional[CustomerProductCycle],
        today: date,
        follow_up=None,
    ) -> FollowUpItemResponse:
        due = follow_up.due_date if follow_up is not None else None
        return FollowUpItemResponse(
            follow_up_id=follow_up.id if follow_up is not None else None,
            due_date=due,
            product_id=product_id,
            product_name=product.name,
            avg_interval_days=cycle.avg_interval_days if cycle else None,
            last_purchase_date=cycle.last_purchase_date if cycle else None,
            last_quantity=cycle.last_quantity if cycle else Decimal("0"),
            estimated_next_purchase=cycle.estimated_next_purchase if cycle else None,
            days_until=(due - today).days if due is not None else None,
            current_stock=product.stock,
            min_stock=product.min_stock,
            stock_alert=product.stock <= product.min_stock,
        )

    def get_follow_ups(
        self, filter_type: str = "all", limit: int = 10, offset: int = 0
    ) -> tuple[list[FollowUpResponse], int]:
        today = business_today()
        cycles = self.cycle_repo.get_all_for_follow_ups()
        cycle_by_pair = {(c.customer_id, c.product_id): c for c in cycles}

        customers: dict[uuid.UUID, object] = {}
        items_by_customer: dict[uuid.UUID, list[FollowUpItemResponse]] = {}

        # Lo pendiente son los seguimientos abiertos: uno cerrado por venta,
        # pospuesto o descartado ya no cuenta.
        for fu in self.follow_up_repo.get_open_with_relations():
            customers[fu.customer_id] = fu.customer
            items_by_customer.setdefault(fu.customer_id, []).append(
                self._follow_up_item(
                    fu.product_id,
                    fu.product,
                    cycle_by_pair.get((fu.customer_id, fu.product_id)),
                    today,
                    follow_up=fu,
                )
            )

        # `needs_estimate`: clientes sin ninguna proyeccion. Un cliente con
        # proyeccion pero sin seguimiento abierto (descartado) no es eso: no
        # hay nada pendiente con el, y no debe reaparecer.
        if filter_type == "all":
            cycles_by_customer: dict[uuid.UUID, list[CustomerProductCycle]] = {}
            for c in cycles:
                cycles_by_customer.setdefault(c.customer_id, []).append(c)
            for customer_id, c_cycles in cycles_by_customer.items():
                if customer_id in items_by_customer:
                    continue
                if any(c.estimated_next_purchase for c in c_cycles):
                    continue
                customers[customer_id] = c_cycles[0].customer
                items_by_customer[customer_id] = [
                    self._follow_up_item(c.product_id, c.product, c, today)
                    for c in c_cycles
                ]

        follow_ups: list[FollowUpResponse] = []
        for customer_id, items in items_by_customer.items():
            days = [i.days_until for i in items if i.days_until is not None]
            worst_days = min(days) if days else None

            if worst_days is None:
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
            if filter_type == "overdue" and fu_status != "overdue":
                continue
            if filter_type == "7_days" and worst_days > 7:
                continue
            if filter_type == "14_days" and worst_days > 14:
                continue
            if filter_type == "30_days" and worst_days > 30:
                continue

            items.sort(key=lambda i: i.days_until if i.days_until is not None else 9999)
            customer = customers[customer_id]
            follow_ups.append(
                FollowUpResponse(
                    customer_id=customer_id,
                    customer=customer.name,
                    email=customer.email,
                    status=fu_status,
                    items=items,
                )
            )

        follow_ups.sort(
            key=lambda f: min(
                (i.days_until for i in f.items if i.days_until is not None),
                default=9999,
            )
        )

        total = len(follow_ups)
        return follow_ups[offset : offset + limit], total
```

En `get_follow_up_metrics`, reemplazar desde `cycles = self.cycle_repo.get_all_with_estimation()` hasta el final del `for` por:

```python
        open_follow_ups = self.follow_up_repo.get_open_with_relations()

        customer_worst: dict[uuid.UUID, int] = {}
        for fu in open_follow_ups:
            days = (fu.due_date - today).days
            if fu.customer_id not in customer_worst or days < customer_worst[fu.customer_id]:
                customer_worst[fu.customer_id] = days
```

y actualizar el comentario de arriba: los tiles se definen por `days_until` de seguimientos abiertos; los `needs_estimate` no tienen seguimiento.

En `get_calendar_events`, reemplazar el bloque desde `cycles = self.cycle_repo.get_all_with_estimation()` hasta el cierre del `for` por:

```python
        all_events: list[CalendarEvent] = []
        for fu in self.follow_up_repo.get_open_with_relations():
            if fu.due_date < start_date or fu.due_date > end_date:
                continue
            all_events.append(
                CalendarEvent(
                    date=fu.due_date,
                    followUpId=fu.id,
                    customerId=fu.customer_id,
                    customer=fu.customer.name,
                    productId=fu.product_id,
                    productName=fu.product.name,
                    type="overdue" if fu.due_date < today else "upcoming",
                )
            )
```

- [ ] **Step 5: Update the existing tests that fed cycles directly**

`tests/test_business_today_everywhere.py` — agregar debajo de `_cycle`:

```python
def _follow_up(days_until):
    c = _cycle(days_until)
    return SimpleNamespace(
        id=uuid.uuid4(),
        customer_id=c.customer_id,
        product_id=c.product_id,
        customer=c.customer,
        product=c.product,
        due_date=c.estimated_next_purchase,
    )


def _service_with(follow_ups):
    service = SaleService(db=None)
    service.follow_up_repo.get_open_with_relations = lambda: follow_ups
    service.cycle_repo.get_all_for_follow_ups = lambda: []
    return service
```

y en los tres tests de follow-ups/métricas/calendario reemplazar la construcción del servicio:
- `test_follow_ups_count_days_from_the_business_day`: `service = _service_with([_follow_up(3)])` (borrar las dos líneas `SaleService(db=None)` / `get_all_for_follow_ups`).
- `test_follow_up_metrics_count_days_from_the_business_day`: `service = _service_with([_follow_up(3)])`.
- `test_calendar_marks_overdue_against_the_business_day`: `service = _service_with([_follow_up(-1), _follow_up(0)])`.

`tests/test_follow_up_metrics.py` — en `_cycle`, renombrar el campo `estimated_next_purchase=` a `due_date=`; en `_service`, reemplazar `service.cycle_repo.get_all_with_estimation = lambda: cycles` por `service.follow_up_repo.get_open_with_relations = lambda: cycles`.

`tests/test_sales_projection.py::test_a_fractional_ewma_interval_flows_through_follow_ups_without_raising` — después de `session.add(CustomerProductCycle(...))` agregar:

```python
        from app.models.follow_up import FollowUp

        session.add(
            FollowUp(
                customer_id=customer.id,
                product_id=product.id,
                due_date=_date.today() + _timedelta(days=5),
                status="open",
            )
        )
```

`tests/test_agent_read_tools.py::test_consultar_seguimiento_reports_real_status_and_days_until` — antes de `db_session.add_all([urgente, vencido])` agregar (y `from app.models.follow_up import FollowUp` en los imports del archivo):

```python
    seguimientos = [
        FollowUp(
            customer_id=c.customer_id,
            product_id=c.product_id,
            due_date=c.estimated_next_purchase,
            status="open",
        )
        for c in (urgente, vencido)
    ]
```

y cambiar `db_session.add_all([urgente, vencido])` por `db_session.add_all([urgente, vencido, *seguimientos])`.

- [ ] **Step 6: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: PASS (toda la suite)

- [ ] **Step 7: Commit**

```bash
git add app/schemas/sale.py app/services/sales/orchestrator.py tests/test_follow_up_reads.py \
  tests/test_business_today_everywhere.py tests/test_follow_up_metrics.py \
  tests/test_sales_projection.py tests/test_agent_read_tools.py
git commit -m "feat: Read priority customers, metrics and calendar from open follow-ups"
```

---

### Task 5: Endpoints `postpone` y `dismiss`

**Files:**
- Create: `app/schemas/follow_up.py`
- Modify: `app/api/dependencies.py`, `app/api/v1/endpoints/follow_ups.py`
- Test: `tests/test_follow_up_endpoints.py` (nuevo)

**Interfaces:**
- Consumes: `FollowUpService.postpone`, `FollowUpService.dismiss` (Task 3); `CurrentUser` (`app/api/dependencies.py`).
- Produces: `POST /api/v1/follow-ups/{id}/postpone` → 200 `FollowUpOut`; `POST /api/v1/follow-ups/{id}/dismiss` → 204. `FollowUpServiceDep`.

- [ ] **Step 1: Write the failing tests**

`tests/test_follow_up_endpoints.py`:

```python
from datetime import timedelta

from app.core.datetime_utils import business_today
from app.models.follow_up import FollowUp
from tests.fixtures_http import _auth


def _open_follow_up(db, customer, product):
    follow_up = FollowUp(
        customer_id=customer.id,
        product_id=product.id,
        due_date=business_today() + timedelta(days=2),
        status="open",
    )
    db.add(follow_up)
    db.commit()
    return follow_up


def test_postpone_returns_the_new_follow_up(
    client, db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    follow_up = _open_follow_up(db_session, seeded_customer, seeded_product_with_lot)
    new_date = business_today() + timedelta(days=9)

    resp = client.post(
        f"/api/v1/follow-ups/{follow_up.id}/postpone",
        json={"date": new_date.isoformat()},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["due_date"] == new_date.isoformat()
    assert body["status"] == "open"
    assert body["id"] != str(follow_up.id)


def test_postpone_to_today_is_unprocessable(
    client, db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    follow_up = _open_follow_up(db_session, seeded_customer, seeded_product_with_lot)

    resp = client.post(
        f"/api/v1/follow-ups/{follow_up.id}/postpone",
        json={"date": business_today().isoformat()},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 422


def test_dismiss_twice_is_a_conflict(
    client, db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    follow_up = _open_follow_up(db_session, seeded_customer, seeded_product_with_lot)
    url = f"/api/v1/follow-ups/{follow_up.id}/dismiss"

    assert client.post(url, headers=_auth(seeded_user)).status_code == 204
    assert client.post(url, headers=_auth(seeded_user)).status_code == 409


def test_actions_require_authentication(client, db_session, seeded_customer, seeded_product_with_lot):
    follow_up = _open_follow_up(db_session, seeded_customer, seeded_product_with_lot)

    resp = client.post(f"/api/v1/follow-ups/{follow_up.id}/dismiss")

    assert resp.status_code in (401, 403)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_follow_up_endpoints.py -v`
Expected: FAIL — 404/405 en las rutas nuevas.

- [ ] **Step 3: Write the schemas**

`app/schemas/follow_up.py`:

```python
import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict


class FollowUpPostpone(BaseModel):
    date: dt.date


class FollowUpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    product_id: uuid.UUID
    due_date: dt.date
    status: str
```

- [ ] **Step 4: Add the dependency and the routes**

En `app/api/dependencies.py`: importar `from app.services.follow_up_service import FollowUpService`, y agregar junto a las otras fábricas:

```python
def get_follow_up_service(db: Session = Depends(get_db)) -> FollowUpService:
    """Dependency that builds a FollowUpService on the request's session."""
    return FollowUpService(db)
```

y junto a los demás alias:

```python
FollowUpServiceDep = Annotated[FollowUpService, Depends(get_follow_up_service)]
```

En `app/api/v1/endpoints/follow_ups.py`, imports:

```python
import uuid

from fastapi import APIRouter, Depends, status

from app.api.dependencies import (
    CurrentUser,
    FollowUpServiceDep,
    SaleServiceDep,
    get_current_user,
)
from app.schemas.follow_up import FollowUpOut, FollowUpPostpone
from app.schemas.sale import FollowUpMetrics, PaginatedFollowUpResponse
```

y al final del archivo:

```python
@router.post("/{follow_up_id}/postpone", response_model=FollowUpOut)
def postpone_follow_up(
    follow_up_id: uuid.UUID,
    body: FollowUpPostpone,
    service: FollowUpServiceDep,
    current_user: CurrentUser,
):
    """Cierra el seguimiento y abre otro en `date` (debe ser posterior a hoy)."""
    return service.postpone(follow_up_id, body.date, current_user.id)


@router.post("/{follow_up_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_follow_up(
    follow_up_id: uuid.UUID,
    service: FollowUpServiceDep,
    current_user: CurrentUser,
):
    """Cierra el seguimiento sin abrir otro hasta la proxima venta del producto."""
    service.dismiss(follow_up_id, current_user.id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_follow_up_endpoints.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/schemas/follow_up.py app/api/dependencies.py app/api/v1/endpoints/follow_ups.py \
  tests/test_follow_up_endpoints.py
git commit -m "feat: Add endpoints to postpone and dismiss a follow-up"
```

---

### Task 6: Cliente de Google Calendar y pasada de sincronización

**Files:**
- Create: `app/services/google_calendar.py`
- Modify: `app/services/calendar_sync.py`
- Test: `tests/test_google_calendar_client.py`, `tests/test_calendar_sync.py` (nuevos)

**Interfaces:**
- Consumes: `FollowUpRepository.get_to_sync()` (Task 3), `CustomerProductCycleRepository.get_by_customer_and_product`.
- Produces:
  - `app.services.google_calendar`: `CalendarError(Exception)`, `GoogleCalendarClient(calendar_id: str, session)` con `insert(event: dict) -> bool` (False si ya existía), `patch(event_id: str, fields: dict) -> None`, `delete(event_id: str) -> None`, y `GoogleCalendarClient.from_settings() -> GoogleCalendarClient`.
  - `app.services.calendar_sync`: `build_event(follow_up, cycle) -> dict`, `sync_pending_follow_ups(db, client) -> int` (filas que quedaron `failed`).

- [ ] **Step 1: Write the failing client tests**

`tests/test_google_calendar_client.py`:

```python
from types import SimpleNamespace

import pytest

from app.services.google_calendar import CalendarError, GoogleCalendarClient


class FakeSession:
    def __init__(self, status_code):
        self.status_code = status_code
        self.calls = []

    def _respond(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return SimpleNamespace(
            status_code=self.status_code,
            ok=200 <= self.status_code < 300,
            text="boom",
        )

    def post(self, url, **kw):
        return self._respond("POST", url, **kw)

    def patch(self, url, **kw):
        return self._respond("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._respond("DELETE", url, **kw)


def _client(status_code):
    session = FakeSession(status_code)
    return GoogleCalendarClient("cal@group.calendar.google.com", session), session


def test_insert_reports_an_existing_event_instead_of_failing():
    client, _ = _client(409)
    assert client.insert({"id": "abc"}) is False


def test_insert_creates_with_a_timeout_and_an_escaped_calendar_id():
    client, session = _client(200)
    assert client.insert({"id": "abc"}) is True
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/calendars/cal%40group.calendar.google.com/events")
    assert kwargs["timeout"] == 5


@pytest.mark.parametrize("status_code", [404, 410])
def test_deleting_a_missing_event_is_success(status_code):
    client, _ = _client(status_code)
    client.delete("abc")


def test_other_errors_raise_with_the_status():
    client, _ = _client(500)
    with pytest.raises(CalendarError, match="500"):
        client.patch("abc", {"summary": "x"})
```

- [ ] **Step 2: Write the failing sync tests**

`tests/test_calendar_sync.py`:

```python
"""La pasada de sincronizacion lleva cada seguimiento `pending`/`failed` a
Google segun su estado, y deja la fila `synced` o `failed`."""

from datetime import timedelta

from sqlalchemy import update

from app.core.datetime_utils import business_today
from app.models.follow_up import FollowUp
from app.services.calendar_sync import sync_pending_follow_ups
from app.services.google_calendar import CalendarError


class FakeCalendar:
    def __init__(self, exists=False, fail=None, on_insert=None):
        self.exists = exists
        self.fail = fail
        self.on_insert = on_insert
        self.calls = []

    def insert(self, event):
        self.calls.append(("insert", event["id"], event))
        if self.on_insert:
            self.on_insert()
        if self.fail:
            raise CalendarError(self.fail)
        return not self.exists

    def patch(self, event_id, fields):
        self.calls.append(("patch", event_id, fields))

    def delete(self, event_id):
        self.calls.append(("delete", event_id, None))
        if self.fail:
            raise CalendarError(self.fail)


def _follow_up(db, customer, product, **fields):
    values = {
        "due_date": business_today() + timedelta(days=3),
        "status": "open",
        "sync_status": "pending",
        **fields,
    }
    follow_up = FollowUp(customer_id=customer.id, product_id=product.id, **values)
    db.add(follow_up)
    db.commit()
    return follow_up


def test_an_open_follow_up_is_inserted_as_an_all_day_event(
    db_session, seeded_customer, seeded_product_with_lot
):
    follow_up = _follow_up(db_session, seeded_customer, seeded_product_with_lot)
    calendar = FakeCalendar()

    assert sync_pending_follow_ups(db_session, calendar) == 0

    [(op, event_id, event)] = calendar.calls
    assert (op, event_id) == ("insert", follow_up.id.hex)
    assert event["start"] == {"date": follow_up.due_date.isoformat()}
    assert event["end"] == {"date": (follow_up.due_date + timedelta(days=1)).isoformat()}
    assert event["summary"] == (
        f"Seguimiento: {seeded_customer.name} · {seeded_product_with_lot.name}"
    )
    db_session.refresh(follow_up)
    assert (follow_up.sync_status, follow_up.sync_error) == ("synced", None)


def test_an_existing_event_is_patched_back_to_confirmed(
    db_session, seeded_customer, seeded_product_with_lot
):
    """Si alguien lo borro a mano en Google, el id queda cancelado: 409 al
    insertar, y el patch con status confirmed lo restaura."""
    follow_up = _follow_up(db_session, seeded_customer, seeded_product_with_lot)
    calendar = FakeCalendar(exists=True)

    sync_pending_follow_ups(db_session, calendar)

    assert [c[0] for c in calendar.calls] == ["insert", "patch"]
    patch_fields = calendar.calls[1][2]
    assert patch_fields["status"] == "confirmed"
    assert patch_fields["start"] == {"date": follow_up.due_date.isoformat()}


def test_a_closed_follow_up_is_deleted(db_session, seeded_customer, seeded_product_with_lot):
    from datetime import datetime, timezone

    follow_up = _follow_up(
        db_session,
        seeded_customer,
        seeded_product_with_lot,
        status="closed",
        close_reason="dismissed",
        closed_at=datetime.now(timezone.utc),
    )
    calendar = FakeCalendar()

    sync_pending_follow_ups(db_session, calendar)

    assert calendar.calls == [("delete", follow_up.id.hex, None)]
    db_session.refresh(follow_up)
    assert follow_up.sync_status == "synced"


def test_a_failure_is_recorded_and_retried(db_session, seeded_customer, seeded_product_with_lot):
    follow_up = _follow_up(db_session, seeded_customer, seeded_product_with_lot)

    assert sync_pending_follow_ups(db_session, FakeCalendar(fail="403: forbidden")) == 1
    db_session.refresh(follow_up)
    assert follow_up.sync_status == "failed"
    assert "403" in follow_up.sync_error

    assert sync_pending_follow_ups(db_session, FakeCalendar()) == 0
    db_session.refresh(follow_up)
    assert (follow_up.sync_status, follow_up.sync_error) == ("synced", None)


def test_synced_and_skipped_rows_are_not_sent(db_session, seeded_customer, seeded_product_with_lot, seeded_product_with_one_lot):
    _follow_up(db_session, seeded_customer, seeded_product_with_lot, sync_status="synced")
    _follow_up(db_session, seeded_customer, seeded_product_with_one_lot, sync_status="skipped")
    calendar = FakeCalendar()

    sync_pending_follow_ups(db_session, calendar)

    assert calendar.calls == []


def test_a_follow_up_closed_mid_sync_stays_pending(
    db_session, seeded_customer, seeded_product_with_lot
):
    """Se cierra mientras se insertaba su evento: marcarlo `synced` dejaria el
    evento vivo en Google para siempre."""
    from datetime import datetime, timezone

    follow_up = _follow_up(db_session, seeded_customer, seeded_product_with_lot)

    def close_it():
        db_session.execute(
            update(FollowUp)
            .where(FollowUp.id == follow_up.id)
            .values(
                status="closed",
                close_reason="dismissed",
                closed_at=datetime.now(timezone.utc),
                sync_status="pending",
            )
        )

    sync_pending_follow_ups(db_session, FakeCalendar(on_insert=close_it))

    db_session.expire_all()
    assert db_session.get(FollowUp, follow_up.id).sync_status == "pending"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_google_calendar_client.py tests/test_calendar_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.google_calendar'`

- [ ] **Step 4: Write the client**

`app/services/google_calendar.py`:

```python
"""Cliente minimo de la API REST de Google Calendar v3.

Tres llamadas, con `AuthorizedSession` de google-auth: no hace falta
google-api-python-client. Los ids de evento los pone quien llama (el id del
seguimiento), lo que hace idempotente cada operacion.
"""

from urllib.parse import quote

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from app.core.config import settings

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TIMEOUT_SECONDS = 5


class CalendarError(Exception):
    pass


class GoogleCalendarClient:
    def __init__(self, calendar_id: str, session):
        self.calendar_id = calendar_id
        self.session = session

    @classmethod
    def from_settings(cls) -> "GoogleCalendarClient":
        """La misma cuenta de servicio que usa Firebase."""
        if settings.FIREBASE_CREDENTIALS_PATH:
            creds = service_account.Credentials.from_service_account_file(
                settings.FIREBASE_CREDENTIALS_PATH, scopes=SCOPES
            )
        else:
            creds, _project = google.auth.default(scopes=SCOPES)
        return cls(settings.GOOGLE_CALENDAR_ID, AuthorizedSession(creds))

    def _url(self, event_id: str | None = None) -> str:
        base = f"{CALENDAR_API}/calendars/{quote(self.calendar_id, safe='')}/events"
        return f"{base}/{event_id}" if event_id else base

    @staticmethod
    def _raise_for(response) -> None:
        if not response.ok:
            raise CalendarError(f"{response.status_code}: {response.text[:500]}")

    def insert(self, event: dict) -> bool:
        """True si lo creo; False si ese id ya existia (409)."""
        response = self.session.post(self._url(), json=event, timeout=TIMEOUT_SECONDS)
        if response.status_code == 409:
            return False
        self._raise_for(response)
        return True

    def patch(self, event_id: str, fields: dict) -> None:
        response = self.session.patch(
            self._url(event_id), json=fields, timeout=TIMEOUT_SECONDS
        )
        self._raise_for(response)

    def delete(self, event_id: str) -> None:
        response = self.session.delete(self._url(event_id), timeout=TIMEOUT_SECONDS)
        if response.status_code in (404, 410):
            return
        self._raise_for(response)
```

- [ ] **Step 5: Write the sync pass**

Agregar a `app/services/calendar_sync.py` (los imports arriba, junto a `import threading`):

```python
import logging
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.follow_up import OPEN, SYNC_FAILED, SYNC_SYNCED, FollowUp
from app.repositories.customer_product_cycle_repository import (
    CustomerProductCycleRepository,
)
from app.repositories.follow_up_repository import FollowUpRepository

logger = logging.getLogger(__name__)

EVENT_FIELDS = ("summary", "description", "start", "end", "status")


def build_event(follow_up: FollowUp, cycle) -> dict:
    lines = []
    if cycle is not None:
        lines.append(
            f"Ultima compra: {cycle.last_purchase_date:%d/%m/%Y} "
            f"({cycle.last_quantity.normalize()} u.)"
        )
        if cycle.avg_interval_days is not None:
            lines.append(f"Intervalo promedio: {cycle.avg_interval_days:.1f} dias")
    if follow_up.customer.email:
        lines.append(f"Email: {follow_up.customer.email}")
    return {
        "id": follow_up.id.hex,
        "summary": f"Seguimiento: {follow_up.customer.name} · {follow_up.product.name}",
        "description": "\n".join(lines),
        "start": {"date": follow_up.due_date.isoformat()},
        "end": {"date": (follow_up.due_date + timedelta(days=1)).isoformat()},
        # Restaura un evento que alguien borro a mano en Google (queda
        # `cancelled` con el mismo id y el insert responde 409).
        "status": "confirmed",
    }


def sync_pending_follow_ups(db: Session, client) -> int:
    """Lleva a Google cada seguimiento `pending`/`failed`.

    Devuelve cuantas filas quedaron en `failed`. Cada fila se confirma por
    separado: un error de Google no deshace lo que ya se sincronizo.
    """
    cycles = CustomerProductCycleRepository(db)
    failed = 0
    for follow_up in FollowUpRepository(db).get_to_sync():
        seen_status = follow_up.status
        try:
            if seen_status == OPEN:
                event = build_event(
                    follow_up,
                    cycles.get_by_customer_and_product(
                        follow_up.customer_id, follow_up.product_id
                    ),
                )
                if not client.insert(event):
                    client.patch(event["id"], {k: event[k] for k in EVENT_FIELDS})
            else:
                client.delete(follow_up.id.hex)
            values = {"sync_status": SYNC_SYNCED, "sync_error": None}
        except Exception as exc:  # CalendarError, timeouts de requests
            logger.warning("No se pudo sincronizar el seguimiento %s: %s", follow_up.id, exc)
            values = {"sync_status": SYNC_FAILED, "sync_error": str(exc)[:1000]}
            failed += 1
        # Solo si el estado no cambio mientras se hablaba con Google: si lo
        # cerraron entretanto, la fila sigue `pending` y su escritor ya pidio
        # otra pasada, que borrara el evento.
        db.execute(
            update(FollowUp)
            .where(FollowUp.id == follow_up.id, FollowUp.status == seen_status)
            .values(**values)
        )
        db.commit()
    return failed
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_google_calendar_client.py tests/test_calendar_sync.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/services/google_calendar.py app/services/calendar_sync.py \
  tests/test_google_calendar_client.py tests/test_calendar_sync.py
git commit -m "feat: Mirror follow-ups to Google Calendar with idempotent event ids"
```

---

### Task 7: Hilo que despierta sólo cuando hay trabajo, y configuración

**Files:**
- Modify: `app/services/calendar_sync.py`, `app/core/config.py`, `app/main.py`, `requirements.txt`, `.env.example`
- Test: `tests/test_calendar_sync_worker.py` (nuevo)

**Interfaces:**
- Consumes: `sync_pending_follow_ups`, `_wake`, `request_calendar_sync` (Tasks 3 y 6), `GoogleCalendarClient.from_settings()` (Task 6).
- Produces: `RETRY_SECONDS = 900`, `next_wait_timeout(failed: int) -> Optional[float]`, `sync_pass(session_factory, client) -> int`, `start_calendar_sync(session_factory, client) -> threading.Thread`; `settings.GOOGLE_CALENDAR_ID: Optional[str]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_calendar_sync_worker.py`:

```python
from app.services import calendar_sync


def test_without_failures_the_worker_sleeps_until_woken():
    assert calendar_sync.next_wait_timeout(0) is None


def test_with_failures_the_worker_retries_every_15_minutes():
    assert calendar_sync.next_wait_timeout(2) == 900


def test_a_crashing_pass_counts_as_a_failure_so_it_is_retried():
    def broken_session_factory():
        raise RuntimeError("db down")

    assert calendar_sync.sync_pass(broken_session_factory, client=None) == 1


def test_request_calendar_sync_sets_the_signal():
    calendar_sync._wake.clear()
    calendar_sync.request_calendar_sync()
    assert calendar_sync._wake.is_set()
    calendar_sync._wake.clear()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_calendar_sync_worker.py -v`
Expected: FAIL — `AttributeError: module 'app.services.calendar_sync' has no attribute 'next_wait_timeout'`

- [ ] **Step 3: Write the worker**

Agregar al final de `app/services/calendar_sync.py` (y `from typing import Optional` en los imports):

```python
RETRY_SECONDS = 900


def next_wait_timeout(failed: int) -> Optional[float]:
    """Sin fallas, dormir hasta que alguien pida una pasada; con fallas,
    reintentar cada 15 minutos."""
    return RETRY_SECONDS if failed else None


def sync_pass(session_factory, client) -> int:
    """Una pasada completa. Un error inesperado (p. ej. la base caida) cuenta
    como falla, para que se reintente en vez de dormir para siempre."""
    try:
        with session_factory() as db:
            return sync_pending_follow_ups(db, client)
    except Exception:
        logger.exception("Fallo la pasada de sincronizacion con Google Calendar")
        return 1


def _run(session_factory, client) -> None:
    while True:
        # Limpiar ANTES de la pasada: un pedido que llega durante ella deja la
        # senal encendida y el `wait` de abajo vuelve enseguida.
        _wake.clear()
        failed = sync_pass(session_factory, client)
        _wake.wait(timeout=next_wait_timeout(failed))


def start_calendar_sync(session_factory, client) -> threading.Thread:
    """Arranca el hilo. Su primera pasada cubre la carga inicial y lo que
    haya quedado pendiente de un reinicio."""
    thread = threading.Thread(
        target=_run, args=(session_factory, client), name="calendar-sync", daemon=True
    )
    thread.start()
    return thread
```

Actualizar el docstring del módulo: quitar "(mas abajo, Task 7)".

- [ ] **Step 4: Configuration and startup**

`app/core/config.py`, debajo de `FIREBASE_CREDENTIALS_PATH`:

```python
    # Calendario del negocio donde se reflejan los seguimientos abiertos. Sin
    # el, el espejo no arranca y los seguimientos se quedan `pending` en la base.
    GOOGLE_CALENDAR_ID: Optional[str] = None
```

`app/main.py`: cambiar `from app.core.database import engine` por `from app.core.database import SessionLocal, engine`, agregar

```python
from app.services.calendar_sync import start_calendar_sync
from app.services.google_calendar import GoogleCalendarClient
```

y en `lifespan`, justo después del bloque `try: initialize_firebase() ...`:

```python
    # Mismo patron que Firebase: que el espejo de Google no arranque no debe
    # tumbar las ventas; los seguimientos se quedan `pending` hasta el proximo
    # arranque que si pueda.
    if settings.GOOGLE_CALENDAR_ID:
        try:
            start_calendar_sync(SessionLocal, GoogleCalendarClient.from_settings())
        except Exception:
            logger.exception("Google Calendar sync failed to start")
            startup_issues.append("calendar_sync_init_failed")
    else:
        logger.info(
            "GOOGLE_CALENDAR_ID no definido: los seguimientos no se reflejan en Google Calendar"
        )
```

`requirements.txt`, debajo de `firebase-admin==6.7.0`:

```
google-auth==2.60.0
requests==2.32.5
```

`.env.example`, debajo de `FIREBASE_CREDENTIALS_PATH=...`:

```
# Id del calendario de seguimientos (compartido con la cuenta de servicio de
# FIREBASE_CREDENTIALS_PATH, permiso "Hacer cambios en eventos"). Vacio = sin espejo.
GOOGLE_CALENDAR_ID=
```

- [ ] **Step 5: Run the worker tests and the full suite**

Run: `venv/bin/python -m pytest tests/test_calendar_sync_worker.py -v`
Expected: PASS

Run: `venv/bin/python -m pytest -q`
Expected: PASS. Los tests no definen `GOOGLE_CALENDAR_ID`, así que el lifespan no arranca el hilo.

- [ ] **Step 6: Commit**

```bash
git add app/services/calendar_sync.py app/core/config.py app/main.py requirements.txt \
  .env.example tests/test_calendar_sync_worker.py
git commit -m "feat: Wake the calendar sync only when follow-ups change"
```

---

### Task 8: Contrato de API y tipos del front

**Files:**
- Modify: `API_CONTRACT.md` (sección "6. Follow-up Module" y los modelos `FollowUp`), `types.ts` (sección Follow-ups y Calendar)

**Interfaces:**
- Consumes: los esquemas de Tasks 4 y 5.

- [ ] **Step 1: Update `API_CONTRACT.md`**

En "## 6. Follow-up Module", reemplazar la línea
`Follow-ups are computed from \`CustomerProductCycle\` records. Each follow-up groups all tracked products for a given customer.`
por:

```markdown
Follow-ups are stored rows (`follow_ups`), one per customer + product, opened from the purchase projection and closed by a newer sale of that product or by hand (postpone / dismiss). Lists only show **open** follow-ups, grouped by customer. Customers with no projection at all appear as `needs_estimate` in the `all` filter only.
```

y agregar al final de la sección:

```markdown
### `POST /follow-ups/{id}/postpone`

- **Body:** `{ "date": "YYYY-MM-DD" }` — must be after today (business timezone).
- **Response 200:** `{ "id", "customer_id", "product_id", "due_date", "status": "open" }` — the new follow-up.
- **Errors:** `404` unknown id · `409` already closed · `422` date not in the future.

### `POST /follow-ups/{id}/dismiss`

- Closes the follow-up; no new one opens until the next sale of that product.
- **Response:** `204`. **Errors:** `404` unknown id · `409` already closed.
```

En el modelo `FollowUpItem` del contrato agregar los campos `follow_up_id` (UUID | null, null only for `needs_estimate`) y `due_date` (YYYY-MM-DD | null; **show this as "Próxima compra"**). En `CalendarEvent` agregar `followUpId` (UUID).

- [ ] **Step 2: Update `types.ts`**

En `FollowUpItem`, agregar como primeros campos:

```ts
  follow_up_id: string | null; // UUID; null only for needs_estimate
  due_date: string | null; // YYYY-MM-DD — show this as "Próxima compra"
```

Cambiar `FollowUpStatus` a:

```ts
export type FollowUpStatus = "overdue" | "urgent" | "upcoming" | "normal" | "needs_estimate";
```

Agregar debajo de `FollowUpFilter`:

```ts
export interface FollowUpPostponeRequest {
  date: string; // YYYY-MM-DD, after today
}

export interface FollowUpRecord {
  id: string; // UUID
  customer_id: string; // UUID
  product_id: string; // UUID
  due_date: string; // YYYY-MM-DD
  status: "open" | "closed";
}
```

En la interfaz del evento de calendario (sección `// Calendar`) agregar `followUpId: string; // UUID`.

- [ ] **Step 3: Commit**

```bash
git add API_CONTRACT.md types.ts
git commit -m "docs: Document follow-up actions and fields in the API contract"
```
