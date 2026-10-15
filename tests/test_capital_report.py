"""Donde esta el dinero, por producto: lo que queda en inventario a costo real
y lo que se vendio sin cobrar todavia."""

from datetime import date
from decimal import Decimal

from app.models.purchase import PurchaseItem
from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.product_service import ProductService
from app.services.sales import SaleService
from tests.fixtures_domain import _seed_product_with_one_confirmed_lot


def test_capital_report_splits_inventory_and_receivables_by_product(
    db_session, seeded_user, seeded_customer
):
    vendido = _seed_product_with_one_confirmed_lot(
        db_session,
        seeded_user,
        quantity=Decimal("4"),
        unit_cost=Decimal("10.00"),
        purchase_date=date(2026, 9, 1),
    )
    agotado = _seed_product_with_one_confirmed_lot(
        db_session,
        seeded_user,
        quantity=Decimal("2"),
        unit_cost=Decimal("5.00"),
        purchase_date=date(2026, 9, 1),
    )
    lot = db_session.query(PurchaseItem).filter_by(product_id=agotado.id).one()
    lot.remaining_quantity = Decimal("0")
    db_session.commit()

    SaleService(db_session).create(
        SaleCreate(
            customerId=seeded_customer.id,
            date=date(2026, 9, 24),
            items=[
                SaleItemCreate(
                    productId=vendido.id,
                    quantity=Decimal("1"),
                    unitPrice=Decimal("15.00"),
                    pricingExceptionReason="precio fijo del test",
                )
            ],
            isPaymentPending=True,
        ),
        user_id=seeded_user.id,
    )

    report = ProductService(db_session).get_capital_report()

    rows = {r.product_id: r for r in report.data}
    assert agotado.id not in rows
    assert rows[vendido.id].inventory_value == Decimal("30.00")
    assert rows[vendido.id].receivable == Decimal("15.00")
    assert report.totals.inventory_value == Decimal("30.00")
    assert report.totals.receivable == Decimal("15.00")
    # Mismo "por cobrar" que el dashboard (pendingPaymentsTotal).
    assert report.totals.receivable == SaleService(db_session).sum_total(
        is_payment_pending=True
    )
