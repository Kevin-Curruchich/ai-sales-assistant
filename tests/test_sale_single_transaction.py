"""Una venta es una sola transaccion.

`_update_cycle` commiteaba a mitad de `SaleService.create` (los repos del
ciclo hacian commit), asi que la venta, su entrada de caja y el descuento de
stock quedaban guardados antes de escribir los seguimientos. Si algo fallaba
despues, la API respondia 500 por una venta que ya existia, y un reintento la
duplicaba.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.models.cash_movement import CashMovement
from app.models.customer_product_cycle import CustomerProductCycle
from app.models.product import Product
from app.models.sale import Sale
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.sales import SaleService


def _sell(service, customer, product, user, day):
    return service.create(
        SaleCreate(
            customerId=customer.id,
            date=day,
            items=[SaleItemCreate(productId=product.id, quantity=Decimal("1"))],
        ),
        user_id=user.id,
    )


def test_a_failure_after_the_cycle_update_leaves_nothing_written(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    _sell(
        service,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 10),
    )
    before = {
        "sales": db_session.query(Sale).count(),
        "cash": db_session.query(CashMovement).count(),
        "stock": db_session.get(Product, seeded_product_with_lot.id).stock,
        "last": db_session.query(CustomerProductCycle).one().last_purchase_date,
    }

    def boom(*_args, **_kwargs):
        raise RuntimeError("fallo al escribir el seguimiento")

    service.follow_up_service.record_purchase = boom
    with pytest.raises(RuntimeError):
        _sell(
            service,
            seeded_customer,
            seeded_product_with_lot,
            seeded_user,
            date(2026, 9, 17),
        )
    db_session.rollback()

    assert db_session.query(Sale).count() == before["sales"]
    assert db_session.query(CashMovement).count() == before["cash"]
    db_session.expire_all()
    assert db_session.get(Product, seeded_product_with_lot.id).stock == before["stock"]
    assert (
        db_session.query(CustomerProductCycle).one().last_purchase_date
        == before["last"]
    )
