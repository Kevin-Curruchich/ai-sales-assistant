"""Cuentas por cobrar (suma de ventas pendientes) y totales del reporte de
ganancias: ambos deben cubrir todas las ventas, no solo la pagina devuelta."""

from datetime import date
from decimal import Decimal

from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.sales import SaleService
from tests.fixtures_http import _auth


def _sale(customer, product, pending, price="40.00"):
    return SaleCreate(
        customerId=customer.id,
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=product.id, quantity=Decimal("1"), unitPrice=Decimal(price), pricingExceptionReason="precio de prueba")],
        isPaymentPending=pending,
    )


def _seed(db_session, customer, product, user):
    service = SaleService(db_session)
    service.create(_sale(customer, product, pending=True, price="40.00"), user_id=user.id)
    service.create(_sale(customer, product, pending=True, price="45.00"), user_id=user.id)
    service.create(_sale(customer, product, pending=False, price="50.00"), user_id=user.id)
    return service


def test_sum_total_respects_the_list_filters(db_session, seeded_customer, seeded_product_with_lot, seeded_user):
    service = _seed(db_session, seeded_customer, seeded_product_with_lot, seeded_user)

    assert service.sum_total(is_payment_pending=True) == Decimal("85.00")
    assert service.sum_total(is_payment_pending=False) == Decimal("50.00")
    assert service.sum_total() == Decimal("135.00")
    assert service.sum_total(start_date=date(2026, 10, 1)) == Decimal("0")


def test_sales_list_meta_has_the_amount_of_the_whole_filter(
    client, db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    _seed(db_session, seeded_customer, seeded_product_with_lot, seeded_user)

    resp = client.get("/api/v1/sales?is_payment_pending=true&limit=1", headers=_auth(seeded_user))

    meta = resp.json()["meta"]
    assert len(resp.json()["data"]) == 1
    assert meta["total"] == 2
    assert Decimal(meta["total_amount"]) == Decimal("85.00")


def test_dashboard_reports_pending_payments(
    client, db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    _seed(db_session, seeded_customer, seeded_product_with_lot, seeded_user)

    body = client.get("/api/v1/dashboard/summary", headers=_auth(seeded_user)).json()

    assert body["pendingPaymentsCount"] == 2
    assert Decimal(body["pendingPaymentsTotal"]) == Decimal("85.00")


def test_profit_totals_cover_rows_beyond_the_limit(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = _seed(db_session, seeded_customer, seeded_product_with_lot, seeded_user)

    report = service.get_profit_report(group_by="sale", limit=1)

    assert len(report.data) == 1
    assert report.totals.quantity == Decimal("3")
    assert report.totals.revenue == Decimal("135.00")
    assert report.totals.gross_profit == sum(
        (r.gross_profit for r in service.get_profit_report(group_by="sale").data), Decimal("0")
    )
