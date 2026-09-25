"""Las tres herramientas de escritura: cada una se detiene con `interrupt()`
y, al aprobar, recalcula dentro de la misma sesion que va a escribir antes
de escribir.

`interrupt()` se monkeypatchea directamente en `app.agent.tools.write`: estos
tests no montan un checkpointer real de LangGraph, solo prueban que la
funcion de la herramienta hace lo correcto para cada respuesta posible
("aprobar", "corregir", "cancelar", cualquier otra cosa).
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.agent.tools.write import (
    registrar_compra,
    registrar_movimiento_caja,
    registrar_venta,
)
from app.models.cash_movement import CashMovement, CashMovementType
from app.models.product import Product
from app.models.purchase import Purchase, PurchaseItem
from app.models.sale import Sale


def _config(user):
    return {"configurable": {"user_id": str(user.id)}}


def _sale_payload(customer, product, **overrides):
    payload = {
        "cliente_id": str(customer.id),
        "items": [{"producto_id": str(product.id), "cantidad": "2"}],
        "fecha": "2026-09-24",
    }
    payload.update(overrides)
    return payload


def _consume_the_lot(db_session, product):
    """Simula que otra venta se llevo el lote entero entre el preview y la
    aprobacion: agota `remaining_quantity` y el stock del producto, tal como
    quedarian despues de una venta real de todo lo disponible."""
    item = db_session.query(PurchaseItem).filter_by(product_id=product.id).one()
    item.remaining_quantity = Decimal("0")
    db_session.query(Product).filter_by(id=product.id).update({"stock": Decimal("0")})
    db_session.commit()


# ---------------------------------------------------------------------
# registrar_venta
# ---------------------------------------------------------------------


def test_a_sale_whose_lots_were_consumed_asks_again_instead_of_writing(
    db_session, seeded_customer, seeded_product_with_one_lot, seeded_user, monkeypatch
):
    """El preview mostro un lote a Q10.00.  Antes de aprobar, otra venta se lo
    llevo.  Escribir con el costo viejo seria registrar algo que no paso.

    El monkeypatch de `interrupt` simula exactamente ese momento: la
    herramienta ya calculo y mostro el preview (Q10.00, el `payload` no
    cambia), y es AL REANUDAR -- es decir, dentro de esta funcion que hace
    de `interrupt()` -- que otra venta se lleva el lote y recien despues
    llega la aprobacion. Consumir el lote antes de invocar la herramienta
    seria trampa: el preview inicial de la propia herramienta ya veria el
    lote vacio, y el test no probaria nada."""
    payload = _sale_payload(seeded_customer, seeded_product_with_one_lot)

    def consume_the_lot_then_approve(_payload):
        _consume_the_lot(db_session, seeded_product_with_one_lot)
        return {"accion": "aprobar"}

    monkeypatch.setattr("app.agent.tools.write.interrupt", consume_the_lot_then_approve)

    result = registrar_venta.invoke(payload, config=_config(seeded_user))

    assert result["estado"] == "recalculado"
    assert db_session.query(Sale).count() == 0
    assert "difiere" in result["mensaje"].lower()
    # El costo anterior (Q10.00, lote intacto) y el actual (sin lote, otra
    # cifra o una advertencia de insuficiencia) deben venir los dos, para que
    # el agente pueda explicar que cambio.
    assert result["anterior"]["items"][0]["cost_basis_unit"] == "10.00"
    assert result["actual"]["items"][0]["cost_basis_unit"] != "10.00"


def test_an_approved_sale_writes_exactly_what_was_shown(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})

    result = registrar_venta.invoke(
        _sale_payload(seeded_customer, seeded_product_with_lot), config=_config(seeded_user)
    )

    sale = db_session.query(Sale).one()
    assert result["estado"] == "registrado"
    assert result["venta_id"] == str(sale.id)
    assert sale.user_id == seeded_user.id
    assert sale.items[0].cost_basis_unit == Decimal(result["preview"]["items"][0]["cost_basis_unit"])
    # La forma es la misma que previsualizar_venta: "lotes", no "allocations".
    assert "lotes" in result["preview"]["items"][0]
    assert "allocations" not in result["preview"]["items"][0]
    # La venta quedo pagada (cash hook de Tasks 4/5) -- esta herramienta no
    # duplica ese movimiento.
    movements = db_session.query(CashMovement).filter_by(sale_id=sale.id).all()
    assert len(movements) == 1
    assert movements[0].type == CashMovementType.ENTRADA


def test_a_cancelled_sale_writes_nothing(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "cancelar"})

    result = registrar_venta.invoke(
        _sale_payload(seeded_customer, seeded_product_with_lot), config=_config(seeded_user)
    )

    assert result["estado"] == "cancelado"
    assert db_session.query(Sale).count() == 0


def test_an_unrecognized_decision_writes_nothing(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """Cualquier respuesta que no sea aprobar/corregir/cancelar se trata como
    si no hubiera aprobacion -- nunca se escribe por default."""
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "que-es-esto"})

    result = registrar_venta.invoke(
        _sale_payload(seeded_customer, seeded_product_with_lot), config=_config(seeded_user)
    )

    assert result["estado"] == "cancelado"
    assert db_session.query(Sale).count() == 0


def test_correcting_re_previews_with_the_new_values_before_writing(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """'corregir' rearma con los valores nuevos y vuelve a mostrar -- no
    reusa el preview viejo, porque cambiar la cantidad cambia de que lotes
    sale y a que costo."""
    decisions = iter(
        [
            {"accion": "corregir", "valores": {"items": [{"producto_id": str(seeded_product_with_lot.id), "cantidad": "3"}]}},
            {"accion": "aprobar"},
        ]
    )
    seen_previews = []

    def fake_interrupt(payload):
        seen_previews.append(payload["preview"])
        return next(decisions)

    monkeypatch.setattr("app.agent.tools.write.interrupt", fake_interrupt)

    result = registrar_venta.invoke(
        _sale_payload(seeded_customer, seeded_product_with_lot, items=[{"producto_id": str(seeded_product_with_lot.id), "cantidad": "2"}]),
        config=_config(seeded_user),
    )

    assert result["estado"] == "registrado"
    # Dos previews mostrados: cantidad 2 (original) y cantidad 3 (corregida).
    assert seen_previews[0]["items"][0]["requested_quantity"] == "2"
    assert seen_previews[1]["items"][0]["requested_quantity"] == "3"
    sale = db_session.query(Sale).one()
    assert sale.items[0].quantity == Decimal("3")


def test_registrar_venta_writes_nothing_without_reaching_an_approval(
    db_session, seeded_customer, seeded_product_with_lot, monkeypatch
):
    """Sin user_id en el config, la rama de aprobar reventaria en
    user_id_from_config -- pero eso solo pasa DESPUES de recalcular y
    encontrar que coincide. Este test fija que, si el que aprueba no esta
    autenticado, la excepcion sale sin haber escrito la venta."""
    from app.agent.auth import AgentAuthError

    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})

    with pytest.raises(AgentAuthError):
        registrar_venta.invoke(_sale_payload(seeded_customer, seeded_product_with_lot))

    assert db_session.query(Sale).count() == 0


# ---------------------------------------------------------------------
# registrar_compra
# ---------------------------------------------------------------------


def _purchase_payload(product, **overrides):
    payload = {
        "items": [{"producto_id": str(product.id), "cantidad": "5", "costo_unitario": "12.50"}],
        "fecha": "2026-09-24",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def draft_product(db_session):
    product = Product(sku=f"SKU-{uuid.uuid4().hex[:10]}", name="Producto para compra", stock=Decimal("0"))
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def test_an_approved_purchase_is_created_and_confirmed_in_one_go(
    db_session, draft_product, seeded_user, monkeypatch
):
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})

    result = registrar_compra.invoke(_purchase_payload(draft_product), config=_config(seeded_user))

    assert result["estado"] == "registrado"
    purchase = db_session.query(Purchase).one()
    assert str(purchase.id) == result["compra_id"]
    assert purchase.status == "confirmed"

    db_session.refresh(draft_product)
    assert draft_product.stock == Decimal("5")

    item = db_session.query(PurchaseItem).filter_by(purchase_id=purchase.id).one()
    assert item.remaining_quantity == Decimal("5")

    # confirm() ya registra la salida de caja -- esta herramienta no duplica.
    movements = db_session.query(CashMovement).filter_by(purchase_id=purchase.id).all()
    assert len(movements) == 1
    assert movements[0].type == CashMovementType.SALIDA
    assert movements[0].amount == purchase.total


def test_a_cancelled_purchase_writes_nothing(db_session, draft_product, seeded_user, monkeypatch):
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "cancelar"})

    result = registrar_compra.invoke(_purchase_payload(draft_product), config=_config(seeded_user))

    assert result["estado"] == "cancelado"
    assert db_session.query(Purchase).count() == 0


def test_a_purchase_whose_product_was_deactivated_asks_again_instead_of_writing(
    db_session, draft_product, seeded_user, monkeypatch
):
    """El producto que se iba a comprar se desactivo entre el preview y la
    aprobacion -- confirmar igual dejaria un lote de un producto inactivo."""
    payload = _purchase_payload(draft_product)

    def deactivate_and_approve(_payload):
        draft_product.status = "inactive"
        db_session.commit()
        return {"accion": "aprobar"}

    monkeypatch.setattr("app.agent.tools.write.interrupt", deactivate_and_approve)

    result = registrar_compra.invoke(payload, config=_config(seeded_user))

    assert result["estado"] == "recalculado"
    assert db_session.query(Purchase).count() == 0
    assert result["anterior"]["items"][0]["producto_activo"] is True
    assert result["actual"]["items"][0]["producto_activo"] is False


def test_correcting_a_purchase_re_previews_before_writing(db_session, draft_product, seeded_user, monkeypatch):
    decisions = iter(
        [
            {"accion": "corregir", "valores": {"items": [{"producto_id": str(draft_product.id), "cantidad": "5", "costo_unitario": "20.00"}]}},
            {"accion": "aprobar"},
        ]
    )
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: next(decisions))

    result = registrar_compra.invoke(_purchase_payload(draft_product), config=_config(seeded_user))

    assert result["estado"] == "registrado"
    purchase = db_session.query(Purchase).one()
    assert purchase.total == Decimal("100.00")  # 5 * 20.00, no 5 * 12.50


# ---------------------------------------------------------------------
# registrar_movimiento_caja
# ---------------------------------------------------------------------


def test_an_approved_cash_movement_is_recorded(db_session, seeded_user, monkeypatch):
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})

    result = registrar_movimiento_caja.invoke(
        {
            "tipo": "aporte_socio",
            "monto": "500.00",
            "fecha": "2026-09-24",
            "nota": "Aporte del socio",
        },
        config=_config(seeded_user),
    )

    assert result["estado"] == "registrado"
    movement = db_session.query(CashMovement).one()
    assert str(movement.id) == result["movimiento_id"]
    assert movement.type == CashMovementType.APORTE_SOCIO
    assert movement.amount == Decimal("500.00")
    assert movement.note == "Aporte del socio"


def test_a_cash_movement_can_be_linked_to_a_purchase(db_session, seeded_purchase_draft, seeded_user, monkeypatch):
    """El aporte del socio a una compra especifica -- no es automatico, lo
    pide el agente por separado, y esta herramienta lo asocia via compra_id."""
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})

    result = registrar_movimiento_caja.invoke(
        {
            "tipo": "aporte_socio",
            "monto": "60.00",
            "fecha": "2026-09-20",
            "compra_id": str(seeded_purchase_draft.id),
        },
        config=_config(seeded_user),
    )

    assert result["estado"] == "registrado"
    movement = db_session.query(CashMovement).filter_by(purchase_id=seeded_purchase_draft.id).one()
    assert movement.type == CashMovementType.APORTE_SOCIO
    assert movement.amount == Decimal("60.00")


def test_a_cancelled_cash_movement_writes_nothing(db_session, seeded_user, monkeypatch):
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "cancelar"})

    result = registrar_movimiento_caja.invoke(
        {"tipo": "retiro_socio", "monto": "50.00", "fecha": "2026-09-24"},
        config=_config(seeded_user),
    )

    assert result["estado"] == "cancelado"
    assert db_session.query(CashMovement).count() == 0


def test_correcting_a_cash_movement_uses_the_new_amount(db_session, seeded_user, monkeypatch):
    decisions = iter(
        [
            {"accion": "corregir", "valores": {"monto": "75.00"}},
            {"accion": "aprobar"},
        ]
    )
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: next(decisions))

    result = registrar_movimiento_caja.invoke(
        {"tipo": "aporte_socio", "monto": "50.00", "fecha": "2026-09-24"},
        config=_config(seeded_user),
    )

    assert result["estado"] == "registrado"
    movement = db_session.query(CashMovement).one()
    assert movement.amount == Decimal("75.00")


# ---------------------------------------------------------------------
# Ninguna herramienta escribe sin pasar por una aprobacion
# ---------------------------------------------------------------------


def test_write_tools_source_never_commits_before_interrupt():
    """Sanity check de texto: `interrupt(` debe aparecer antes de cualquier
    `service.create(`/`service.confirm(`/`.record(` en cada funcion. No
    reemplaza los tests de comportamiento de arriba -- es una red adicional
    contra un futuro refactor que mueva la escritura antes de la pausa."""
    import inspect

    from app.agent.tools import write as write_module

    for fn in (registrar_venta, registrar_compra, registrar_movimiento_caja):
        source = inspect.getsource(fn.func)
        interrupt_pos = source.index("interrupt(")
        for write_call in ("service.create(", "service.confirm(", ".record("):
            if write_call in source:
                assert source.index(write_call) > interrupt_pos, (
                    f"{fn.name}: {write_call!r} aparece antes de interrupt()"
                )
