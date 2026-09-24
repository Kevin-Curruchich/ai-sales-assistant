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
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.customer import Customer
from app.models.product import Product
from app.models.purchase import Purchase, PurchaseItem
from app.models.user import User
from tests.conftest import TEST_DATABASE_URL

MONEY = Decimal("0.01")


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
def db_session(migrated_schema):
    """Sesion contra un schema migrado desechable."""
    session, engine = _session_for(migrated_schema)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def seeded_user(db_session):
    user = User(email="vendedor@example.com", display_name="Vendedor de prueba", role="admin")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def seeded_customer(db_session):
    customer = Customer(name="Cliente de prueba")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    return customer


def _seed_product_with_one_confirmed_lot(
    db_session, user: User, *, quantity: Decimal, unit_cost: Decimal, purchase_date: date
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
    product = Product(sku=sku, name=f"Producto para compra draft {sku}", stock=Decimal("0"))
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
