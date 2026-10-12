"""Ciclo de vida de un seguimiento: venta, posponer, descartar.

Caso que motivo la regla de fechas: ventas de huevos a Aurita el 10 y el 17;
seguimiento abierto en la proyeccion. Una venta olvidada registrada tarde
(fecha anterior a la ultima compra) no es informacion nueva: no cierra el
seguimiento ni le mueve la fecha.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

import app.services.calendar_sync as calendar_sync
from app.core.datetime_utils import business_today
from app.models.follow_up import FollowUp
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.follow_up_service import FollowUpService
from app.services.sales import SaleService


@pytest.fixture
def sync_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(calendar_sync, "request_calendar_sync", lambda: calls.append(1))
    return calls


def _sell(service, customer, product, user, day, lines=1):
    return service.create(
        SaleCreate(
            customerId=customer.id,
            date=day,
            items=[
                SaleItemCreate(productId=product.id, quantity=Decimal("1"))
                for _ in range(lines)
            ],
        ),
        user_id=user.id,
    )


def _follow_ups(db, customer, product):
    return (
        db.query(FollowUp)
        .filter_by(customer_id=customer.id, product_id=product.id)
        .order_by(FollowUp.created_at, FollowUp.due_date)
        .all()
    )


def _open(db, customer, product):
    return [f for f in _follow_ups(db, customer, product) if f.status == "open"]


@pytest.fixture
def two_sales(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, sync_requests
):
    service = SaleService(db_session)
    _sell(
        service,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 10),
    )
    _sell(
        service,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 17),
    )
    return service


def test_a_first_sale_opens_nothing(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, sync_requests
):
    _sell(
        SaleService(db_session),
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 10),
    )

    assert _follow_ups(db_session, seeded_customer, seeded_product_with_lot) == []


def test_a_second_sale_opens_one_at_the_projection(
    db_session, seeded_customer, seeded_product_with_lot, two_sales
):
    cycle = two_sales.cycle_repo.get_by_customer_and_product(
        seeded_customer.id, seeded_product_with_lot.id
    )
    [opened] = _open(db_session, seeded_customer, seeded_product_with_lot)
    assert opened.due_date == cycle.estimated_next_purchase
    assert opened.sync_status == "pending"


def test_a_newer_sale_closes_the_open_one_and_opens_the_next(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [before] = _open(db_session, seeded_customer, seeded_product_with_lot)

    sale = _sell(
        two_sales,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 24),
    )

    db_session.refresh(before)
    assert (before.status, before.close_reason, before.sale_id) == (
        "closed",
        "sale",
        sale.id,
    )
    assert before.closed_at is not None and before.closed_by is None
    [after] = _open(db_session, seeded_customer, seeded_product_with_lot)
    assert after.id != before.id


def test_a_backdated_sale_leaves_follow_ups_alone(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [before] = _open(db_session, seeded_customer, seeded_product_with_lot)
    due = before.due_date

    _sell(
        two_sales,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 12),
    )

    [after] = _open(db_session, seeded_customer, seeded_product_with_lot)
    assert after.id == before.id and after.due_date == due


def test_a_second_sale_on_the_same_day_leaves_follow_ups_alone(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [before] = _open(db_session, seeded_customer, seeded_product_with_lot)

    _sell(
        two_sales,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 17),
    )

    [after] = _open(db_session, seeded_customer, seeded_product_with_lot)
    assert after.id == before.id


def test_the_same_product_twice_in_one_sale_opens_a_single_follow_up(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    _sell(
        two_sales,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 24),
        lines=2,
    )

    assert len(_open(db_session, seeded_customer, seeded_product_with_lot)) == 1


def test_a_sale_requests_a_calendar_sync(two_sales, sync_requests):
    assert len(sync_requests) == 2


def test_postpone_closes_and_reopens_on_the_chosen_date(
    db_session,
    seeded_customer,
    seeded_product_with_lot,
    seeded_user,
    two_sales,
    sync_requests,
):
    [current] = _open(db_session, seeded_customer, seeded_product_with_lot)
    new_date = business_today() + timedelta(days=5)
    before_calls = len(sync_requests)

    new = FollowUpService(db_session).postpone(current.id, new_date, seeded_user.id)

    db_session.refresh(current)
    assert (current.status, current.close_reason, current.closed_by) == (
        "closed",
        "postponed",
        seeded_user.id,
    )
    assert (new.status, new.due_date) == ("open", new_date)
    assert len(sync_requests) == before_calls + 1


@pytest.mark.parametrize("days", [0, -1])
def test_postpone_rejects_a_date_that_is_not_in_the_future(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales, days
):
    [current] = _open(db_session, seeded_customer, seeded_product_with_lot)

    with pytest.raises(HTTPException) as exc:
        FollowUpService(db_session).postpone(
            current.id, business_today() + timedelta(days=days), seeded_user.id
        )
    assert exc.value.status_code == 422


def test_postpone_or_dismiss_a_closed_follow_up_is_a_conflict(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [current] = _open(db_session, seeded_customer, seeded_product_with_lot)
    service = FollowUpService(db_session)
    service.dismiss(current.id, seeded_user.id)

    with pytest.raises(HTTPException) as exc:
        service.dismiss(current.id, seeded_user.id)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        service.postpone(
            current.id, business_today() + timedelta(days=3), seeded_user.id
        )
    assert exc.value.status_code == 409


def test_an_unknown_follow_up_is_not_found(db_session, seeded_user):
    import uuid

    with pytest.raises(HTTPException) as exc:
        FollowUpService(db_session).dismiss(uuid.uuid4(), seeded_user.id)
    assert exc.value.status_code == 404


def test_dismiss_opens_nothing_until_the_next_sale(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [current] = _open(db_session, seeded_customer, seeded_product_with_lot)

    FollowUpService(db_session).dismiss(current.id, seeded_user.id)

    db_session.refresh(current)
    assert (current.status, current.close_reason) == ("closed", "dismissed")
    assert _open(db_session, seeded_customer, seeded_product_with_lot) == []

    _sell(
        two_sales,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 24),
    )

    assert len(_open(db_session, seeded_customer, seeded_product_with_lot)) == 1


def test_a_backdated_sale_that_creates_the_first_projection_opens_one(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, sync_requests
):
    service = SaleService(db_session)
    _sell(
        service,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 17),
    )
    assert _follow_ups(db_session, seeded_customer, seeded_product_with_lot) == []

    # Venta olvidada, registrada despues: el par pasa a tener proyeccion.
    _sell(
        service,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 10),
    )

    cycle = service.cycle_repo.get_by_customer_and_product(
        seeded_customer.id, seeded_product_with_lot.id
    )
    assert cycle.estimated_next_purchase is not None
    [opened] = _open(db_session, seeded_customer, seeded_product_with_lot)
    assert opened.due_date == cycle.estimated_next_purchase


def test_a_backdated_sale_after_a_dismiss_opens_nothing(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, two_sales
):
    [current] = _open(db_session, seeded_customer, seeded_product_with_lot)
    FollowUpService(db_session).dismiss(current.id, seeded_user.id)

    _sell(
        two_sales,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 5),
    )

    assert _open(db_session, seeded_customer, seeded_product_with_lot) == []


def test_deleting_a_customer_deletes_their_follow_ups(
    db_session, seeded_customer, seeded_product_with_lot, two_sales
):
    from app.services.customer_service import CustomerService

    customer_id = seeded_customer.id
    assert len(_open(db_session, seeded_customer, seeded_product_with_lot)) == 1

    CustomerService(db_session).delete(customer_id)

    assert db_session.query(FollowUp).filter_by(customer_id=customer_id).count() == 0
