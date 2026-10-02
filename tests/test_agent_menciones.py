"""Menciones (@cliente/@producto/@venta) y comandos (/venta, /compra, /cobro,
/caja) del composer del panel.

El panel manda, junto al `mensaje`, los ids de lo que la persona menciono y la
intencion del comando. El agente los recibe como referencias junto al texto y
los usa sin buscar -- antes terminaba pidiendole el UUID del producto a la
persona. Contrato: `docs/agente.md`, "Menciones y comandos".
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.historial import traducir_estado
from app.agent.prompt_runtime import prompt_con_fecha
from app.agent.referencias import con_referencias
from tests.fixtures_http import _auth, _create_thread_as
from tests.test_agent_stream_endpoint import (  # noqa: F401 -- fixtures autouse
    _grafo_de_juguete,
    _hilos_en_curso_limpios,
    _last_entrada,
    _turno,
)

CLIENTE_ID = "9ab4f1c2-0000-4000-8000-000000000001"
PRODUCTO_ID = "115e2b7a-0000-4000-8000-000000000002"
VENTA_ID = "5a1e0000-0000-4000-8000-000000000003"

MENSAJE = "Vendí 2 @Cartón de huevos a @Aurita, no pagado"
MENCIONES = [
    {"tipo": "producto", "id": PRODUCTO_ID, "nombre": "Cartón de huevos", "inicio": 8, "fin": 25},
    {"tipo": "cliente", "id": CLIENTE_ID, "nombre": "Aurita", "inicio": 28, "fin": 35},
]


def test_the_example_ranges_point_at_the_mentions():
    """Si este falla, el resto del archivo prueba con rangos equivocados."""
    assert MENSAJE[8:25] == "@Cartón de huevos"
    assert MENSAJE[28:35] == "@Aurita"


def _post(client, user, cuerpo):
    return client.post("/api/v1/agent/stream", json=cuerpo, headers=_auth(user))


def _estado(client, user, thread_id):
    resp = client.get(f"/api/v1/agent/threads/{thread_id}/state", headers=_auth(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------
# El request
# ---------------------------------------------------------------------


def test_a_message_with_mentions_and_a_command_reaches_the_graph_with_them(client, seeded_user):
    hilo = _create_thread_as(client, seeded_user, "mio")

    _turno(
        client,
        seeded_user,
        {"thread_id": hilo["id"], "mensaje": MENSAJE, "comando": "venta", "menciones": MENCIONES},
    )

    (humano,) = _last_entrada()["messages"]
    assert isinstance(humano, HumanMessage)
    assert humano.content == MENSAJE
    assert humano.additional_kwargs["comando"] == "venta"
    assert humano.additional_kwargs["menciones"] == MENCIONES


def test_a_message_without_the_new_fields_reaches_the_graph_as_plain_text(client, seeded_user):
    hilo = _create_thread_as(client, seeded_user, "mio")

    _turno(client, seeded_user, {"thread_id": hilo["id"], "mensaje": "hola"})

    (humano,) = _last_entrada()["messages"]
    assert humano.content == "hola"
    assert "comando" not in humano.additional_kwargs
    assert "menciones" not in humano.additional_kwargs


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"comando": "borrar"}, id="comando desconocido"),
        pytest.param({"menciones": [{**MENCIONES[0], "tipo": "proveedor"}]}, id="tipo desconocido"),
        pytest.param({"menciones": [{**MENCIONES[0], "fin": len(MENSAJE) + 1}]}, id="rango fuera del texto"),
        pytest.param({"menciones": [{**MENCIONES[0], "inicio": -1}]}, id="inicio negativo"),
        pytest.param({"menciones": [{**MENCIONES[0], "fin": 8}]}, id="rango vacio"),
        pytest.param(
            {"menciones": [MENCIONES[0], {**MENCIONES[1], "inicio": 20}]}, id="rangos superpuestos"
        ),
        pytest.param({"menciones": [{**MENCIONES[0], "id": "no-es-un-uuid"}]}, id="id que no es uuid"),
    ],
)
def test_a_malformed_mention_or_command_is_a_422(client, seeded_user, extra):
    hilo = _create_thread_as(client, seeded_user, "mio")

    resp = _post(client, seeded_user, {"thread_id": hilo["id"], "mensaje": MENSAJE, **extra})

    assert resp.status_code == 422, resp.text


def test_mentions_without_a_mensaje_are_a_422(client, seeded_user):
    """Un rango solo tiene sentido dentro de un texto: con una `decision` no
    hay mensaje que apuntar."""
    hilo = _create_thread_as(client, seeded_user, "mio")

    resp = _post(
        client,
        seeded_user,
        {"thread_id": hilo["id"], "decision": {"accion": "cancelar"}, "interrupt_id": "x", "comando": "venta"},
    )

    assert resp.status_code == 422, resp.text


def test_ranges_are_counted_in_code_points_not_utf16_units(client, seeded_user):
    """El huevo es UN code point y DOS unidades UTF-16. Contado en code points
    "@Aurita" va de 2 a 9; contado en UTF-16 iria de 3 a 10 y se saldria del
    texto."""
    mensaje = "🥚 @Aurita"
    assert mensaje[2:9] == "@Aurita"
    hilo = _create_thread_as(client, seeded_user, "mio")
    mencion = {"tipo": "cliente", "id": CLIENTE_ID, "nombre": "Aurita", "inicio": 2, "fin": 9}

    _turno(client, seeded_user, {"thread_id": hilo["id"], "mensaje": mensaje, "menciones": [mencion]})

    resp = _post(
        client,
        seeded_user,
        {"thread_id": hilo["id"], "mensaje": mensaje, "menciones": [{**mencion, "inicio": 3, "fin": 10}]},
    )
    assert resp.status_code == 422


def test_ids_that_do_not_exist_are_not_rejected_by_the_endpoint(client, seeded_user):
    """La existencia la decide la tool (`no_encontrada`), no el endpoint."""
    hilo = _create_thread_as(client, seeded_user, "mio")

    _turno(client, seeded_user, {"thread_id": hilo["id"], "mensaje": MENSAJE, "menciones": MENCIONES})


# ---------------------------------------------------------------------
# El historial
# ---------------------------------------------------------------------


def test_the_state_gives_back_the_command_and_mentions_of_a_user_message(client, seeded_user):
    hilo = _create_thread_as(client, seeded_user, "mio")
    _turno(
        client,
        seeded_user,
        {"thread_id": hilo["id"], "mensaje": MENSAJE, "comando": "venta", "menciones": MENCIONES},
    )

    primero = _estado(client, seeded_user, hilo["id"])["mensajes"][0]

    assert primero == {"rol": "usuario", "texto": MENSAJE, "comando": "venta", "menciones": MENCIONES}


def test_a_plain_user_message_keeps_its_old_shape_in_the_state(client, seeded_user):
    hilo = _create_thread_as(client, seeded_user, "mio")
    _turno(client, seeded_user, {"thread_id": hilo["id"], "mensaje": "hola"})

    assert _estado(client, seeded_user, hilo["id"])["mensajes"][0] == {"rol": "usuario", "texto": "hola"}


def test_an_old_checkpointed_message_without_the_fields_still_translates():
    snapshot = type("S", (), {"values": {"messages": [HumanMessage("viejo")]}, "tasks": []})()

    assert traducir_estado(snapshot)["mensajes"] == [{"rol": "usuario", "texto": "viejo"}]


# ---------------------------------------------------------------------
# Lo que ve el modelo
# ---------------------------------------------------------------------


def _con_campos(comando=None, menciones=None, texto=MENSAJE):
    extra = {}
    if comando:
        extra["comando"] = comando
    if menciones:
        extra["menciones"] = menciones
    return HumanMessage(content=texto, additional_kwargs=extra)


def test_the_model_sees_each_mention_with_the_id_the_tools_expect():
    texto = con_referencias(_con_campos(menciones=MENCIONES)).content

    assert texto.startswith(MENSAJE)
    assert f'"@Cartón de huevos" = producto, producto_id {PRODUCTO_ID}' in texto
    assert f'"@Aurita" = cliente, cliente_id {CLIENTE_ID}' in texto


def test_a_mentioned_sale_is_the_venta_id_of_registrar_cobro():
    mensaje = "@Aurita pagó @Venta 24/09 · Q33.33 en efectivo"
    menciones = [
        {"tipo": "cliente", "id": CLIENTE_ID, "nombre": "Aurita", "inicio": 0, "fin": 7},
        {"tipo": "venta", "id": VENTA_ID, "nombre": "Venta 24/09 · Q33.33", "inicio": 13, "fin": 34},
    ]
    assert mensaje[13:34] == "@Venta 24/09 · Q33.33"

    texto = con_referencias(_con_campos("cobro", menciones, texto=mensaje)).content

    assert f'"@Venta 24/09 · Q33.33" = venta, venta_id {VENTA_ID}' in texto
    assert "registrar_cobro" in texto


def test_the_command_is_a_hint_of_intent():
    texto = con_referencias(_con_campos(comando="venta")).content

    assert "/venta" in texto
    assert "registrar una venta" in texto


def test_a_message_without_references_reaches_the_model_untouched():
    mensaje = HumanMessage("hola")

    assert con_referencias(mensaje) is mensaje


def test_the_references_do_not_leak_into_the_stored_message():
    """El bloque de referencias se arma en cada turno para el modelo; el
    mensaje guardado sigue siendo el texto de la persona, que es lo que el
    historial le devuelve al panel."""
    guardado = _con_campos("venta", MENCIONES)

    prompt_con_fecha({"messages": [guardado]})

    assert guardado.content == MENSAJE


def test_prompt_con_fecha_sends_the_references_to_the_model():
    mensajes = prompt_con_fecha({"messages": [_con_campos("venta", MENCIONES), AIMessage("listo")]})

    assert CLIENTE_ID in mensajes[1].content
    assert mensajes[2].content == "listo"
