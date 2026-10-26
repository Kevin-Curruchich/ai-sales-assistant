"""La migracion de seguimientos crea uno abierto por ciclo con fecha.

Los vencidos al migrar quedan `skipped` para no llenar Google de eventos en
el pasado; los ciclos sin proyeccion no generan nada.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from app.core.datetime_utils import business_today
from app.models import Customer, CustomerProductCycle, Product
from tests.fixtures_domain import _session_for, bind_first_business


def _insert(session, model, **values):
    """INSERT por Core: el modelo ya trae `business_id`, que a esta revision
    todavia no existe, y el Core solo manda las columnas con default en Python."""
    table = model.__table__
    return session.execute(
        insert(table).values(**values).returning(table.c.id)
    ).scalar_one()


def _cycle(session, customer_id, product_id, next_purchase):
    _insert(
        session,
        CustomerProductCycle,
        customer_id=customer_id,
        product_id=product_id,
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
        customer = _insert(session, Customer, name="Aurita")
        future, past, unprojected = (
            _insert(session, Product, sku=f"SKU-{n}", name=f"Producto {n}")
            for n in ("F", "P", "U")
        )
        today = business_today()
        _cycle(session, customer, future, today + timedelta(days=10))
        _cycle(session, customer, past, today - timedelta(days=10))
        _cycle(session, customer, unprojected, None)
        session.commit()

        command.upgrade(alembic_config, "head")

        rows = session.execute(
            text("SELECT product_id, due_date, status, sync_status FROM follow_ups")
        ).all()
        by_product = {r.product_id: r for r in rows}
        assert set(by_product) == {future, past}
        assert by_product[future].due_date == today + timedelta(days=10)
        assert by_product[future].sync_status == "pending"
        assert by_product[past].sync_status == "skipped"
        assert {r.status for r in rows} == {"open"}
    finally:
        session.close()
        engine.dispose()


def test_only_one_open_follow_up_per_pair(migrated_schema):
    session, engine = _session_for(migrated_schema)
    bind_first_business(session)
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
