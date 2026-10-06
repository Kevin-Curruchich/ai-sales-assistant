"""Todo "hoy" del backend es hoy en Guatemala, no en el servidor.

Railway corre en UTC: entre las 18:00 y la medianoche local, `date.today()`
ya devuelve el dia siguiente. Los dias que faltan para una recompra, si un
evento del calendario esta vencido, las ventas del mes y que lotes ve el
panel se calculaban con ese dia equivocado.

Cada test fija `business_today` en una fecha lejana a la real (2026-01-15):
si el codigo volviera a usar `date.today()`, el resultado cambia y el test
falla, sin depender de la hora a la que corra la suite.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import app.repositories.sale_repository as sale_repository_module
import app.services.product_service as product_service_module
import app.services.sales.orchestrator as orchestrator_module
from app.models.sale import Sale
from app.repositories.sale_repository import SaleRepository
from app.services.product_service import ProductService
from app.services.sales.orchestrator import SaleService
from tests.fixtures_domain import _seed_product_with_one_confirmed_lot

HOY = date(2026, 1, 15)


def _cycle(days_until):
    return SimpleNamespace(
        customer_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        customer=SimpleNamespace(name="Cliente", email=None),
        product=SimpleNamespace(
            name="Producto", stock=Decimal("5"), min_stock=Decimal("1")
        ),
        avg_interval_days=Decimal("7"),
        last_purchase_date=HOY - timedelta(days=7),
        last_quantity=Decimal("1"),
        estimated_next_purchase=HOY + timedelta(days=days_until),
    )


def test_follow_ups_count_days_from_the_business_day(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "business_today", lambda: HOY)
    service = SaleService(db=None)
    service.cycle_repo.get_all_for_follow_ups = lambda: [_cycle(3)]

    follow_ups, _total = service.get_follow_ups()

    assert follow_ups[0].items[0].days_until == 3
    assert follow_ups[0].status == "urgent"


def test_follow_up_metrics_count_days_from_the_business_day(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "business_today", lambda: HOY)
    service = SaleService(db=None)
    service.cycle_repo.get_all_with_estimation = lambda: [_cycle(3)]

    metrics = service.get_follow_up_metrics()

    assert (metrics.overdue, metrics.next7Days) == (0, 1)


def test_calendar_marks_overdue_against_the_business_day(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "business_today", lambda: HOY)
    service = SaleService(db=None)
    service.cycle_repo.get_all_with_estimation = lambda: [_cycle(-1), _cycle(0)]

    calendar = service.get_calendar_events(
        HOY - timedelta(days=5), HOY + timedelta(days=5)
    )

    tipos = {e.date: e.type for d in calendar.dates for e in d.events}
    assert tipos == {HOY - timedelta(days=1): "overdue", HOY: "upcoming"}


def test_sales_this_month_uses_the_business_month(
    db_session, seeded_customer, seeded_user, monkeypatch
):
    monkeypatch.setattr(sale_repository_module, "business_today", lambda: HOY)
    db_session.add(
        Sale(
            customer_id=seeded_customer.id,
            user_id=seeded_user.id,
            date=HOY,
            total=Decimal("40.00"),
        )
    )
    db_session.commit()

    assert SaleRepository(db_session).get_sales_this_month() == 40.0


def test_the_sale_dropdown_only_sees_lots_bought_up_to_the_business_day(
    db_session, seeded_user, monkeypatch
):
    """Un lote comprado "manana" en Guatemala no se puede vender hoy, aunque
    en el reloj del servidor ya sea manana."""
    monkeypatch.setattr(product_service_module, "business_today", lambda: HOY)
    product = _seed_product_with_one_confirmed_lot(
        db_session,
        seeded_user,
        quantity=Decimal("3"),
        unit_cost=Decimal("10.00"),
        purchase_date=HOY + timedelta(days=1),
    )

    for_sale = {
        p.id: p for p in ProductService(db_session).get_all_active_with_first_lot()
    }

    assert for_sale[product.id].first_available_lot is None


def test_lots_availability_defaults_to_the_business_day(
    db_session, seeded_user, monkeypatch
):
    monkeypatch.setattr(product_service_module, "business_today", lambda: HOY)
    product = _seed_product_with_one_confirmed_lot(
        db_session,
        seeded_user,
        quantity=Decimal("3"),
        unit_cost=Decimal("10.00"),
        purchase_date=HOY + timedelta(days=1),
    )

    availability = ProductService(db_session).get_lots_availability(product.id)

    assert availability.lots == []
