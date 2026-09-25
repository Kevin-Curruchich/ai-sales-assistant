"""Las herramientas de consulta no pueden escribir y no eligen por el usuario."""

from decimal import Decimal

from app.agent.tools.read import (
    buscar_cliente,
    consultar_caja,
    consultar_seguimiento,
    previsualizar_venta,
)
from app.models.sale import Sale


def test_buscar_cliente_returns_every_match_without_choosing(db_session, two_similar_customers):
    result = buscar_cliente.invoke({"nombre": "Gonzalez"})
    assert len(result["clientes"]) == 2
    # No hay campo que elija uno: la desambiguacion es del agente, hablando.
    assert "seleccionado" not in result


def test_buscar_cliente_with_no_match_says_so(db_session):
    result = buscar_cliente.invoke({"nombre": "nadie-con-este-nombre"})
    assert result["clientes"] == []


def test_previsualizar_venta_returns_lots_and_cost(db_session, seeded_customer, seeded_product_with_lot):
    result = previsualizar_venta.invoke({
        "cliente_id": str(seeded_customer.id),
        "items": [{"producto_id": str(seeded_product_with_lot.id), "cantidad": "0.5"}],
        "fecha": "2026-09-24",
    })
    item = result["items"][0]
    assert item["cost_basis_unit"] is not None
    assert item["lotes"]
    assert "is_habitual_price" in item


def test_previsualizar_venta_writes_nothing(db_session, seeded_customer, seeded_product_with_lot):
    before = db_session.query(Sale).count()
    previsualizar_venta.invoke({
        "cliente_id": str(seeded_customer.id),
        "items": [{"producto_id": str(seeded_product_with_lot.id), "cantidad": "0.5"}],
        "fecha": "2026-09-24",
    })
    assert db_session.query(Sale).count() == before


def test_previsualizar_venta_reports_insufficient_lots(db_session, seeded_customer, seeded_product_with_one_lot):
    # El lote solo tiene 3 unidades; pedir 5 debe volver con advertencia, no reventar.
    result = previsualizar_venta.invoke({
        "cliente_id": str(seeded_customer.id),
        "items": [{"producto_id": str(seeded_product_with_one_lot.id), "cantidad": "5"}],
        "fecha": "2026-09-24",
    })
    item = result["items"][0]
    assert item["warnings"]


def test_consultar_seguimiento_unpacks_the_tuple_into_items_and_total(db_session):
    result = consultar_seguimiento.invoke({})
    assert result["seguimientos"] == []
    assert result["total"] == 0


def test_consultar_caja_returns_plain_decimals_as_strings(db_session):
    result = consultar_caja.invoke({})
    assert result["saldo"] == "0.00"
    assert result["saldo_socio"] == "0.00"
    assert result["movimientos"] == []


def test_read_tools_source_never_writes_to_the_db():
    # Chequeo honesto sobre el texto fuente: ninguna herramienta de esta task
    # llama db.add / db.commit / db.delete -- esas operaciones quedan para
    # las herramientas de escritura de la Task 8.
    import app.agent.tools.read as mod

    source = mod.__file__
    with open(source) as f:
        text = f.read()

    for forbidden in ("db.add(", "db.commit(", "db.delete(", ".commit()"):
        assert forbidden not in text, f"{forbidden!r} no deberia aparecer en una herramienta de solo lectura"
