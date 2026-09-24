from datetime import date, datetime, timezone
from decimal import Decimal

from app.core.config import settings
from app.core.datetime_utils import (
    format_business_date,
    format_business_datetime,
    to_business_tz,
)


def test_business_timezone_is_configured():
    assert settings.BUSINESS_TIMEZONE == "America/Guatemala"


def test_a_late_night_sale_keeps_its_own_day():
    """Caso real: 2026-04-07 03:48 UTC son las 21:48 del 6 en Guatemala.

    El panel venia mostrando 07/04 para 78 de 156 ventas.
    """
    utc = datetime(2026, 4, 7, 3, 48, 44, tzinfo=timezone.utc)
    assert format_business_date(utc) == "06/04/2026"
    assert to_business_tz(utc).date() == date(2026, 4, 6)


def test_a_midday_timestamp_is_unaffected():
    utc = datetime(2026, 8, 27, 17, 26, 44, tzinfo=timezone.utc)
    assert format_business_date(utc) == "27/08/2026"


def test_datetime_format_shows_the_local_hour():
    utc = datetime(2026, 4, 7, 3, 48, 44, tzinfo=timezone.utc)
    assert format_business_datetime(utc) == "06/04/2026 21:48"


def test_a_naive_datetime_is_assumed_utc_not_silently_local():
    """Un naive que se interprete en la zona del servidor es como nacen los
    desfases de seis horas."""
    naive = datetime(2026, 4, 7, 3, 48, 44)
    assert format_business_date(naive) == "06/04/2026"


def test_none_formats_as_empty_not_as_a_crash():
    assert format_business_date(None) == ""
    assert format_business_datetime(None) == ""


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
