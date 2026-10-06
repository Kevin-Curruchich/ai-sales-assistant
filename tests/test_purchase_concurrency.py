"""Compras confirmadas en paralelo: dos requests que llegan al mismo tiempo
(un doble click, un reintento del navegador, dos compras del mismo producto)
no pueden duplicar la salida de caja ni perder una suma de stock.

Cada hilo usa su propia sesion -- como dos requests reales -- y una barrera
los retiene justo antes de registrar la salida de caja, para que ambos hayan
leido lo que necesitan antes de que alguno escriba. Si el codigo bloquea las
filas que lee, el segundo hilo ni siquiera llega a la barrera: espera el
lock, la barrera vence y el primero termina solo.
"""

import threading
from datetime import date
from decimal import Decimal

from app.models.cash_movement import CashMovement
from app.models.product import Product
from app.models.purchase import Purchase, PurchaseItem
from app.services.cash_service import CashService
from app.services.purchase_service import PurchaseService
from tests.fixtures_domain import _session_for

BARRIER_TIMEOUT_S = 2


def _confirm_concurrently(schema, monkeypatch, purchase_ids):
    barrier = threading.Barrier(len(purchase_ids))
    original_record = CashService.record

    def record_after_everyone_read(self, *args, **kwargs):
        try:
            barrier.wait(timeout=BARRIER_TIMEOUT_S)
        except threading.BrokenBarrierError:
            pass  # el otro hilo esta esperando un lock: seguimos solos
        return original_record(self, *args, **kwargs)

    monkeypatch.setattr(CashService, "record", record_after_everyone_read)
    errors = []

    def worker(purchase_id):
        session, engine = _session_for(schema)
        try:
            PurchaseService(session).confirm(purchase_id)
        except Exception as exc:  # noqa: BLE001 - el test reporta cualquier fallo
            errors.append(repr(exc))
        finally:
            session.close()
            engine.dispose()

    threads = [threading.Thread(target=worker, args=(pid,)) for pid in purchase_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "un hilo quedo colgado"
    return errors


def test_confirming_the_same_purchase_twice_at_once_records_one_cash_exit(
    db_session, migrated_schema, seeded_purchase_draft, monkeypatch
):
    purchase_id = seeded_purchase_draft.id
    product_id = seeded_purchase_draft.items[0].product_id
    db_session.commit()

    errors = _confirm_concurrently(migrated_schema, monkeypatch, [purchase_id, purchase_id])

    assert errors == []
    db_session.expire_all()
    assert db_session.query(CashMovement).filter_by(purchase_id=purchase_id).count() == 1
    assert db_session.get(Product, product_id).stock == Decimal("4")


def test_confirming_two_purchases_of_the_same_product_at_once_adds_both_to_stock(
    db_session, migrated_schema, seeded_purchase_draft, seeded_user, monkeypatch
):
    first = seeded_purchase_draft
    product_id = first.items[0].product_id
    second = Purchase(
        user_id=seeded_user.id, date=date(2026, 9, 21), total=Decimal("30.00"), status="draft"
    )
    db_session.add(second)
    db_session.flush()
    db_session.add(
        PurchaseItem(
            purchase_id=second.id,
            product_id=product_id,
            quantity=Decimal("2"),
            remaining_quantity=Decimal("0"),
            unit_cost=Decimal("15.00"),
            subtotal=Decimal("30.00"),
        )
    )
    db_session.commit()

    errors = _confirm_concurrently(migrated_schema, monkeypatch, [first.id, second.id])

    assert errors == []
    db_session.expire_all()
    assert db_session.get(Product, product_id).stock == Decimal("6")
