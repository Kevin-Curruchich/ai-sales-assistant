from datetime import date, datetime, timezone

import pytest
from sqlalchemy.orm import Session

import app.core.datetime_utils as datetime_utils_module
from app.core.config import settings
from app.core.datetime_utils import (
    MissingBusinessContext,
    business_instant,
    business_midnight,
    business_today,
    business_tz,
    format_business_date,
    format_business_datetime,
    to_business_tz,
)


def test_business_timezone_is_configured():
    assert settings.BUSINESS_TIMEZONE == "America/Guatemala"


def test_a_late_night_sale_keeps_its_own_day(db_session):
    """Caso real: 2026-04-07 03:48 UTC son las 21:48 del 6 en Guatemala.

    El panel venia mostrando 07/04 para 78 de 156 ventas.
    """
    utc = datetime(2026, 4, 7, 3, 48, 44, tzinfo=timezone.utc)
    assert format_business_date(db_session, utc) == "06/04/2026"
    assert to_business_tz(db_session, utc).date() == date(2026, 4, 6)


def test_business_today_is_the_day_in_guatemala_not_on_the_server(
    db_session, monkeypatch
):
    """Railway corre en UTC: a las 21:30 del 1 de octubre en Guatemala el
    servidor ya esta en el 2. Un cobro marcado a esa hora es del dia 1."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, 3, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(datetime_utils_module, "datetime", FrozenDatetime)

    assert business_today(db_session) == date(2026, 10, 1)


class _FrozenAtNineFortyGuatemala(datetime):
    @classmethod
    def now(cls, tz=None):
        # 2026-10-12 15:40 UTC son las 09:40 del 12 en Guatemala.
        return datetime(2026, 10, 12, 15, 40, tzinfo=timezone.utc).astimezone(tz)


def test_a_movement_dated_today_keeps_the_real_hour(db_session, monkeypatch):
    """Caso real: todos los movimientos de caja salian a las 12:00 a. m. y
    no se podia saber a que hora se hizo cada venta o compra del dia."""
    monkeypatch.setattr(datetime_utils_module, "datetime", _FrozenAtNineFortyGuatemala)

    instant = business_instant(db_session, date(2026, 10, 12))

    assert format_business_datetime(db_session, instant) == "12/10/2026 09:40"


def test_a_movement_dated_in_the_past_stays_at_midnight(db_session, monkeypatch):
    """Un dia pasado no tiene hora conocida: no se inventa una."""
    monkeypatch.setattr(datetime_utils_module, "datetime", _FrozenAtNineFortyGuatemala)

    day = date(2026, 10, 9)

    assert business_instant(db_session, day) == business_midnight(db_session, day)


def _freeze_utc(monkeypatch, instant):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(datetime_utils_module, "datetime", Frozen)


def test_today_follows_business_timezone(db_session, monkeypatch):
    # 2026-10-26 03:00 UTC: Guatemala sigue en el 25, Madrid ya esta en el 26
    _freeze_utc(monkeypatch, datetime(2026, 10, 26, 3, tzinfo=timezone.utc))
    db_session.info["business_tz"] = "America/Guatemala"
    assert business_today(db_session) == date(2026, 10, 25)
    db_session.info["business_tz"] = "Europe/Madrid"
    assert business_today(db_session) == date(2026, 10, 26)


def test_missing_timezone_raises():
    with pytest.raises(MissingBusinessContext):
        business_tz(Session())


def test_a_midday_timestamp_is_unaffected(db_session):
    utc = datetime(2026, 8, 27, 17, 26, 44, tzinfo=timezone.utc)
    assert format_business_date(db_session, utc) == "27/08/2026"


def test_datetime_format_shows_the_local_hour(db_session):
    utc = datetime(2026, 4, 7, 3, 48, 44, tzinfo=timezone.utc)
    assert format_business_datetime(db_session, utc) == "06/04/2026 21:48"


def test_a_naive_datetime_is_assumed_utc_not_silently_local(db_session):
    """Un naive que se interprete en la zona del servidor es como nacen los
    desfases de seis horas."""
    naive = datetime(2026, 4, 7, 3, 48, 44)
    assert format_business_date(db_session, naive) == "06/04/2026"


def test_none_formats_as_empty_not_as_a_crash(db_session):
    assert format_business_date(db_session, None) == ""
    assert format_business_datetime(db_session, None) == ""


def test_occurred_at_exists_and_is_timestamptz(test_engine, migrated_schema):
    from sqlalchemy import text

    with test_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, is_nullable, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = :s AND column_name = 'occurred_at'"
            ),
            {"s": migrated_schema},
        ).all()

    by_table = {r[0]: (r[1], r[2]) for r in rows}
    assert by_table["sales"] == ("YES", "timestamp with time zone")
    assert by_table["purchases"] == ("YES", "timestamp with time zone")
    # La caja nace con el instante en la Task 2: NOT NULL, sin filas historicas.
    assert by_table["cash_movements"] == ("NO", "timestamp with time zone")
