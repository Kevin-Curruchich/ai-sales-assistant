import uuid

import pytest
from sqlalchemy import text

from app.core.database import bind_business
from tests.fixtures_domain import _session_for


@pytest.fixture
def unbound_session(migrated_schema):
    # Sesion propia, nunca ligada a un comercio por otros fixtures.
    session, engine = _session_for(migrated_schema)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _current(db):
    return db.execute(text("select current_setting('app.business_id', true)")).scalar()


def test_bind_sets_variable_in_open_transaction(unbound_session):
    bid = uuid.uuid4()
    unbound_session.execute(text("select 1"))  # abre transaccion
    bind_business(unbound_session, bid, "America/Guatemala")
    assert _current(unbound_session) == str(bid)


def test_variable_survives_commit(unbound_session):
    bid = uuid.uuid4()
    bind_business(unbound_session, bid, "America/Guatemala")
    unbound_session.commit()
    assert _current(unbound_session) == str(bid)


def test_unbound_session_has_no_business(unbound_session):
    assert _current(unbound_session) in (None, "")
