"""Las tres herramientas de escritura: cada una se detiene con `interrupt()`
y, al aprobar, recalcula dentro de la misma sesion que va a escribir antes
de escribir.

La mayoria de estos tests monkeypatchean `interrupt()` directamente en
`app.agent.tools.write` con un stand-in de una sola pasada -- eso alcanza
para probar que la herramienta hace lo correcto para cada respuesta posible
("aprobar", "corregir", "cancelar", cualquier otra cosa) SIN levantar un
checkpointer real. Pero ese stand-in modela una ejecucion que atraviesa la
pausa -- y la real de LangGraph no: al reanudar con `Command(resume=...)`,
LangGraph vuelve a correr la funcion entera desde el principio. Por eso, ADEMAS
de esos tests rapidos, hay uno (`test_a_real_checkpointer_resume_...`) que monta
un `InMemorySaver` real y reanuda de verdad -- es el unico que puede probar
que la guardia sobrevive a ese comportamiento de reanudacion, y es el que la
revision de la Task 8 (ronda 1) pidio explicitamente.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command

from app.agent.auth import AgentAuthError
from app.agent.tools.write import (
    _occurred_at,
    registrar_compra,
    registrar_movimiento_caja,
    registrar_venta,
)
from app.core.datetime_utils import business_tz
from app.models.cash_movement import CashMovement, CashMovementType
from app.models.product import Product
from app.models.purchase import Purchase, PurchaseItem
from app.models.sale import Sale
from app.repositories.product_repository import ProductRepository
from app.services.cash_service import CashService
from app.services.purchase_service import PurchaseService


def _config(user):
    return {"configurable": {"user_id": str(user.id)}}


def _approve(payload):
    """El panel real devuelve la huella tal cual la recibio -- estos tests
    hacen lo mismo en vez de omitirla, porque omitirla (o no compararla) es
    justo el bug que la ronda 1 de revision encontro (Finding C1)."""
    return {"accion": "aprobar", "huella": payload["huella"]}


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


def _consume_part_of_the_lot(db_session, product, quantity):
    """Simula que otra venta se lleva PARTE del lote (no todo): el costo
    unitario no cambia (sigue siendo el mismo lote, el mismo unit_cost), pero
    ya no alcanza para cubrir la cantidad pedida completa."""
    item = db_session.query(PurchaseItem).filter_by(product_id=product.id).one()
    item.remaining_quantity = item.remaining_quantity - quantity
    product_row = db_session.query(Product).filter_by(id=product.id).one()
    product_row.stock = product_row.stock - quantity
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

    def consume_the_lot_then_approve(p):
        _consume_the_lot(db_session, seeded_product_with_one_lot)
        return _approve(p)

    monkeypatch.setattr("app.agent.tools.write.interrupt", consume_the_lot_then_approve)

    result = registrar_venta.invoke(payload, config=_config(seeded_user))

    assert result["estado"] == "recalculado"
    assert db_session.query(Sale).count() == 0
    assert "difiere" in result["mensaje"].lower()
    # Lo aprobado (la huella congelada, Q10.00) y lo actual (sin lote) deben
    # venir los dos, para que el agente pueda explicar que cambio.
    assert result["aprobado"][0]["cost_basis_unit"] == "10.00"
    assert result["actual"]["items"][0]["cost_basis_unit"] != "10.00"


def test_a_sale_whose_lot_was_partially_consumed_asks_again_instead_of_leaking_an_exception(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """El lote tiene 6 unidades a Q33.33; se piden las 6.  Antes de aprobar,
    otra venta se lleva 1 sola -- no todas.  El costo unitario NO cambia
    (sigue siendo el mismo lote, el mismo unit_cost), y el subtotal se
    calcula sobre la cantidad PEDIDA (6), no la disponible, asi que tampoco
    cambia: comparar solo cost_basis_unit/subtotal no alcanzaba a detectar
    esto. Sin la huella extendida (lotes + warnings), `create()` -- que si
    bloquea la fila y ve que solo hay 5 -- rechazaba con una `HTTPException`
    sin manejar en vez de que esta herramienta devolviera "recalculado"
    (Finding I1)."""
    payload = _sale_payload(
        seeded_customer,
        seeded_product_with_lot,
        items=[{"producto_id": str(seeded_product_with_lot.id), "cantidad": "6"}],
    )

    def consume_one_unit_then_approve(p):
        _consume_part_of_the_lot(db_session, seeded_product_with_lot, Decimal("1"))
        return _approve(p)

    monkeypatch.setattr("app.agent.tools.write.interrupt", consume_one_unit_then_approve)

    result = registrar_venta.invoke(payload, config=_config(seeded_user))

    assert result["estado"] == "recalculado"
    assert db_session.query(Sale).count() == 0


def test_an_approved_sale_writes_exactly_what_was_shown(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    monkeypatch.setattr("app.agent.tools.write.interrupt", _approve)

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


def test_a_zero_unit_price_is_honored_not_treated_as_missing(
    db_session, seeded_customer, seeded_product_with_lot, seeded_user, monkeypatch
):
    """precio_unitario="0" (un regalo) es falsy en Python -- `is not None` en
    vez de una verdad booleana evita que se lea como "no vino precio" y se
    use el sugerido a precio completo en su lugar (Minor de la revision).

    `pago_pendiente=True`: un total de Q0 no puede pagarse -- CashService.record()
    exige un monto > 0 -- asi que esta venta se deja pendiente de cobro para
    aislar lo que el test prueba (el precio en 0) del hook de caja."""
    payload = _sale_payload(
        seeded_customer,
        seeded_product_with_lot,
        items=[{"producto_id": str(seeded_product_with_lot.id), "cantidad": "1", "precio_unitario": "0"}],
        pago_pendiente=True,
    )
    monkeypatch.setattr("app.agent.tools.write.interrupt", _approve)

    result = registrar_venta.invoke(payload, config=_config(seeded_user))

    assert result["estado"] == "registrado"
    sale = db_session.query(Sale).one()
    assert sale.items[0].unit_price == Decimal("0.00")


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
    seen_previews = []

    def fake_interrupt(payload):
        seen_previews.append(payload["preview"])
        if len(seen_previews) == 1:
            return {
                "accion": "corregir",
                "valores": {"items": [{"producto_id": str(seeded_product_with_lot.id), "cantidad": "3"}]},
            }
        return _approve(payload)

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
    monkeypatch.setattr("app.agent.tools.write.interrupt", _approve)

    with pytest.raises(AgentAuthError):
        registrar_venta.invoke(_sale_payload(seeded_customer, seeded_product_with_lot))

    assert db_session.query(Sale).count() == 0


def _registrar_venta_node(state, config):
    resultado = registrar_venta.func(
        cliente_id=state["cliente_id"],
        items=state["items"],
        fecha=state["fecha"],
        config=config,
    )
    return {"resultado": resultado}


def test_a_real_checkpointer_resume_still_catches_a_stale_approval(
    db_session, seeded_customer, seeded_product_with_one_lot, seeded_user
):
    """El resto de esta suite monkeypatchea `interrupt()` con un stand-in de
    una sola pasada, que MODELA una ejecucion que atraviesa la pausa. La
    ejecucion real de LangGraph no atraviesa nada: al reanudar con
    `Command(resume=...)`, corre la funcion entera desde el principio otra
    vez (ver el docstring de `app/agent/tools/write.py`). Este test es el
    unico que monta un `InMemorySaver` real y reanuda de verdad, para probar
    que la guardia sigue funcionando bajo ese comportamiento -- sin este
    test, ninguno de los demas puede probar que la propiedad central de la
    task (recalcular antes de escribir) hace algo (Finding C1)."""
    graph = StateGraph(dict)
    graph.add_node("registrar", _registrar_venta_node)
    graph.set_entry_point("registrar")
    graph.add_edge("registrar", END)
    app = graph.compile(checkpointer=InMemorySaver())

    thread = {"configurable": {"thread_id": str(uuid.uuid4()), "user_id": str(seeded_user.id)}}
    initial_state = {
        "cliente_id": str(seeded_customer.id),
        "items": [{"producto_id": str(seeded_product_with_one_lot.id), "cantidad": "3"}],
        "fecha": "2026-09-24",
    }

    paused = app.invoke(initial_state, config=thread)
    assert "__interrupt__" in paused
    payload = paused["__interrupt__"][0].value
    assert payload["preview"]["items"][0]["cost_basis_unit"] == "10.00"
    huella_mostrada = payload["huella"]

    # El grafo esta pausado de verdad, con un checkpoint guardado -- no una
    # funcion de Python congelada a medio ejecutar. Mientras espera, otra
    # venta se lleva el lote entero.
    _consume_the_lot(db_session, seeded_product_with_one_lot)

    resumed = app.invoke(
        Command(resume={"accion": "aprobar", "huella": huella_mostrada}), config=thread
    )

    assert resumed["resultado"]["estado"] == "recalculado"
    assert db_session.query(Sale).count() == 0


# ---------------------------------------------------------------------
# _occurred_at
# ---------------------------------------------------------------------


def test_occurred_at_bare_date_anchors_to_business_midnight():
    assert _occurred_at("2026-09-24") == datetime(2026, 9, 24, 0, 0, tzinfo=business_tz())


def test_occurred_at_with_explicit_timezone_is_used_as_is():
    assert _occurred_at("2026-09-24T23:30:00+00:00") == datetime(2026, 9, 24, 23, 30, tzinfo=timezone.utc)


def test_occurred_at_with_time_but_no_timezone_assumes_business_timezone():
    assert _occurred_at("2026-09-24T15:00:00") == datetime(2026, 9, 24, 15, 0, tzinfo=business_tz())


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
    monkeypatch.setattr("app.agent.tools.write.interrupt", _approve)

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


def test_a_purchase_with_fractional_rounding_matches_what_is_actually_written(
    db_session, draft_product, seeded_user, monkeypatch
):
    """Q2.005 por unidad: `Decimal.quantize()` con el redondeo por default
    (ROUND_HALF_EVEN) da Q2.00; `PurchaseService._money` (ROUND_HALF_UP, lo
    que create() en verdad escribe) da Q2.01. Si `_purchase_preview` usa un
    criterio distinto al de create(), la tarjeta de confirmacion le miente a
    quien aprueba por un centavo -- exactamente el desacuerdo entre preview y
    create() que esta task existe para prevenir, reintroducido del lado de
    la compra (Finding I2)."""
    seen = {}

    def approve_and_capture(payload):
        seen["preview"] = payload["preview"]
        return _approve(payload)

    monkeypatch.setattr("app.agent.tools.write.interrupt", approve_and_capture)

    payload = _purchase_payload(
        draft_product, items=[{"producto_id": str(draft_product.id), "cantidad": "1", "costo_unitario": "2.005"}]
    )
    result = registrar_compra.invoke(payload, config=_config(seeded_user))

    assert result["estado"] == "registrado"
    item = db_session.query(PurchaseItem).filter_by(product_id=draft_product.id).one()
    assert item.subtotal == Decimal("2.01")
    # Lo que se le mostro a quien aprobo, ANTES de escribir, tiene que
    # coincidir con lo que create() en verdad cobro.
    assert seen["preview"]["items"][0]["subtotal"] == "2.01"


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

    def deactivate_and_approve(p):
        draft_product.status = "inactive"
        db_session.commit()
        return _approve(p)

    monkeypatch.setattr("app.agent.tools.write.interrupt", deactivate_and_approve)

    result = registrar_compra.invoke(payload, config=_config(seeded_user))

    assert result["estado"] == "recalculado"
    assert db_session.query(Purchase).count() == 0
    assert result["aprobado"]["items"][0]["producto_activo"] is True
    assert result["actual"]["items"][0]["producto_activo"] is False


def test_correcting_a_purchase_re_previews_before_writing(db_session, draft_product, seeded_user, monkeypatch):
    seen = {"n": 0}

    def fake_interrupt(payload):
        seen["n"] += 1
        if seen["n"] == 1:
            return {
                "accion": "corregir",
                "valores": {"items": [{"producto_id": str(draft_product.id), "cantidad": "5", "costo_unitario": "20.00"}]},
            }
        return _approve(payload)

    monkeypatch.setattr("app.agent.tools.write.interrupt", fake_interrupt)

    result = registrar_compra.invoke(_purchase_payload(draft_product), config=_config(seeded_user))

    assert result["estado"] == "registrado"
    purchase = db_session.query(Purchase).one()
    assert purchase.total == Decimal("100.00")  # 5 * 20.00, no 5 * 12.50


def test_confirm_failure_with_partial_stock_already_applied_rolls_back_before_deleting_the_draft(
    db_session, seeded_user, monkeypatch
):
    """confirm() incrementa el stock item por item.  Si falla procesando el
    SEGUNDO item, el stock del PRIMERO ya quedo incrementado EN LA SESION,
    sin commitear, y purchase.status todavia no se puso en "confirmed".  Sin
    un rollback antes de borrar el borrador, PurchaseRepository.delete()
    (que hace su propio commit()) se lleva puesto ese incremento a medias:
    stock fantasma, sin lote ni compra que lo explique (Finding C2, primer
    caso)."""
    p1 = Product(sku=f"SKU-{uuid.uuid4().hex[:10]}", name="Producto 1", stock=Decimal("0"))
    p2 = Product(sku=f"SKU-{uuid.uuid4().hex[:10]}", name="Producto 2", stock=Decimal("0"))
    db_session.add_all([p1, p2])
    db_session.commit()
    db_session.refresh(p1)
    db_session.refresh(p2)

    payload = {
        "items": [
            {"producto_id": str(p1.id), "cantidad": "3", "costo_unitario": "10.00"},
            {"producto_id": str(p2.id), "cantidad": "2", "costo_unitario": "5.00"},
        ],
        "fecha": "2026-09-24",
    }
    monkeypatch.setattr("app.agent.tools.write.interrupt", _approve)

    # Simula que el producto 2 desaparece justo cuando confirm() llega a su
    # item, DESPUES de que el item de p1 ya incremento stock en la sesion.
    # Se instala solo dentro de confirm() (no afecta el preview ni
    # create()->_validate_items, que corren antes con el metodo real).
    original_confirm = PurchaseService.confirm

    def confirm_with_product_2_vanishing_mid_loop(self, purchase_id):
        original_get_by_id = self.product_repo.get_by_id
        seen = {"n": 0}

        def flaky(product_id):
            seen["n"] += 1
            if seen["n"] == 2:
                return None
            return original_get_by_id(product_id)

        self.product_repo.get_by_id = flaky
        return original_confirm(self, purchase_id)

    monkeypatch.setattr(PurchaseService, "confirm", confirm_with_product_2_vanishing_mid_loop)

    with pytest.raises(HTTPException):
        registrar_compra.invoke(payload, config=_config(seeded_user))

    assert db_session.query(Purchase).count() == 0
    assert db_session.query(PurchaseItem).count() == 0
    db_session.refresh(p1)
    db_session.refresh(p2)
    assert p1.stock == Decimal("0")  # sin el rollback, quedaria en 3: stock fantasma.
    assert p2.stock == Decimal("0")


def test_confirm_failure_after_status_was_set_rolls_back_before_deleting_the_draft(
    db_session, draft_product, seeded_user, monkeypatch
):
    """confirm() pone purchase.status = "confirmed" EN LA SESION antes de
    registrar la salida de caja.  Si CashService.record() explota justo ahi,
    sin un rollback antes de borrar, delete() ve ese "confirmed" sin
    commitear y lo rechaza con 409 -- el borrador queda colgado Y el error
    real queda tapado por el 409 (Finding C2, segundo caso)."""
    payload = _purchase_payload(draft_product)
    monkeypatch.setattr("app.agent.tools.write.interrupt", _approve)

    def explode(self, *args, **kwargs):
        raise RuntimeError("caja caida")

    monkeypatch.setattr(CashService, "record", explode)

    with pytest.raises(RuntimeError):
        registrar_compra.invoke(payload, config=_config(seeded_user))

    assert db_session.query(Purchase).count() == 0
    db_session.refresh(draft_product)
    assert draft_product.stock == Decimal("0")


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


def test_registrar_movimiento_caja_requires_an_authenticated_user(db_session, monkeypatch):
    """CashMovement no tiene columna user_id -- la unica forma de dejar un
    rastro (y de impedir que un hilo sin autenticar escriba un aporte_socio
    de Q500) es rechazar la escritura si el config no trae un user_id
    valido (Finding I3)."""
    monkeypatch.setattr("app.agent.tools.write.interrupt", lambda _p: {"accion": "aprobar"})

    with pytest.raises(AgentAuthError):
        registrar_movimiento_caja.invoke({"tipo": "aporte_socio", "monto": "500.00", "fecha": "2026-09-24"})

    assert db_session.query(CashMovement).count() == 0


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

    for fn in (registrar_venta, registrar_compra, registrar_movimiento_caja):
        source = inspect.getsource(fn.func)
        interrupt_pos = source.index("interrupt(")
        for write_call in ("service.create(", "service.confirm(", ".record("):
            if write_call in source:
                assert source.index(write_call) > interrupt_pos, (
                    f"{fn.name}: {write_call!r} aparece antes de interrupt()"
                )
