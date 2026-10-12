"""`PUT /api/v1/sales/{sale_id}` tambien cambia el estado de pago.

`SaleService.update()` es la segunda ruta viva que toca `is_payment_pending`
-- la primera, `update_payment_status_enriched`, ya movia la caja. Esta no
movia nada: pagada -> pendiente dejaba la ENTRADA puesta (el libro seguia
mostrando un ingreso que la venta ya no afirma) y pendiente -> pagada no
registraba el cobro. El saldo quedaba mal en las dos direcciones, y sin
ningun rastro que explicara la diferencia.
"""

from datetime import date
from decimal import Decimal

from app.core.datetime_utils import business_midnight
from app.models.cash_movement import CashMovement, CashMovementType
from app.schemas.sale import SaleCreate, SaleItemCreate, SaleUpdate
from app.services.cash_service import CashService
from app.services.sales import SaleService


def _sale(customer, product, pending):
    return SaleCreate(
        customerId=customer.id,
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=product.id, quantity=Decimal("1"))],
        isPaymentPending=pending,
    )


def test_update_marking_a_pending_sale_as_paid_records_one_cash_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(
        _sale(seeded_customer, seeded_product_with_lot, pending=True),
        user_id=seeded_user.id,
    )
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0

    service.update(sale.id, SaleUpdate(isPaymentPending=False))

    movements = db_session.query(CashMovement).filter_by(sale_id=sale.id).all()
    assert len(movements) == 1
    assert movements[0].type == CashMovementType.ENTRADA
    assert movements[0].amount == sale.total


def test_update_marking_a_pending_sale_as_paid_sets_the_payment_date(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(
        _sale(seeded_customer, seeded_product_with_lot, pending=True),
        user_id=seeded_user.id,
    )

    service.update(sale.id, SaleUpdate(isPaymentPending=False))

    assert service.get_by_id(sale.id).payment_date is not None


def test_update_marking_a_paid_sale_as_pending_removes_the_cash_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(
        _sale(seeded_customer, seeded_product_with_lot, pending=False),
        user_id=seeded_user.id,
    )
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 1

    service.update(sale.id, SaleUpdate(isPaymentPending=True))

    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0
    assert service.get_by_id(sale.id).payment_date is None


def test_update_without_the_payment_field_leaves_the_cash_book_alone(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    """Un PUT que solo cambia el cliente no es una transicion de pago.

    (No usa `date=`: `SaleUpdate.date` es hoy un campo que solo acepta
    `None` -- ver el concern del reporte final. No es esta pieza.)"""
    service = SaleService(db_session)
    sale = service.create(
        _sale(seeded_customer, seeded_product_with_lot, pending=False),
        user_id=seeded_user.id,
    )

    service.update(sale.id, SaleUpdate(customerId=seeded_customer.id))

    movements = db_session.query(CashMovement).filter_by(sale_id=sale.id).all()
    assert len(movements) == 1
    assert movements[0].type == CashMovementType.ENTRADA


def test_update_repeating_the_same_payment_status_does_not_duplicate_the_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(
        _sale(seeded_customer, seeded_product_with_lot, pending=True),
        user_id=seeded_user.id,
    )

    service.update(sale.id, SaleUpdate(isPaymentPending=False))
    service.update(sale.id, SaleUpdate(isPaymentPending=False))

    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 1


def test_update_to_pending_does_not_remove_an_aporte_socio_on_the_same_sale(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    """El mismo filtro de tipo que `update_payment_status_enriched`: esta
    ruta comparte el codigo, asi que comparte la garantia."""
    service = SaleService(db_session)
    sale = service.create(
        _sale(seeded_customer, seeded_product_with_lot, pending=False),
        user_id=seeded_user.id,
    )
    contribution = CashService(db_session).record(
        occurred_at=business_midnight(date(2026, 9, 24)),
        type=CashMovementType.APORTE_SOCIO,
        amount=Decimal("60.00"),
        sale_id=sale.id,
    )

    service.update(sale.id, SaleUpdate(isPaymentPending=True))

    remaining = db_session.query(CashMovement).filter_by(sale_id=sale.id).all()
    assert len(remaining) == 1
    assert remaining[0].id == contribution.id
