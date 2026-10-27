from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.services import calendar_sync


def test_without_failures_the_worker_sleeps_until_woken():
    assert calendar_sync.next_wait_timeout(0) is None


def test_with_failures_the_worker_retries_every_15_minutes():
    assert calendar_sync.next_wait_timeout(2) == 900


def test_a_crashing_pass_counts_as_a_failure_so_it_is_retried():
    def broken_session_factory():
        raise RuntimeError("db down")

    assert calendar_sync.sync_pass(broken_session_factory, client_for=None) == 1


def test_request_calendar_sync_sets_the_signal():
    calendar_sync._wake.clear()
    calendar_sync.request_calendar_sync()
    assert calendar_sync._wake.is_set()
    calendar_sync._wake.clear()


def _factory(db_session):
    # La migracion copia GOOGLE_CALENDAR_ID del entorno al primer comercio.
    db_session.execute(text("UPDATE businesses SET google_calendar_id = NULL"))
    db_session.commit()
    return sessionmaker(bind=db_session.get_bind())


def test_sync_pass_visits_only_businesses_with_calendar(
    db_session, seeded_business, other_business
):
    factory = _factory(db_session)
    seeded_business.google_calendar_id = "cal-a"
    db_session.commit()
    seen = []

    def client_for(calendar_id):
        seen.append(calendar_id)
        return object()

    assert calendar_sync.sync_pass(factory, client_for) == 0
    assert seen == ["cal-a"]


def test_sync_pass_failure_in_one_business_continues(
    db_session, seeded_business, other_business
):
    factory = _factory(db_session)
    seeded_business.google_calendar_id = "cal-a"
    other_business.google_calendar_id = "cal-b"
    db_session.commit()
    seen = []

    def client_for(calendar_id):
        if calendar_id == "cal-a":
            raise RuntimeError("sin credenciales")
        seen.append(calendar_id)
        return object()

    assert calendar_sync.sync_pass(factory, client_for) >= 1
    assert seen == ["cal-b"]


def test_businesses_in_one_pass_share_the_same_session(
    db_session, seeded_business, other_business
):
    from app.services.google_calendar import GoogleCalendarClient

    factory = _factory(db_session)
    seeded_business.google_calendar_id = "cal-a"
    other_business.google_calendar_id = "cal-b"
    db_session.commit()
    shared = object()
    clients = []

    def client_for(calendar_id):
        clients.append(GoogleCalendarClient(calendar_id, shared))
        return clients[-1]

    calendar_sync.sync_pass(factory, client_for)

    assert {c.calendar_id for c in clients} == {"cal-a", "cal-b"}
    assert all(c.session is shared for c in clients)
