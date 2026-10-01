"""El listado de ventas filtra por estado de pago y por producto, y el total
de la paginacion (`count`) respeta los mismos filtros que la pagina."""

from datetime import date
from decimal import Decimal

from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.sales import SaleService


def _sale(customer, product, pending):
    return SaleCreate(
        customerId=customer.id,
        date=date(2026, 9, 24),
        items=[SaleItemCreate(productId=product.id, quantity=Decimal("1"))],
        isPaymentPending=pending,
    )


def test_filters_by_payment_status(db_session, seeded_customer, seeded_product_with_lot, seeded_user):
    service = SaleService(db_session)
    pending = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)
    paid = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=False), user_id=seeded_user.id)

    assert [s.id for s in service.get_all(is_payment_pending=True)] == [pending.id]
    assert [s.id for s in service.get_all(is_payment_pending=False)] == [paid.id]
    assert service.count(is_payment_pending=True) == 1
    assert service.count(is_payment_pending=False) == 1
    # Sin filtro trae ambas: `None` no es lo mismo que `False`.
    assert service.count() == 2


def test_combines_product_and_payment_status(
    db_session, seeded_customer, seeded_product_with_lot, seeded_product_with_one_lot, seeded_user
):
    service = SaleService(db_session)
    target = service.create(_sale(seeded_customer, seeded_product_with_lot, pending=True), user_id=seeded_user.id)
    service.create(_sale(seeded_customer, seeded_product_with_lot, pending=False), user_id=seeded_user.id)
    service.create(_sale(seeded_customer, seeded_product_with_one_lot, pending=True), user_id=seeded_user.id)

    filters = dict(product_id=seeded_product_with_lot.id, is_payment_pending=True)
    assert [s.id for s in service.get_all(**filters)] == [target.id]
    assert service.count(**filters) == 1
