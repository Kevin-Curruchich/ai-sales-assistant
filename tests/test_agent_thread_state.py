"""`GET /api/v1/agent/threads/{thread_id}/state`: el estado de un hilo.

Existe para un caso que el panel vive todo el tiempo: recargar la pagina. Sin
este endpoint, un refresh con una confirmacion abierta dejaba la conversacion
inservible -- el panel no sabia que habia una tarjeta pendiente, y no podia
aprobarla, porque la huella habia viajado una sola vez en el evento
`confirmacion` de ese turno y se fue con el estado del navegador.

Las reglas de traduccion (que es un mensaje, que es actividad de herramienta,
como se ve una confirmacion pendiente) se prueban baratas y solas en
`tests/test_agent_historial.py`, sobre la funcion pura. Este archivo prueba el
camino completo: un turno que corre de verdad, y despues el estado leido por
HTTP con el token de su dueño.

El harness del grafo de juguete se importa de `test_agent_stream_endpoint` en
vez de duplicarse -- incluida la fixture `autouse` que intercepta la
construccion del grafo dentro del lifespan real. Importar la fixture la registra
en ESTE modulo, asi que su `autouse` aplica aca y no al resto de la suite: si
viviera en un plugin compartido parchearia el grafo de todos los tests, incluido
`test_startup.py`.
"""

import pytest

import app.main as main
from app.agent.tools.write import registrar_venta
from app.models.sale import Sale
from tests.fixtures_http import _auth, _create_thread_as
from tests.test_agent_graph import _sale_tool_call
from tests.test_agent_stream_endpoint import (  # noqa: F401 -- fixtures autouse
    _confirmaciones,
    _grafo_de_juguete,
    _hilos_en_curso_limpios,
    _instalar_grafo,
    _turno,
)


