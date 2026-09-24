"""Confirmar una compra genera su salida de caja; un borrador no."""

import pytest

from app.models.cash_movement import CashMovement, CashMovementType
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
