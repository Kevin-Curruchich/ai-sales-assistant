"""Las cantidades fraccionarias deben funcionar de punta a punta.

El negocio vende medios cartones de huevo.  La migracion 003 promovio las
columnas a Numeric(10,4), pero la capa Pydantic seguia declarando int, lo que
rompia la lectura (500) y bloqueaba la escritura.
"""

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.product import ProductCreate, ProductResponse, ProductUpdate
from app.schemas.purchase import PurchaseItemCreate, PurchaseItemResponse
from app.schemas.sale import ProfitReportResponse, ProfitReportRow


# --- A: el reporte de ganancias ---

def test_profit_report_accumulates_fractional_quantities():
    """Reproduce el 500 en /sales/reports/profit.

    sale_service.get_profit_report hace `rows[key].quantity += item.quantity`
    con item.quantity Decimal.  Con quantity: int, FastAPI fallaba al validar
    la respuesta para cualquier rango que incluyera una venta de medio carton.
    """
    row = ProfitReportRow(
        key="k", label="Carton de huevos",
        quantity=Decimal("0"), revenue=Decimal("0.00"), gross_profit=Decimal("0.00"),
    )
    row.quantity += Decimal("0.5000")
    row.quantity += Decimal("1")

    # Esto es lo que hace FastAPI al serializar la respuesta.
    validated = ProfitReportResponse.model_validate({"data": [row.model_dump()]})

    assert validated.data[0].quantity == Decimal("1.5000")


# --- B: compras fraccionarias ---

def test_purchase_item_create_accepts_half_units():
    item = PurchaseItemCreate(
        productId=uuid.uuid4(), quantity=Decimal("0.5"), unitCost=Decimal("33.50")
    )
    assert item.quantity == Decimal("0.5")


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-0.5")])
def test_purchase_item_create_still_rejects_non_positive(bad):
    """Aceptar decimales no debe aflojar la validacion existente."""
    with pytest.raises(ValidationError):
        PurchaseItemCreate(
            productId=uuid.uuid4(), quantity=bad, unitCost=Decimal("33.50")
        )


def test_purchase_item_response_round_trips_a_fractional_quantity():
    payload = {
        "id": uuid.uuid4(), "product_id": uuid.uuid4(),
        "quantity": Decimal("0.5000"), "unit_cost": Decimal("33.50"),
        "subtotal": Decimal("16.75"), "product_name": "Carton de huevos",
        "product_sku": "LKCX-002", "product_earning_mode": "percent",
        "product_status": "active",
    }
    assert PurchaseItemResponse.model_validate(payload).quantity == Decimal("0.5000")


# --- C: min_stock ---

def test_min_stock_accepts_decimals_on_create_update_and_response():
    created = ProductCreate(
        sku="LKCX-002", name="Carton",
        earningPercent=Decimal("10"), min_stock=Decimal("2.5"),
    )
    assert created.min_stock == Decimal("2.5")

    updated = ProductUpdate(min_stock=Decimal("1.5"))
    assert updated.min_stock == Decimal("1.5")

    response = ProductResponse.model_validate({
        "id": uuid.uuid4(), "sku": "LKCX-002", "name": "Carton",
        "earning_mode": "percent", "stock": Decimal("5.5"),
        "min_stock": Decimal("2.5"), "status": "active",
        "created_at": "2026-09-22", "updated_at": "2026-09-22",
        "stock_alert_status": "ok", "should_reorder": False,
    })
    assert response.min_stock == Decimal("2.5")
