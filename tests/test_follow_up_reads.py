"""Prioritarios, metricas y calendario leen seguimientos abiertos.

El caso de Aurita: un seguimiento descartado o pospuesto ya no la deja
"Urgente" en el dashboard.
"""

from datetime import timedelta
from decimal import Decimal

from app.core.datetime_utils import business_today
from app.models import CustomerProductCycle
from app.models.follow_up import FollowUp
from app.services.follow_up_service import FollowUpService
from app.services.sales import SaleService


def _cycle_and_follow_up(db, customer, product, days_until):
    due = business_today(db) + timedelta(days=days_until)
    db.add(
        CustomerProductCycle(
            customer_id=customer.id,
            product_id=product.id,
            avg_interval_days=Decimal("7"),
            estimated_next_purchase=due,
            last_purchase_date=business_today(db) - timedelta(days=5),
            last_quantity=Decimal("1"),
            total_purchases=3,
        )
    )
    follow_up = FollowUp(
        customer_id=customer.id, product_id=product.id, due_date=due, status="open"
    )
    db.add(follow_up)
    db.commit()
    return follow_up


def test_priority_list_shows_open_follow_ups_with_their_id_and_date(
    db_session, seeded_customer, seeded_product_with_lot
):
    follow_up = _cycle_and_follow_up(
        db_session, seeded_customer, seeded_product_with_lot, 2
    )

    [row], total = SaleService(db_session).get_follow_ups(filter_type="7_days")

    assert total == 1 and row.status == "urgent"
    assert row.items[0].follow_up_id == follow_up.id
    assert row.items[0].due_date == follow_up.due_date
    assert row.items[0].days_until == 2


def test_a_dismissed_follow_up_leaves_every_list(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    follow_up = _cycle_and_follow_up(
        db_session, seeded_customer, seeded_product_with_lot, 2
    )
    FollowUpService(db_session).dismiss(follow_up.id, seeded_user.id)
    service = SaleService(db_session)

    for filter_type in ("7_days", "all"):
        assert service.get_follow_ups(filter_type=filter_type) == ([], 0)
    assert service.get_follow_up_metrics().next7Days == 0
    calendar = service.get_calendar_events(
        business_today(db_session), business_today(db_session) + timedelta(days=7)
    )
    assert calendar.summary.upcoming == 0


def test_a_postponed_follow_up_uses_the_new_date(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    follow_up = _cycle_and_follow_up(
        db_session, seeded_customer, seeded_product_with_lot, 2
    )
    new_date = business_today(db_session) + timedelta(days=20)
    new = FollowUpService(db_session).postpone(follow_up.id, new_date, seeded_user.id)
    service = SaleService(db_session)

    assert service.get_follow_ups(filter_type="7_days") == ([], 0)
    [row], _ = service.get_follow_ups(filter_type="30_days")
    assert (row.items[0].follow_up_id, row.items[0].due_date) == (new.id, new_date)
    calendar = service.get_calendar_events(new_date, new_date)
    [event] = calendar.dates[0].events
    assert event.followUpId == new.id
