"""Las columnas de pago existen desde la pieza 1 y nada las escribia."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from app.core.datetime_utils import business_instant
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


def test_occurred_at_stays_unset_in_the_schema():
    data = SaleCreate(
        customerId=uuid.uuid4(),
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=uuid.uuid4(), quantity=Decimal("1"))],
    )
    # El orquestador lo resuelve con la zona del comercio.
    assert data.occurredAt is None


def test_a_created_sale_stores_its_payment_method(
    db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    service = SaleService(db_session)
    data = SaleCreate(
        customerId=seeded_customer.id,
        date=date(2026, 9, 24),
        items=[
            SaleItemCreate(productId=seeded_product_with_lot.id, quantity=Decimal("1"))
        ],
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


def test_service_create_defaults_occurred_at_to_the_business_instant(
    db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    data = SaleCreate(
        customerId=seeded_customer.id,
        date=date(2026, 9, 24),
        items=[
            SaleItemCreate(productId=seeded_product_with_lot.id, quantity=Decimal("1"))
        ],
    )
    sale = SaleService(db_session).create(data, user_id=seeded_user.id)
    db_session.refresh(sale)
    assert sale.occurred_at == business_instant(db_session, date(2026, 9, 24))


def test_service_create_keeps_an_explicit_occurred_at(
    db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    explicit = datetime(2026, 9, 24, 15, 30, tzinfo=timezone.utc)
    data = SaleCreate(
        customerId=seeded_customer.id,
        date=date(2026, 9, 24),
        items=[
            SaleItemCreate(productId=seeded_product_with_lot.id, quantity=Decimal("1"))
        ],
        occurredAt=explicit,
    )
    sale = SaleService(db_session).create(data, user_id=seeded_user.id)
    db_session.refresh(sale)
    assert sale.occurred_at == explicit
