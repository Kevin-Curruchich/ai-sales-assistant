"""Una venta pagada mueve la caja; una pendiente no."""

from datetime import date
from decimal import Decimal

import pytest

from app.models.cash_movement import CashMovement, CashMovementType
from app.models.sale import Sale
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.sales import SaleService


def _sale(customer, product, pending=False):
    return SaleCreate(
        customerId=customer.id,
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=product.id, quantity=Decimal("1"))],
        isPaymentPending=pending,
    )


def test_a_paid_sale_records_one_cash_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(
        _sale(seeded_customer, seeded_product_with_lot), user_id=seeded_user.id
    )

    movements = db_session.query(CashMovement).filter_by(sale_id=sale.id).all()
    assert len(movements) == 1
    assert movements[0].type == CashMovementType.ENTRADA
    assert movements[0].amount == sale.total


def test_a_pending_sale_records_no_cash_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(
        _sale(seeded_customer, seeded_product_with_lot, pending=True),
        user_id=seeded_user.id,
    )
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0


def test_the_cash_entry_shares_the_sale_transaction(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """Si la caja falla, la venta no queda escrita a medias."""
    service = SaleService(db_session)

    def explode(*args, **kwargs):
        raise RuntimeError("caja caida")

    monkeypatch.setattr(service.cash_service, "record", explode)

    with pytest.raises(RuntimeError):
        service.create(
            _sale(seeded_customer, seeded_product_with_lot), user_id=seeded_user.id
        )

    db_session.rollback()
    assert db_session.query(Sale).count() == 0
