"""Cambiar el estado de pago de una venta mueve la caja en la misma
transaccion: pendiente -> pagada registra la entrada y fija la fecha de
pago; pagada -> pendiente retira esa entrada, porque el libro no debe
seguir mostrando un ingreso que ya no es cierto."""

from datetime import date
from decimal import Decimal

import pytest

import app.services.sales.orchestrator as orchestrator_module
from app.core.datetime_utils import business_midnight
from app.models.cash_movement import CashMovement, CashMovementType
from app.schemas.sale import SaleCreate, SaleItemCreate, SalePaymentStatusUpdate
from app.services.cash_service import CashService
from app.services.sales import SaleService


def _sale(customer, product, pending):
    return SaleCreate(
        customerId=customer.id,
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=product.id, quantity=Decimal("1"))],
        isPaymentPending=pending,
    )


def test_marking_a_pending_sale_as_paid_records_one_cash_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0

    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))

    movements = db_session.query(CashMovement).filter_by(sale_id=sale.id).all()
    assert len(movements) == 1
    assert movements[0].type == CashMovementType.ENTRADA
    assert movements[0].amount == sale.total


def test_marking_a_pending_sale_as_paid_sets_the_payment_date(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)
    assert sale.payment_date is None

    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))

    reloaded = service.get_by_id(sale.id)
    assert reloaded.payment_date is not None


def test_marking_an_already_paid_sale_as_paid_again_does_not_duplicate_the_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)

    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))
    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))

    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 1


def test_marking_a_paid_sale_as_pending_removes_the_cash_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=False), user_id=seeded_user.id)
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 1

    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=True))

    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0


def test_marking_an_already_pending_sale_as_pending_again_is_a_no_op(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)

    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=True))

    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0


def test_pending_to_paid_and_back_leaves_no_stray_cash_entry(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)

    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))
    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=True))

    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0


def test_a_second_paid_transition_after_a_correction_uses_the_new_payment_date(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """pendiente -> pagada -> pendiente -> pagada: la primera fecha de pago
    era del dia de la correccion, no del dia del cobro real. Si
    `payment_date` no se limpia junto con el movimiento al volver a
    pendiente, la segunda entrada queda fechada con la fecha vieja en vez
    de la fecha real del segundo cobro."""
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)

    first_payment_date = date(2026, 9, 24)
    second_payment_date = date(2026, 9, 30)
    upcoming_dates = iter([first_payment_date, second_payment_date])

    monkeypatch.setattr(orchestrator_module, "business_today", lambda: next(upcoming_dates))

    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))
    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=True))
    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))

    movements = db_session.query(CashMovement).filter_by(sale_id=sale.id).all()
    assert len(movements) == 1
    assert movements[0].occurred_at == business_midnight(second_payment_date)

    reloaded = service.get_by_id(sale.id)
    assert reloaded.payment_date == second_payment_date


def test_marking_a_sale_paid_dates_it_on_the_business_day_not_the_server_day(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """El panel marca pagada una venta a las 21:30 en Guatemala, cuando el
    servidor (UTC) ya esta en el dia siguiente: el cobro es de hoy en
    Guatemala, y la entrada de caja tambien."""
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)
    monkeypatch.setattr(orchestrator_module, "business_today", lambda: date(2026, 10, 1))

    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))

    assert service.get_by_id(sale.id).payment_date == date(2026, 10, 1)
    movement = db_session.query(CashMovement).filter_by(sale_id=sale.id).one()
    assert movement.occurred_at == business_midnight(date(2026, 10, 1))


def test_the_enriched_response_exposes_the_payment_date_and_method(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    """SaleResponse no tenia payment_date ni payment_method: el panel no
    podia ver lo que esta rama empezo a escribir."""
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)

    result = service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))

    assert result.payment_date is not None
    assert result.payment_date_formatted
    assert result.payment_method is None  # nunca se supo el medio: SalePaymentStatusUpdate no lo carga


def test_the_cash_entry_shares_the_payment_status_transaction(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """Si la caja falla al marcar como pagada, la venta no queda a medias."""
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)

    def explode(*args, **kwargs):
        raise RuntimeError("caja caida")

    monkeypatch.setattr(service.cash_service, "record", explode)

    with pytest.raises(RuntimeError):
        service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=False))

    db_session.rollback()
    reloaded = service.get_by_id(sale.id)
    assert reloaded.is_payment_pending is True
    assert reloaded.payment_date is None
    assert db_session.query(CashMovement).filter_by(sale_id=sale.id).count() == 0


def test_marking_a_paid_sale_as_pending_does_not_remove_an_aporte_socio_on_the_same_sale(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    """Espejo exacto del filtro de tipo que `PurchaseService.cancel()` ya
    tiene: retirar el cobro de una venta debe borrar solo la ENTRADA que ese
    cobro genero, no cualquier movimiento con ese `sale_id`.

    `registrar_movimiento_caja` (Task 8) acepta `venta_id` para los cinco
    tipos, asi que un `aporte_socio` apuntado a una venta es dinero que un
    socio puso de verdad -- corregir la venta a pendiente no lo hace
    desaparecer."""
    service = SaleService(db_session)
    sale = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=False), user_id=seeded_user.id)

    contribution = CashService(db_session).record(
        occurred_at=business_midnight(date(2026, 9, 24)),
        type=CashMovementType.APORTE_SOCIO,
        amount=Decimal("60.00"),
        sale_id=sale.id,
        note="Aporte del socio ligado a esta venta",
    )

    service.update_payment_status_enriched(sale.id, SalePaymentStatusUpdate(isPaymentPending=True))

    remaining = db_session.query(CashMovement).filter_by(sale_id=sale.id).all()
    assert len(remaining) == 1
    assert remaining[0].id == contribution.id
    assert remaining[0].type == CashMovementType.APORTE_SOCIO
