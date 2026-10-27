"""Fixtures de dominio compartidos: sesion contra un schema migrado desechable,
mas datos sembrados (usuario, cliente, productos con lotes FIFO confirmados,
compra en borrador).

Las Tasks 4, 5, 7 y 8 dependen de estos nombres y de lo que devuelven -- no
son un detalle de implementacion de esta task.  Siguen el patron de
`_session_for` en `tests/test_cash_service.py`: una engine propia por fixture,
con un listener de `connect` que fija el `search_path` al schema desechable.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.core.database import bind_business
from app.models.business import Business
from app.models.customer import Customer
from app.models.product import Product
from app.models.purchase import Purchase, PurchaseItem
from app.models.user import User
from tests.conftest import TEST_DATABASE_URL

MONEY = Decimal("0.01")

# Para conexiones crudas (sin Session): fija el comercio que crea la migracion
# solo para la transaccion en curso.
FIRST_BUSINESS_SQL = text(
    "SELECT set_config('app.business_id', (SELECT id::text FROM businesses), true)"
)


def bind_first_business(session) -> None:
    """Liga la sesion al unico comercio del schema, el que crea la migracion.

    Para tests que arman su propia sesion sin pasar por `db_session`.
    """
    row = session.execute(text("SELECT id, timezone FROM businesses")).one()
    bind_business(session, row.id, row.timezone)


def _session_for(schema: str, role: str | None = None):
    engine = create_engine(TEST_DATABASE_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        if role:
            cursor.execute(f'SET ROLE "{role}"')
        cursor.close()
        dbapi_connection.commit()

    return sessionmaker(bind=engine)(), engine


@pytest.fixture
def db_session(migrated_schema, monkeypatch):
    """Sesion contra un schema migrado desechable.

    Tambien redirige `agent_session()` (Task 7) al mismo engine y schema: las
    herramientas del agente abren su propia sesion via
    `app.core.database.SessionLocal`, que por defecto apunta a la base de
    desarrollo local (.env, puerto 55433) y nunca veria los datos sembrados
    aca (puerto 55432, schema desechable). El monkeypatch reemplaza esa
    referencia, no `app/agent/session.py`: el modulo de produccion queda
    intacto.
    """
    session, engine = _session_for(migrated_schema)
    business = Business(name="Comercio de prueba")
    session.add(business)
    session.commit()
    bind_business(session, business.id, business.timezone)
    # Sin `info` heredado: `agent_session(config)` liga el comercio del config.
    monkeypatch.setattr("app.agent.session.SessionLocal", sessionmaker(bind=engine))
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def seeded_business(db_session):
    """El comercio al que esta ligada `db_session`."""
    return db_session.get(Business, db_session.info["business_id"])


@pytest.fixture
def other_business(db_session):
    """Un segundo comercio, sin ligar a ninguna sesion."""
    business = Business(name="Otro comercio", timezone="America/Mexico_City")
    db_session.add(business)
    db_session.commit()
    return business


@pytest.fixture
def tenant_session(migrated_schema):
    """Sesion sobre el mismo schema pero como `app_tenant`, sujeta a RLS.

    `db_session` corre como superusuario y se salta RLS: con ella sola los
    tests de aislamiento pasarian siempre.
    """
    session, engine = _session_for(migrated_schema, role="app_tenant")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def two_similar_customers(db_session):
    """Dos clientes con nombres parecidos, para probar que `buscar_cliente`
    devuelve ambos sin elegir por el usuario."""
    a = Customer(name="Juan Gonzalez")
    b = Customer(name="Juana Gonzalez")
    db_session.add_all([a, b])
    db_session.commit()
    db_session.refresh(a)
    db_session.refresh(b)
    return (a, b)


@pytest.fixture
def seeded_user(db_session):
    user = User(
        email="vendedor@example.com", display_name="Vendedor de prueba", role="admin"
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def second_user(db_session):
    """Un segundo usuario, distinto de `seeded_user`.  Sin este, "solo el
    dueno puede..." no se puede probar: hace falta a alguien mas."""
    user = User(
        email="otro-vendedor@example.com", display_name="Otro vendedor", role="admin"
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def user_of_other_business(db_session, other_business):
    """Un usuario de `other_business`: para probar que el agente no cruza comercios."""
    user = User(
        email="ajeno@example.com",
        display_name="Vendedor ajeno",
        role="admin",
        business_id=other_business.id,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def business_configurable(business_id, timezone: str) -> dict:
    """Las claves de comercio que `/agent/stream` pone en `configurable`."""
    return {"business_id": str(business_id), "business_tz": timezone}


def agent_config(user) -> dict:
    """El `config` de una corrida del agente para `user`, como lo arma el endpoint."""
    return {
        "configurable": {
            "user_id": str(user.id),
            **business_configurable(user.business_id, user.business.timezone),
        }
    }


@pytest.fixture
def tool_config(db_session):
    """`config` minimo para invocar una herramienta de lectura sin usuario."""
    return {
        "configurable": business_configurable(
            db_session.info["business_id"], db_session.info["business_tz"]
        )
    }


@pytest.fixture
def seeded_customer(db_session):
    customer = Customer(name="Cliente de prueba")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    return customer


def _seed_product_with_one_confirmed_lot(
    db_session,
    user: User,
    *,
    quantity: Decimal,
    unit_cost: Decimal,
    purchase_date: date,
) -> Product:
    """Crea un producto con exactamente un lote FIFO disponible (compra confirmada)."""
    sku = f"SKU-{uuid.uuid4().hex[:10]}"
    product = Product(sku=sku, name=f"Producto de prueba {sku}", stock=quantity)
    db_session.add(product)
    db_session.flush()

    subtotal = (unit_cost * quantity).quantize(MONEY)
    purchase = Purchase(
        user_id=user.id,
        date=purchase_date,
        total=subtotal,
        status="confirmed",
    )
    db_session.add(purchase)
    db_session.flush()

    item = PurchaseItem(
        purchase_id=purchase.id,
        product_id=product.id,
        quantity=quantity,
        remaining_quantity=quantity,
        unit_cost=unit_cost,
        subtotal=subtotal,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(product)
    return product


@pytest.fixture
def seeded_product_with_lot(db_session, seeded_user):
    """Producto con un lote confirmado de 6 unidades a Q33.33."""
    return _seed_product_with_one_confirmed_lot(
        db_session,
        seeded_user,
        quantity=Decimal("6"),
        unit_cost=Decimal("33.33"),
        purchase_date=date(2026, 9, 1),
    )


@pytest.fixture
def seeded_product_with_one_lot(db_session, seeded_user):
    """Producto con exactamente un lote (3 unidades a Q10.00), para agotarlo
    por completo en los tests de la T8."""
    return _seed_product_with_one_confirmed_lot(
        db_session,
        seeded_user,
        quantity=Decimal("3"),
        unit_cost=Decimal("10.00"),
        purchase_date=date(2026, 9, 1),
    )


@pytest.fixture
def seeded_purchase_draft(db_session, seeded_user):
    """Compra en estado draft con un item, para tests de confirm/cancel/edicion.

    Draft a proposito: `remaining_quantity=0` porque una compra sin confirmar
    todavia no aporta lotes FIFO disponibles (ver
    PurchaseRepository.get_fifo_available_lots, que solo mira status='confirmed').
    """
    sku = f"SKU-{uuid.uuid4().hex[:10]}"
    product = Product(
        sku=sku, name=f"Producto para compra draft {sku}", stock=Decimal("0")
    )
    db_session.add(product)
    db_session.flush()

    quantity = Decimal("4")
    unit_cost = Decimal("15.00")
    subtotal = (unit_cost * quantity).quantize(MONEY)

    purchase = Purchase(
        user_id=seeded_user.id,
        date=date(2026, 9, 20),
        total=subtotal,
        status="draft",
    )
    db_session.add(purchase)
    db_session.flush()

    item = PurchaseItem(
        purchase_id=purchase.id,
        product_id=product.id,
        quantity=quantity,
        remaining_quantity=Decimal("0"),
        unit_cost=unit_cost,
        subtotal=subtotal,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(purchase)
    return purchase
