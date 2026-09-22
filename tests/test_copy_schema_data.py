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
