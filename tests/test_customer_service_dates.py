"""format_sale_dates no tenia test propio.

Alimenta get_last_purchases, que es visible en el panel, y es exactamente la
pieza de la Task 9 que existe para matar el desfase UTC-vs-Guatemala. Una
regresion aqui reintroduce ese bug sin que ninguna prueba lo note.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.services.customer_service import CustomerService


def _sale(created_at, updated_at, sale_date):
    return SimpleNamespace(
        created_at=created_at,
        updated_at=updated_at,
        date=sale_date,
        items=[],
    )


def test_format_sale_dates_uses_the_guatemala_calendar_day():
    """2026-09-22T03:30:00+00:00 son las 21:30 del 21 en Guatemala (UTC-6):
    el dia calendario difiere entre UTC y hora del negocio."""
    service = CustomerService(db=None)
    utc_instant = datetime(2026, 9, 22, 3, 30, 0, tzinfo=timezone.utc)
    sale = _sale(created_at=utc_instant, updated_at=utc_instant, sale_date=date(2026, 9, 21))

    result = service.format_sale_dates(sale)

    assert result["created_at"] == "2026-09-21"
    assert result["updated_at"] == "2026-09-21"
