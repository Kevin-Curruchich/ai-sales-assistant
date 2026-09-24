"""Las columnas de pago existen desde la pieza 1 y nada las escribia."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.payment_method import PaymentMethod
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.sales import SaleService


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


def test_a_created_sale_stores_its_payment_method(
    db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    service = SaleService(db_session)
    data = SaleCreate(
        customerId=seeded_customer.id,
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=seeded_product_with_lot.id, quantity=Decimal("1"))],
        medioPago=PaymentMethod.TRANSFERENCIA,
    )
    # NOTA: el brief de la task escribia `user_id=seeded_customer.id`, que
    # viola la FK sales.user_id -> users.id (un id de Customer no existe en
    # users).  Se usa `seeded_user.id`, que es lo que la FK exige.
    sale = service.create(data, user_id=seeded_user.id)
    db_session.refresh(sale)
    assert sale.payment_method == PaymentMethod.TRANSFERENCIA
    assert sale.payment_date == date(2026, 9, 24)
    assert sale.occurred_at is not None
