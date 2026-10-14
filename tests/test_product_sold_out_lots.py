"""Los lotes ya vendidos por completo quedan como historial del producto, sin
mezclarse con los lotes que todavia se pueden vender."""

from datetime import date
from decimal import Decimal

from app.models.purchase import PurchaseItem
from app.services.product_service import ProductService
from tests.fixtures_domain import _seed_product_with_one_confirmed_lot


def test_sold_out_lots_are_listed_apart_from_the_available_ones(
    db_session, seeded_user
):
    product = _seed_product_with_one_confirmed_lot(
        db_session,
        seeded_user,
        quantity=Decimal("3"),
        unit_cost=Decimal("10.00"),
        purchase_date=date(2026, 1, 10),
    )
    lot = db_session.query(PurchaseItem).filter_by(product_id=product.id).one()
    lot.remaining_quantity = Decimal("0")
    db_session.commit()

    availability = ProductService(db_session).get_lots_availability(
        product.id, as_of_date=date(2026, 1, 15)
    )

    assert availability.lots == []
    [sold_out] = availability.sold_out_lots
    assert sold_out.purchase_item_id == lot.id
    assert sold_out.purchase_date == date(2026, 1, 10)
    assert sold_out.quantity == Decimal("3")
    assert sold_out.unit_cost == Decimal("10.00")


def test_a_sale_item_says_which_purchase_its_lot_came_from(
    db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    from app.models.purchase import PurchaseItem
    from app.schemas.sale import SaleCreate, SaleItemCreate
    from app.services.sales import SaleService

    service = SaleService(db_session)
    sale = service.create(
        SaleCreate(
            customerId=seeded_customer.id,
            date=date(2026, 9, 24),
            items=[
                SaleItemCreate(
                    productId=seeded_product_with_lot.id, quantity=Decimal("1")
                )
            ],
        ),
        user_id=seeded_user.id,
    )
    lot = (
        db_session.query(PurchaseItem)
        .filter_by(product_id=seeded_product_with_lot.id)
        .one()
    )

    [item] = service.get_by_id_enriched(sale.id).items
    [allocation] = item.allocations
    assert allocation.purchase_item_id == lot.id
    assert allocation.purchase_id == lot.purchase_id