def _estado(client, user, thread_id):
    resp = client.get(f"/api/v1/agent/threads/{thread_id}/state", headers=_auth(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_thread_that_never_ran_answers_empty_instead_of_404(client, seeded_user):
    """Un hilo recien creado no tiene checkpoint. Eso no es un error: es una
    conversacion sin empezar, y el panel tiene que poder abrirla."""
    hilo = _create_thread_as(client, seeded_user, "nuevo")

    assert _estado(client, seeded_user, hilo["id"]) == {
        "mensajes": [],
        "confirmaciones_pendientes": [],
    }


def test_the_owner_sees_the_conversation_with_the_tool_activity(
    client, seeded_user, seeded_customer, seeded_product_with_lot
):
    """El historial se ve como lo que la persona vio en vivo: su mensaje, la
    herramienta que corrio, y nada de la mecanica interna."""
    _instalar_grafo(
        [_sale_tool_call(seeded_customer, seeded_product_with_lot)], [registrar_venta]
    )
    hilo = _create_thread_as(client, seeded_user, "mio")
    _turno(client, seeded_user, {"thread_id": hilo["id"], "mensaje": "vendi un carton"})

    estado = _estado(client, seeded_user, hilo["id"])

    assert estado["mensajes"][0] == {"rol": "usuario", "texto": "vendi un carton"}
    assert {"rol": "herramienta", "nombre": "registrar_venta"} in estado["mensajes"]
    assert all(
        m["rol"] in {"usuario", "asistente", "herramienta"} for m in estado["mensajes"]
    )


def test_a_pending_confirmation_survives_a_reload_with_its_huella_intact(
    client, seeded_user, seeded_customer, seeded_product_with_lot
):
    """La razon de ser del endpoint.

    La huella que el estado devuelve tiene que ser IDENTICA a la que viajo en el
    evento `confirmacion`. Si el servidor la reformateara aunque sea un poco, el
    panel no podria aprobar despues de un refresh: la comparacion del lado de la
    herramienta es byte por byte contra lo que se firmo.
    """
    _instalar_grafo(
        [_sale_tool_call(seeded_customer, seeded_product_with_lot)], [registrar_venta]
    )
    hilo = _create_thread_as(client, seeded_user, "mio")
    eventos = _turno(
        client, seeded_user, {"thread_id": hilo["id"], "mensaje": "vendi un carton"}
    )
    pausa_en_vivo = _confirmaciones(eventos)[0]

    estado = _estado(client, seeded_user, hilo["id"])

    assert estado["confirmaciones_pendientes"] == [pausa_en_vivo]


def test_the_state_lets_a_reloaded_panel_actually_approve(
    client, db_session, seeded_user, seeded_customer, seeded_product_with_lot
):
    """El caso completo, que es lo que el endpoint promete: el panel pierde su
    estado, lo recupera por HTTP, y con SOLO eso aprueba y la venta se escribe.

    Sin este test, los anteriores demuestran que el JSON tiene la forma
    correcta, no que alcance para hacer el trabajo.
    """
    _instalar_grafo(
        [_sale_tool_call(seeded_customer, seeded_product_with_lot)], [registrar_venta]
    )
    hilo = _create_thread_as(client, seeded_user, "mio")
    _turno(client, seeded_user, {"thread_id": hilo["id"], "mensaje": "vendi un carton"})
    assert db_session.query(Sale).count() == 0

    # El panel se recargo: lo unico que tiene es lo que devuelve el endpoint.
    recuperado = _estado(client, seeded_user, hilo["id"])["confirmaciones_pendientes"][
        0
    ]

    segundo = _turno(
        client,
        seeded_user,
        {
            "thread_id": hilo["id"],
            "interrupt_id": recuperado["interrupt_id"],
            "decision": {"accion": "aprobar", "huella": recuperado["huella"]},
        },
    )

    assert [e for e in segundo if e["event"] == "error"] == []
    db_session.expire_all()
    ventas = db_session.query(Sale).all()
    assert len(ventas) == 1
    assert ventas[0].user_id == seeded_user.id


def test_an_answered_confirmation_stops_being_pending(
    client, seeded_user, seeded_customer, seeded_product_with_lot
):
    """Despues de aprobar, el estado no puede seguir ofreciendo esa tarjeta: el
    panel la mostraria de nuevo tras un refresh y aprobarla otra vez daria 409.

    Es el caso que distingue leer las tareas (`result is None`) de leer
    `snapshot.interrupts`, que sigue trayendo las respondidas.
    """
    _instalar_grafo(
        [_sale_tool_call(seeded_customer, seeded_product_with_lot)], [registrar_venta]
    )
    hilo = _create_thread_as(client, seeded_user, "mio")
    eventos = _turno(
        client, seeded_user, {"thread_id": hilo["id"], "mensaje": "vendi un carton"}
    )
    pausa = _confirmaciones(eventos)[0]
    _turno(
        client,
        seeded_user,
        {
            "thread_id": hilo["id"],
            "interrupt_id": pausa["interrupt_id"],
            "decision": {"accion": "aprobar", "huella": pausa["huella"]},
        },
    )

    assert _estado(client, seeded_user, hilo["id"])["confirmaciones_pendientes"] == []


def test_reading_another_users_thread_state_is_a_404(client, seeded_user, second_user):
    """Mismo 404 que el resto de los endpoints de hilo, y por la misma razon:
    distinguir "no existe" de "no es tuyo" le confirmaria a un tercero que ese
    hilo existe.

    Fija las DOS ramas, no solo la negativa: una ruta que no existe tambien
    devuelve 404, asi que sin el 200 del dueño este test pasaba con el endpoint
    todavia sin escribir -- lo comprobe corriendolo antes de implementarlo.
    """
    mio = _create_thread_as(client, seeded_user, "mio")
    ajeno = _create_thread_as(client, second_user, "del otro")

    propio = client.get(
        f"/api/v1/agent/threads/{mio['id']}/state", headers=_auth(seeded_user)
    )
    assert propio.status_code == 200

    resp = client.get(
        f"/api/v1/agent/threads/{ajeno['id']}/state", headers=_auth(seeded_user)
    )

    assert resp.status_code == 404


def test_reading_the_state_without_a_graph_is_a_503(client, seeded_user):
    """El lifespan deja `agent_graph = None` si falta una variable del agente o
    si Postgres no respondio al arrancar. Sin el chequeo, `aget_state` sobre
    `None` seria un `AttributeError` que el panel veria como un 500 opaco."""
    hilo = _create_thread_as(client, seeded_user, "mio")
    anterior = getattr(main.app.state, "agent_graph", None)
    main.app.state.agent_graph = None
    try:
        resp = client.get(
            f"/api/v1/agent/threads/{hilo['id']}/state", headers=_auth(seeded_user)
        )
    finally:
        main.app.state.agent_graph = anterior

    assert resp.status_code == 503


def test_the_state_endpoint_does_not_reserve_the_thread(
    client, seeded_user, seeded_customer, seeded_product_with_lot
):
    """Leer el estado no es correr el hilo: si dejara el `thread_id` en
    `_hilos_en_curso`, el proximo pedido real recibiria un 409 eterno -- la
    misma clase de trabazon que el endpoint de streaming tuvo."""
    _instalar_grafo(
        [_sale_tool_call(seeded_customer, seeded_product_with_lot)], [registrar_venta]
    )
    hilo = _create_thread_as(client, seeded_user, "mio")

    _estado(client, seeded_user, hilo["id"])

    eventos = _turno(
        client, seeded_user, {"thread_id": hilo["id"], "mensaje": "vendi un carton"}
    )
    assert [e for e in eventos if e["event"] == "error"] == []
