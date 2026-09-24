"""preview_sale y create no deben poder divergir en el costo FIFO.

Task 4 extrajo allocate_fifo (redondea una sola vez, al final) para el camino
de create, pero preview_sale conservo el bucle duplicado que redondea el
acumulado en cada iteracion. En ventas fraccionarias (medios cartones) eso
produce un centavo de diferencia entre lo que el panel previsualiza y lo que
create realmente guarda -- y si el panel reenvia el precio previsualizado al
confirmar, create puede rechazar una venta valida con 422.
"""

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.schemas.sale import SaleCreate, SaleItemCreate
from app.services.sales.orchestrator import SaleService


def _lot(remaining_quantity, unit_cost, purchase_date=date(2026, 1, 1)):
    return SimpleNamespace(
        id=uuid.uuid4(),
        purchase_id=uuid.uuid4(),
        purchase=SimpleNamespace(date=purchase_date),
        remaining_quantity=remaining_quantity,
        unit_cost=unit_cost,
    )


def _product(stock=Decimal("100")):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Carton de huevos",
        sku="LKCX-002",
        stock=stock,
        status="active",
        earning_mode="percent",
        earning_percent=Decimal("20"),
        earning_fee_amount=None,
    )


def _service(lots, product):
    service = SaleService(db=None)
    service.customer_repo.get_by_id = lambda cid: SimpleNamespace(id=cid, name="Ana")
    service.product_repo.get_by_id = lambda pid: product
    service.purchase_repo.get_fifo_available_lots = lambda **kwargs: lots
    service.sale_repo.get_recent_items_for_customer_product = lambda **kwargs: []
    service.sale_repo.create = lambda sale: sale
    service._update_cycle = lambda *a, **kw: None
    return service


@pytest.mark.parametrize(
    "lots, quantity, expected_cost",
    [
        # Un solo lote de 34.17, medio carton -> sin division ambigua.
        ([_lot(Decimal("6"), Decimal("34.17"))], Decimal("0.5"), Decimal("34.17")),
        # Medio carton a 33.33 + un carton a 33.50: (16.665 + 33.50) / 1.5 = 33.4433...
        (
            [_lot(Decimal("0.5"), Decimal("33.33")), _lot(Decimal("6"), Decimal("33.50"))],
            Decimal("1.5"),
            Decimal("33.44"),
        ),
    ],
)
def test_preview_and_create_agree_on_cost_basis_unit(lots, quantity, expected_cost):
    product = _product()
    service = _service(lots, product)

    data = SaleCreate(
        customerId=uuid.uuid4(),
        date=date(2026, 9, 23),
        items=[SaleItemCreate(productId=product.id, quantity=quantity)],
    )

    preview = service.preview_sale(data)
    assert preview.items[0].cost_basis_unit == expected_cost

    sale = service.create(data, user_id=uuid.uuid4())
    assert sale.items[0].cost_basis_unit == expected_cost
