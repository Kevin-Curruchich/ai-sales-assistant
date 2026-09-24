"""Confirmar una compra genera su salida de caja; un borrador no."""

import pytest

from app.models.cash_movement import CashMovement, CashMovementType
from app.models.payment_method import PaymentMethod
from app.services.purchase_service import PurchaseService


def test_confirming_a_purchase_records_one_cash_exit(db_session, seeded_purchase_draft):
    service = PurchaseService(db_session)
    service.confirm(seeded_purchase_draft.id)

    movements = db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).all()
    assert len(movements) == 1
    assert movements[0].type == CashMovementType.SALIDA


def test_a_draft_purchase_records_nothing(db_session, seeded_purchase_draft):
    assert db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).count() == 0


def test_confirming_twice_does_not_duplicate_the_cash_exit(db_session, seeded_purchase_draft):
    service = PurchaseService(db_session)
    service.confirm(seeded_purchase_draft.id)
    service.confirm(seeded_purchase_draft.id)

    assert db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).count() == 1


def test_cancelling_a_confirmed_purchase_removes_its_cash_exit(db_session, seeded_purchase_draft):
    service = PurchaseService(db_session)
    service.confirm(seeded_purchase_draft.id)
    assert db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).count() == 1

    service.cancel(seeded_purchase_draft.id)

    assert db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).count() == 0


def test_cancelling_a_draft_purchase_records_nothing_to_remove(db_session, seeded_purchase_draft):
    service = PurchaseService(db_session)
    service.cancel(seeded_purchase_draft.id)

    assert db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).count() == 0


def test_cancelling_twice_does_not_error_after_the_cash_exit_is_gone(db_session, seeded_purchase_draft):
    service = PurchaseService(db_session)
    service.confirm(seeded_purchase_draft.id)
    service.cancel(seeded_purchase_draft.id)
    service.cancel(seeded_purchase_draft.id)

    assert db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).count() == 0


def test_the_enriched_response_exposes_the_payment_method(db_session, seeded_purchase_draft):
    """PurchaseResponse no tenia payment_method pese a que create() ya lo escribe (Task 3)."""
    seeded_purchase_draft.payment_method = PaymentMethod.TRANSFERENCIA
    db_session.commit()

    service = PurchaseService(db_session)
    result = service.confirm(seeded_purchase_draft.id)

    assert result.payment_method == PaymentMethod.TRANSFERENCIA
