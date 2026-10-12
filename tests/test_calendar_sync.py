"""La pasada de sincronizacion lleva cada seguimiento `pending`/`failed` a
Google segun su estado, y deja la fila `synced` o `failed`."""

from datetime import timedelta

from sqlalchemy import update

from app.core.datetime_utils import business_today
from app.models.follow_up import FollowUp
from app.services.calendar_sync import sync_pending_follow_ups
from app.services.google_calendar import CalendarError


class FakeCalendar:
    def __init__(self, exists=False, fail=None, on_insert=None):
        self.exists = exists
        self.fail = fail
        self.on_insert = on_insert
        self.calls = []

    def insert(self, event):
        self.calls.append(("insert", event["id"], event))
        if self.on_insert:
            self.on_insert()
        if self.fail:
            raise CalendarError(self.fail)
        return not self.exists

    def patch(self, event_id, fields):
        self.calls.append(("patch", event_id, fields))

    def delete(self, event_id):
        self.calls.append(("delete", event_id, None))
        if self.fail:
            raise CalendarError(self.fail)


def _follow_up(db, customer, product, **fields):
    values = {
        "due_date": business_today() + timedelta(days=3),
        "status": "open",
        "sync_status": "pending",
        **fields,
    }
    follow_up = FollowUp(customer_id=customer.id, product_id=product.id, **values)
    db.add(follow_up)
    db.commit()
    return follow_up


def test_an_open_follow_up_is_inserted_as_an_all_day_event(
    db_session, seeded_customer, seeded_product_with_lot
):
    follow_up = _follow_up(db_session, seeded_customer, seeded_product_with_lot)
    calendar = FakeCalendar()

    assert sync_pending_follow_ups(db_session, calendar) == 0

    [(op, event_id, event)] = calendar.calls
    assert (op, event_id) == ("insert", follow_up.id.hex)
    assert event["start"] == {"date": follow_up.due_date.isoformat()}
    assert event["end"] == {
        "date": (follow_up.due_date + timedelta(days=1)).isoformat()
    }
    assert event["summary"] == (
        f"Seguimiento: {seeded_customer.name} · {seeded_product_with_lot.name}"
    )
    db_session.refresh(follow_up)
    assert (follow_up.sync_status, follow_up.sync_error) == ("synced", None)


def test_an_existing_event_is_patched_back_to_confirmed(
    db_session, seeded_customer, seeded_product_with_lot
):
    """Si alguien lo borro a mano en Google, el id queda cancelado: 409 al
    insertar, y el patch con status confirmed lo restaura."""
    follow_up = _follow_up(db_session, seeded_customer, seeded_product_with_lot)
    calendar = FakeCalendar(exists=True)

    sync_pending_follow_ups(db_session, calendar)

    assert [c[0] for c in calendar.calls] == ["insert", "patch"]
    patch_fields = calendar.calls[1][2]
    assert patch_fields["status"] == "confirmed"
    assert patch_fields["start"] == {"date": follow_up.due_date.isoformat()}


def test_a_closed_follow_up_is_deleted(
    db_session, seeded_customer, seeded_product_with_lot
):
    from datetime import datetime, timezone

    follow_up = _follow_up(
        db_session,
        seeded_customer,
        seeded_product_with_lot,
        status="closed",
        close_reason="dismissed",
        closed_at=datetime.now(timezone.utc),
    )
    calendar = FakeCalendar()

    sync_pending_follow_ups(db_session, calendar)

    assert calendar.calls == [("delete", follow_up.id.hex, None)]
    db_session.refresh(follow_up)
    assert follow_up.sync_status == "synced"


def test_a_failure_is_recorded_and_retried(
    db_session, seeded_customer, seeded_product_with_lot
):
    follow_up = _follow_up(db_session, seeded_customer, seeded_product_with_lot)

    assert sync_pending_follow_ups(db_session, FakeCalendar(fail="403: forbidden")) == 1
    db_session.refresh(follow_up)
    assert follow_up.sync_status == "failed"
    assert "403" in follow_up.sync_error

    assert sync_pending_follow_ups(db_session, FakeCalendar()) == 0
    db_session.refresh(follow_up)
    assert (follow_up.sync_status, follow_up.sync_error) == ("synced", None)


def test_synced_and_skipped_rows_are_not_sent(
    db_session, seeded_customer, seeded_product_with_lot, seeded_product_with_one_lot
):
    _follow_up(
        db_session, seeded_customer, seeded_product_with_lot, sync_status="synced"
    )
    _follow_up(
        db_session, seeded_customer, seeded_product_with_one_lot, sync_status="skipped"
    )
    calendar = FakeCalendar()

    sync_pending_follow_ups(db_session, calendar)

    assert calendar.calls == []


def test_a_follow_up_closed_mid_sync_stays_pending(
    db_session, seeded_customer, seeded_product_with_lot
):
    """Se cierra mientras se insertaba su evento: marcarlo `synced` dejaria el
    evento vivo en Google para siempre."""
    from datetime import datetime, timezone

    follow_up = _follow_up(db_session, seeded_customer, seeded_product_with_lot)

    def close_it():
        db_session.execute(
            update(FollowUp)
            .where(FollowUp.id == follow_up.id)
            .values(
                status="closed",
                close_reason="dismissed",
                closed_at=datetime.now(timezone.utc),
                sync_status="pending",
            )
        )

    sync_pending_follow_ups(db_session, FakeCalendar(on_insert=close_it))

    db_session.expire_all()
    assert db_session.get(FollowUp, follow_up.id).sync_status == "pending"
