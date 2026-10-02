"""Las herramientas de consulta no pueden escribir y no eligen por el usuario."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.agent.tools.read import (
    buscar_cliente,
    buscar_producto,
    consultar_caja,
    consultar_seguimiento,
    previsualizar_venta,
)
from app.core.datetime_utils import business_today, business_tz
from app.models.cash_movement import CashMovement, CashMovementType
from app.models.customer import Customer
from app.models.customer_product_cycle import CustomerProductCycle
from app.models.sale import Sale

# Los movimientos de caja se siembran con la misma zona con la que produccion
# los escribe (business_midnight()/SaleCreate.occurredAt son siempre
# tz-aware, nunca naive) -- si la siembra fuera naive junto con un boundary
# tambien naive, ambos comparten el mismo supuesto equivocado y el desfase de
# husario queda invisible para el test.
GT = business_tz()


def test_buscar_cliente_returns_every_match_without_choosing(db_session, two_similar_customers):
    result = buscar_cliente.invoke({"nombre": "Gonzalez"})
    assert len(result["clientes"]) == 2
    # No hay campo que elija uno: la desambiguacion es del agente, hablando.
    assert "seleccionado" not in result


def test_buscar_cliente_with_no_match_says_so(db_session):
    result = buscar_cliente.invoke({"nombre": "nadie-con-este-nombre"})
    assert result["clientes"] == []
    assert result["total"] == 0
    assert result["hay_mas"] is False


def test_buscar_cliente_signals_truncation_instead_of_pretending_10_is_all(db_session):
    # 12 clientes que matchean "Perez", con el limite de la herramienta en 10:
    # sin `hay_mas`/`total`, el agente le diria al usuario "encontre 10" como
    # si esos fueran todos.
    for i in range(12):
        db_session.add(Customer(name=f"Perez {i:02d}"))
    db_session.commit()

    result = buscar_cliente.invoke({"nombre": "Perez"})

    assert len(result["clientes"]) == 10
    assert result["total"] == 12
    assert result["hay_mas"] is True


def test_buscar_cliente_writes_nothing(db_session, two_similar_customers):
    before = db_session.query(Customer).count()
    buscar_cliente.invoke({"nombre": "Gonzalez"})
    assert db_session.query(Customer).count() == before


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


@pytest.mark.parametrize("precio_cero", [0, 0.0, "0", "0.00"])
def test_previsualizar_venta_honours_a_price_of_zero(
    db_session, seeded_customer, seeded_product_with_lot, precio_cero
):
    """Un precio de cero es un regalo, no "no vino precio".

    `write.py` ya se habia corregido a `is not None`; esta herramienta
    seguia con una verdad booleana. Con un cero NUMERICO -- que es lo que un
    modelo emite cuando el JSON de la tool call trae `0` en vez de `"0"` --
    eso se leia como "no vino precio", y el preview mostraba el precio
    SUGERIDO a precio completo mientras `registrar_venta`, con la misma
    entrada, registraba cero. Dos cifras distintas para la misma venta, y la
    que el usuario ve antes de aprobar es la equivocada.

    Los cuatro casos van juntos a proposito: la divergencia solo se ve en
    los dos primeros (`"0"` es una cadena no vacia y por lo tanto truthy),
    asi que un test escrito solo con la forma de cadena -- la que el
    docstring de la herramienta documenta -- pasaria con el bug puesto."""
    result = previsualizar_venta.invoke({
        "cliente_id": str(seeded_customer.id),
        "items": [
            {
                "producto_id": str(seeded_product_with_lot.id),
                "cantidad": "1",
                "precio_unitario": precio_cero,
            }
        ],
        "fecha": "2026-09-24",
    })
    item = result["items"][0]

    assert Decimal(str(item["final_unit_price"])) == Decimal("0")
    assert Decimal(str(item["subtotal"])) == Decimal("0")
    assert Decimal(str(result["totals"]["total_revenue"])) == Decimal("0")
    # El sugerido sigue viajando en el preview -- lo que estaba mal era que
    # se usara como precio final.
    assert Decimal(str(item["suggested_unit_price"])) > 0


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


def test_consultar_seguimiento_reports_real_status_and_days_until(
    db_session, seeded_customer, seeded_product_with_lot, seeded_product_with_one_lot
):
    otro_cliente = Customer(name="Otro cliente de prueba")
    db_session.add(otro_cliente)
    db_session.flush()

    # 5 dias -> "urgent" (<=7). -3 dias -> "overdue".
    urgente = CustomerProductCycle(
        customer_id=seeded_customer.id,
        product_id=seeded_product_with_lot.id,
        avg_interval_days=Decimal("15"),
        estimated_next_purchase=business_today() + timedelta(days=5),
        last_purchase_date=business_today() - timedelta(days=10),
        last_quantity=Decimal("2"),
        total_purchases=3,
    )
    vencido = CustomerProductCycle(
        customer_id=otro_cliente.id,
        product_id=seeded_product_with_one_lot.id,
        avg_interval_days=Decimal("10"),
        estimated_next_purchase=business_today() - timedelta(days=3),
        last_purchase_date=business_today() - timedelta(days=13),
        last_quantity=Decimal("1"),
        total_purchases=2,
    )
    db_session.add_all([urgente, vencido])
    db_session.commit()

    todos = consultar_seguimiento.invoke({})
    assert todos["total"] == 2
    status_por_cliente = {s["customer_id"]: s["status"] for s in todos["seguimientos"]}
    dias_por_cliente = {s["customer_id"]: s["items"][0]["days_until"] for s in todos["seguimientos"]}
    assert status_por_cliente[str(seeded_customer.id)] == "urgent"
    assert status_por_cliente[str(otro_cliente.id)] == "overdue"
    assert dias_por_cliente[str(seeded_customer.id)] == 5
    assert dias_por_cliente[str(otro_cliente.id)] == -3

    solo_vencidos = consultar_seguimiento.invoke({"filtro": "overdue"})
    assert solo_vencidos["total"] == 1
    assert solo_vencidos["seguimientos"][0]["customer_id"] == str(otro_cliente.id)


def test_consultar_seguimiento_writes_nothing(db_session):
    before = db_session.query(CustomerProductCycle).count()
    consultar_seguimiento.invoke({})
    assert db_session.query(CustomerProductCycle).count() == before


def _seed_movement(db_session, *, occurred_at, type_, amount):
    movement = CashMovement(occurred_at=occurred_at, type=type_, amount=amount)
    db_session.add(movement)
    db_session.flush()
    return movement


def test_consultar_caja_returns_plain_decimals_as_strings(db_session):
    result = consultar_caja.invoke({})
    assert result["saldo"] == "0.00"
    assert result["saldo_socio"] == "0.00"
    assert result["movimientos"] == []


def test_consultar_caja_reports_real_balance_ordering_and_field_mapping(db_session):
    entrada = _seed_movement(
        db_session,
        occurred_at=datetime(2026, 9, 1, 9, 0, tzinfo=GT),
        type_=CashMovementType.ENTRADA,
        amount=Decimal("500.00"),
    )
    salida = _seed_movement(
        db_session,
        occurred_at=datetime(2026, 9, 10, 15, 30, tzinfo=GT),
        type_=CashMovementType.SALIDA,
        amount=Decimal("120.00"),
    )
    aporte = _seed_movement(
        db_session,
        occurred_at=datetime(2026, 9, 15, 8, 0, tzinfo=GT),
        type_=CashMovementType.APORTE_SOCIO,
        amount=Decimal("200.00"),
    )
    retiro = _seed_movement(
        db_session,
        occurred_at=datetime(2026, 9, 20, 12, 0, tzinfo=GT),
        type_=CashMovementType.RETIRO_SOCIO,
        amount=Decimal("50.00"),
    )
    db_session.commit()

    result = consultar_caja.invoke({})

    # saldo = 500 - 120 + 200 - 50; saldo_socio = 200 (aporte) - 50 (retiro).
    assert result["saldo"] == "530.00"
    assert result["saldo_socio"] == "150.00"

    # Newest first.
    ids = [m["id"] for m in result["movimientos"]]
    assert ids == [str(retiro.id), str(aporte.id), str(salida.id), str(entrada.id)]

    ultimo = result["movimientos"][0]
    assert ultimo["tipo"] == "retiro_socio"
    assert ultimo["monto"] == "50.00"
    assert ultimo["saldo_acumulado"] == "530.00"

    primero_cronologico = result["movimientos"][-1]
    assert primero_cronologico["id"] == str(entrada.id)
    assert primero_cronologico["tipo"] == "entrada"
    assert primero_cronologico["monto"] == "500.00"
    assert primero_cronologico["saldo_acumulado"] == "500.00"
    assert primero_cronologico["medio_pago"] is None
    assert primero_cronologico["venta_id"] is None
    assert primero_cronologico["compra_id"] is None


def test_consultar_caja_limite_caps_to_the_most_recent(db_session):
    for i, (type_, amount) in enumerate([
        (CashMovementType.ENTRADA, Decimal("10.00")),
        (CashMovementType.ENTRADA, Decimal("20.00")),
        (CashMovementType.ENTRADA, Decimal("30.00")),
    ]):
        _seed_movement(
            db_session,
            occurred_at=datetime(2026, 9, 1 + i, 12, 0, tzinfo=GT),
            type_=type_,
            amount=amount,
        )
    db_session.commit()

    result = consultar_caja.invoke({"limite": 2})

    assert len(result["movimientos"]) == 2
    assert [m["monto"] for m in result["movimientos"]] == ["30.00", "20.00"]


def test_consultar_caja_hasta_a_bare_date_includes_that_whole_day(db_session):
    # La salida ocurre a las 23:30 hora de Guatemala, el mismo dia calendario
    # que pedimos como "hasta" (solo fecha, sin hora) -- deliberadamente tarde
    # en el dia local. Con un boundary naive (leido como UTC por Postgres,
    # que corre en UTC) "hasta el 10" caia a las 17:59 hora local y esta
    # salida quedaba afuera; el fix la tiene que incluir.
    entrada = _seed_movement(
        db_session,
        occurred_at=datetime(2026, 9, 1, 9, 0, tzinfo=GT),
        type_=CashMovementType.ENTRADA,
        amount=Decimal("500.00"),
    )
    salida = _seed_movement(
        db_session,
        occurred_at=datetime(2026, 9, 10, 23, 30, tzinfo=GT),
        type_=CashMovementType.SALIDA,
        amount=Decimal("120.00"),
    )
    _seed_movement(
        db_session,
        occurred_at=datetime(2026, 9, 15, 8, 0, tzinfo=GT),
        type_=CashMovementType.APORTE_SOCIO,
        amount=Decimal("200.00"),
    )
    db_session.commit()

    result = consultar_caja.invoke({"hasta": "2026-09-10"})

    ids = [m["id"] for m in result["movimientos"]]
    assert ids == [str(salida.id), str(entrada.id)]
    assert result["movimientos"][0]["saldo_acumulado"] == "380.00"


def test_consultar_caja_desde_hasta_use_the_local_calendar_day_not_utc(db_session):
    # Tres movimientos a caballo del dia pedido, todos en hora de Guatemala:
    # - la noche anterior (23:30 del 9) tiene que quedar AFUERA de `desde`;
    # - la noche del dia pedido (23:30 del 10) tiene que quedar ADENTRO;
    # - la madrugada del dia siguiente (00:15 del 11) tiene que quedar AFUERA
    #   de `hasta`.
    # Bajo el bug original (boundary naive, leido como UTC): `desde
    # "2026-09-10"` caia a las 18:00 local del 9 -- incluyendo de mas esa
    # noche anterior -- y `hasta "2026-09-10"` caia a las 17:59 local del 10
    # -- excluyendo la noche del propio dia pedido.
    noche_anterior = _seed_movement(
        db_session,
        occurred_at=datetime(2026, 9, 9, 23, 30, tzinfo=GT),
        type_=CashMovementType.ENTRADA,
        amount=Decimal("10.00"),
    )
    dentro_del_dia = _seed_movement(
        db_session,
        occurred_at=datetime(2026, 9, 10, 23, 30, tzinfo=GT),
        type_=CashMovementType.ENTRADA,
        amount=Decimal("20.00"),
    )
    madrugada_siguiente = _seed_movement(
        db_session,
        occurred_at=datetime(2026, 9, 11, 0, 15, tzinfo=GT),
        type_=CashMovementType.ENTRADA,
        amount=Decimal("30.00"),
    )
    db_session.commit()

    result = consultar_caja.invoke({"desde": "2026-09-10", "hasta": "2026-09-10"})

    ids = {m["id"] for m in result["movimientos"]}
    assert ids == {str(dentro_del_dia.id)}
    assert str(noche_anterior.id) not in ids
    assert str(madrugada_siguiente.id) not in ids


def test_consultar_caja_writes_nothing(db_session):
    before = db_session.query(CashMovement).count()
    consultar_caja.invoke({})
    assert db_session.query(CashMovement).count() == before


def test_read_tools_own_source_has_no_write_calls():
    # Chequeo HONESTO sobre el texto fuente de read.py: ninguna llamada a
    # db.add/db.commit/db.delete aparece escrita DENTRO de este modulo. Esto
    # no prueba que los servicios que envuelve (CashService, SaleService,
    # CustomerService) no escriban -- un tool que llamara a
    # CashService.record() pasaria este grep igual, porque el commit vive en
    # cash_service.py, no aca. La garantia real, contra el estado de la base,
    # la dan los tests test_*_writes_nothing de arriba.
    import app.agent.tools.read as mod

    with open(mod.__file__) as f:
        text = f.read()

    for forbidden in ("db.add(", "db.commit(", "db.delete(", ".commit()"):
        assert forbidden not in text, f"{forbidden!r} no deberia aparecer en el texto fuente de read.py"


# ---------------------------------------------------------------------
# buscar_producto
#
# Existe porque el agente no tenia con que resolver "carton de huevos" a un
# UUID: tenia `buscar_cliente` y ninguna busqueda de productos, asi que pedia
# el id a mano. Visto en produccion el 2026-10-01, con el agente respondiendo
# "no cuento con una herramienta de busqueda de productos" y el usuario
# pegando el UUID en el chat.
# ---------------------------------------------------------------------


def test_buscar_producto_devuelve_lo_que_el_agente_necesita_para_vender(
    db_session, seeded_product_with_lot
):
    """Sin `id` la herramienta no sirve -- es el dato que el agente vino a
    buscar. El resto (precio, stock) le evita una consulta mas antes de
    previsualizar."""
    result = buscar_producto.invoke({"nombre": seeded_product_with_lot.name[:6]})

    assert len(result["productos"]) >= 1
    encontrado = next(p for p in result["productos"] if p["id"] == str(seeded_product_with_lot.id))
    assert encontrado["nombre"] == seeded_product_with_lot.name
    assert "stock" in encontrado
    assert "sku" in encontrado


def test_buscar_producto_con_cero_coincidencias_lo_dice(db_session):
    result = buscar_producto.invoke({"nombre": "no-existe-este-producto"})

    assert result["productos"] == []
    assert result["total"] == 0


def test_buscar_producto_no_elige_por_el_usuario(db_session, seeded_user):
    """Misma regla que `buscar_cliente`: devolver todas las coincidencias y
    dejar que la persona elija. Un agente que elige solo entre dos productos
    parecidos registra la venta del equivocado."""
    from app.models.product import Product

    for nombre in ("Cartón de huevos (30 U)", "Cartón de huevos (12 U)"):
        db_session.add(
            Product(
                sku=f"SKU-{nombre[-5:-1]}",
                name=nombre,
                earning_mode="fee",
                earning_fee_amount=Decimal("3.50"),
                stock=Decimal("0"),
                min_stock=Decimal("0"),
                status="active",
            )
        )
    # commit, no flush: `agent_session()` abre su PROPIA sesion sobre el mismo
    # engine (ver `db_session` en fixtures_domain.py), asi que no ve lo que
    # todavia vive solo en la transaccion de esta.
    db_session.commit()

    result = buscar_producto.invoke({"nombre": "Cartón de huevos"})

    assert len(result["productos"]) == 2


def test_buscar_producto_avisa_cuando_hay_mas_de_los_que_devuelve(db_session):
    """Igual que `buscar_cliente`: `total` es cuantos hay, no cuantos se
    devolvieron. Sin `hay_mas`, el agente diria "estos son los productos" sobre
    una lista truncada."""
    from app.models.product import Product

    for i in range(12):
        db_session.add(
            Product(
                sku=f"MUCHOS-{i:02d}",
                name=f"Producto repetido {i:02d}",
                earning_mode="fee",
                earning_fee_amount=Decimal("1.00"),
                stock=Decimal("0"),
                min_stock=Decimal("0"),
                status="active",
            )
        )
    db_session.commit()

    result = buscar_producto.invoke({"nombre": "Producto repetido"})

    assert len(result["productos"]) == 10
    assert result["total"] == 12
    assert result["hay_mas"] is True
