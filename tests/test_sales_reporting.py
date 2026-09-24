from decimal import Decimal
from types import SimpleNamespace

from app.services.sales.reporting import build_profit_rows


def _item(product_id, name, quantity, subtotal, gross_profit_total):
    return SimpleNamespace(
        product_id=product_id,
        product=SimpleNamespace(name=name, sku=name[:4]),
        quantity=Decimal(quantity),
        subtotal=Decimal(subtotal),
        gross_profit_total=Decimal(gross_profit_total),
    )


def test_groups_by_product_and_sums_fractional_quantities():
    """Medio carton mas un carton son 1.5 unidades, no un error."""
    sales = [
        SimpleNamespace(items=[_item("p1", "Carton de huevos", "0.5", "18.00", "0.92")]),
        SimpleNamespace(items=[_item("p1", "Carton de huevos", "1", "37.00", "3.50")]),
    ]
    rows = build_profit_rows(sales, group_by="product")
    assert len(rows) == 1
    assert rows[0].quantity == Decimal("1.5")
    assert rows[0].revenue == Decimal("55.00")
    assert rows[0].gross_profit == Decimal("4.42")


def test_separate_products_get_separate_rows():
    sales = [
        SimpleNamespace(items=[
            _item("p1", "Carton de huevos", "1", "37.00", "3.50"),
            _item("p2", "Cilindro de gas", "1", "115.00", "20.00"),
        ])
    ]
    rows = build_profit_rows(sales, group_by="product")
    assert {r.quantity for r in rows} == {Decimal("1")}
    assert len(rows) == 2


def test_no_sales_gives_no_rows():
    assert build_profit_rows([], group_by="product") == []


def _sale(sale_id, sale_date, customer_id, customer_name, items):
    customer = SimpleNamespace(name=customer_name) if customer_name else None
    return SimpleNamespace(
        id=sale_id,
        date=sale_date,
        customer_id=customer_id,
        customer=customer,
        items=items,
    )


def test_groups_by_sale():
    """group_by='sale' sigue existiendo tras la extraccion (no solo 'product')."""
    sales = [
        _sale(
            "s1",
            "2026-01-01",
            "c1",
            "Ana",
            [_item("p1", "Carton de huevos", "1", "37.00", "3.50")],
        ),
        _sale(
            "s2",
            "2026-01-02",
            "c2",
            "Beto",
            [_item("p2", "Cilindro de gas", "1", "115.00", "20.00")],
        ),
    ]
    rows = build_profit_rows(sales, group_by="sale")
    assert {r.key for r in rows} == {"s1", "s2"}
    row1 = next(r for r in rows if r.key == "s1")
    assert row1.label == "2026-01-01 - Ana"
    assert row1.revenue == Decimal("37.00")


def test_groups_by_customer_merges_across_sales():
    sales = [
        _sale(
            "s1",
            "2026-01-01",
            "c1",
            "Ana",
            [_item("p1", "Carton de huevos", "1", "37.00", "3.50")],
        ),
        _sale(
            "s2",
            "2026-01-02",
            "c1",
            "Ana",
            [_item("p2", "Cilindro de gas", "1", "115.00", "20.00")],
        ),
    ]
    rows = build_profit_rows(sales, group_by="customer")
    assert len(rows) == 1
    assert rows[0].key == "c1"
    assert rows[0].label == "Ana"
    assert rows[0].revenue == Decimal("152.00")
    assert rows[0].gross_profit == Decimal("23.50")


def test_invalid_group_by_raises_a_domain_exception_not_http():
    """reporting.py es una superficie pura: nada de FastAPI en su interfaz.

    Un agente que llama build_profit_rows en proceso, sin HTTP de por medio,
    debe recibir una excepcion de dominio normal, no un HTTPException.
    """
    from app.services.sales.reporting import InvalidGroupBy

    sales = [
        _sale(
            "s1",
            "2026-01-01",
            "c1",
            "Ana",
            [_item("p1", "Carton de huevos", "1", "37.00", "3.50")],
        )
    ]
    try:
        build_profit_rows(sales, group_by="bogus")
        assert False, "expected InvalidGroupBy"
    except InvalidGroupBy as exc:
        assert exc.group_by == "bogus"
        assert exc.allowed == ("sale", "customer", "product")


def test_orchestrator_translates_invalid_group_by_to_422_with_the_same_detail():
    """El contrato HTTP se conserva: reporting.py es de dominio, la API sigue en 422."""
    from fastapi import HTTPException

    from app.services.sales.orchestrator import SaleService

    service = SaleService(db=None)
    service.sale_repo.get_all = lambda **kwargs: [
        _sale(
            "s1",
            "2026-01-01",
            "c1",
            "Ana",
            [_item("p1", "Carton de huevos", "1", "37.00", "3.50")],
        )
    ]

    try:
        service.get_profit_report(group_by="bogus")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 422
        assert exc.detail == "group_by must be one of: sale, customer, product"
