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
            text("SELECT product_id, due_date, status, sync_status FROM follow_ups")
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
