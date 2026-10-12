"""`last_purchase_date` es la compra mas reciente, no la ultima registrada.

Una venta olvidada que se registra tarde (fecha anterior a la ultima compra)
pisaba `last_purchase_date` con su fecha vieja. La regla de seguimientos
("una venta cierra el seguimiento solo si es posterior a la ultima compra")
depende de este campo.
"""

from datetime import date
from decimal import Decimal

from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.sales import SaleService


def _sell(service, customer, product, user, day, quantity="1"):
    return service.create(
        SaleCreate(
            customerId=customer.id,
            date=day,
            items=[SaleItemCreate(productId=product.id, quantity=Decimal(quantity))],
        ),
        user_id=user.id,
    )


def test_a_backdated_sale_does_not_move_last_purchase_back(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user
):
    service = SaleService(db_session)
    _sell(
        service,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 10),
    )
    _sell(
        service,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 17),
        "2",
    )

    _sell(
        service,
        seeded_customer,
        seeded_product_with_lot,
        seeded_user,
        date(2026, 9, 12),
    )

    cycle = service.cycle_repo.get_by_customer_and_product(
        seeded_customer.id, seeded_product_with_lot.id
    )
    assert cycle.last_purchase_date == date(2026, 9, 17)
    assert cycle.last_quantity == Decimal("2")
    assert cycle.total_purchases == 3
