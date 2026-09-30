"""La traduccion de un snapshot del checkpointer a lo que el panel renderiza.

`app/agent/historial.py::traducir_estado` es una funcion PURA sobre lo que
devuelve `graph.aget_state(config)`: sin grafo, sin base, sin HTTP. Por eso
estos tests arman snapshots a mano en vez de correr un turno -- las reglas de
traduccion se prueban mas barato y mas preciso asi, y el camino completo (correr
un turno de verdad y leerlo por HTTP) tiene sus propios tests en
`tests/test_agent_thread_state.py`.

El test del final es el que importa que no se rompa: fija que este traductor y
`eventos_sse` hablen el MISMO vocabulario. Si divergen, el historial que el
panel carga tras un refresh no coincide con lo que la persona vio en vivo, y esa
clase de deriva no la agarra ningun test de cada lado por separado.
"""

import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.historial import traducir_estado


def _snapshot(messages=(), tasks=()):
    """Lo minimo que `traducir_estado` lee de un `StateSnapshot`.

    `aget_state` devuelve mucho mas (config, metadata, next, checkpoint...);
    un `SimpleNamespace` con los dos campos que la funcion toca alcanza y
    deja explicito de que depende.
    """
    return SimpleNamespace(values={"messages": list(messages)}, tasks=list(tasks))


def _tarea(interrupts=(), result=None):
    return SimpleNamespace(interrupts=list(interrupts), result=result)


def _interrupcion(valor, id=None):
    return SimpleNamespace(id=id or uuid.uuid4().hex, value=valor)


def test_a_thread_that_never_ran_has_nothing_to_show():
    """Un hilo recien creado no tiene checkpoint: `aget_state` devuelve un
    snapshot vacio, y eso es un hilo sin mensajes -- no un error."""
    estado = traducir_estado(_snapshot())

    assert estado == {"mensajes": [], "confirmaciones_pendientes": []}


def test_a_snapshot_without_a_messages_key_is_not_a_crash():
    """Defensa contra la forma, no paranoia: un hilo sin checkpoint puede dar
    `values` vacio, y un `KeyError` aca seria un 500 en el panel por un hilo
    nuevo."""
    estado = traducir_estado(SimpleNamespace(values={}, tasks=[]))

    assert estado == {"mensajes": [], "confirmaciones_pendientes": []}


def test_the_user_and_the_assistant_become_the_conversation():
    estado = traducir_estado(
        _snapshot([HumanMessage("vendi dos cartones a Aurita"), AIMessage("Listo, la registre.")])
    )

    assert estado["mensajes"] == [
        {"rol": "usuario", "texto": "vendi dos cartones a Aurita"},
        {"rol": "asistente", "texto": "Listo, la registre."},
    ]


def test_a_tool_call_becomes_tool_activity_where_it_was_requested():
    """El panel muestra «consultando lotes...» en el historial igual que en
    vivo, asi que la actividad va en la POSICION donde el modelo la pidio: el
    `AIMessage` que la pide viene antes de su `ToolMessage`."""
    estado = traducir_estado(
        _snapshot(
            [
                HumanMessage("vendi un carton"),
                AIMessage(
                    "",
                    tool_calls=[
                        {"name": "previsualizar_venta", "args": {}, "id": "1"},
                    ],
                ),
                ToolMessage("lo que devolvio la herramienta", tool_call_id="1"),
                AIMessage("Son Q50."),
            ]
        )
    )

    assert estado["mensajes"] == [
        {"rol": "usuario", "texto": "vendi un carton"},
        {"rol": "herramienta", "nombre": "previsualizar_venta"},
        {"rol": "asistente", "texto": "Son Q50."},
    ]


def test_two_tool_calls_in_one_message_keep_their_order():
    """`version="v2"` reparte un `Send()` por tool_call, pero las dos viajan en
    UN solo `AIMessage` -- el mismo motivo por el que `eventos_sse` las lee del
    canal `updates` y no de `messages`."""
    estado = traducir_estado(
        _snapshot(
            [
                AIMessage(
                    "",
                    tool_calls=[
                        {"name": "previsualizar_venta", "args": {}, "id": "1"},
                        {"name": "registrar_movimiento_caja", "args": {}, "id": "2"},
                    ],
                )
            ]
        )
    )

    assert estado["mensajes"] == [
        {"rol": "herramienta", "nombre": "previsualizar_venta"},
        {"rol": "herramienta", "nombre": "registrar_movimiento_caja"},
    ]


def test_the_tool_results_and_the_system_prompt_are_not_conversation():
    """Un `ToolMessage` es resultado interno y el `SystemMessage` es el prompt:
    ninguno de los dos es algo que la persona dijo ni que el agente le dijo."""
    estado = traducir_estado(
        _snapshot(
            [
                SystemMessage("sos un asistente de ventas"),
                HumanMessage("hola"),
                ToolMessage('{"total": "50.00"}', tool_call_id="1"),
            ]
        )
    )

    assert estado["mensajes"] == [{"rol": "usuario", "texto": "hola"}]


def test_an_assistant_message_with_no_text_is_not_a_message():
    """Misma regla que el evento `token`, que saltea el texto vacio: un
    `AIMessage` que solo pide herramientas no tiene nada que escribir, y una
    burbuja vacia en el historial es ruido."""
    estado = traducir_estado(
        _snapshot([AIMessage("", tool_calls=[{"name": "buscar", "args": {}, "id": "1"}])])
    )

    assert [m for m in estado["mensajes"] if m["rol"] == "asistente"] == []


def test_the_assistant_text_comes_from_blocks_not_from_raw_content():
    """`content` con el modelo real y herramientas bindeadas es una LISTA DE
    BLOQUES, no un `str` (`coerce_content_to_string` es siempre falso en ese
    camino). Sin leer el texto de los bloques, el historial mandaria la lista
    entera -- incluido el JSON parcial de los argumentos de una tool_call -- al
    campo `texto`, que es exactamente el bug que el evento `token` ya tuvo."""
    estado = traducir_estado(
        _snapshot(
            [
                AIMessage(
                    [
                        {"type": "text", "text": "Vendiste medio"},
                        {"type": "tool_use", "name": "x", "input": {}, "id": "1"},
                    ]
                )
            ]
        )
    )

    assert estado["mensajes"] == [{"rol": "asistente", "texto": "Vendiste medio"}]


def test_a_pending_interrupt_comes_back_with_its_id_alongside_its_payload():
    """Misma forma que el `data` del evento `confirmacion`: el `interrupt_id`
    mezclado con las claves del payload, no envuelto. El panel usa UN renderer
    para la tarjeta, venga del stream o de un refresh."""
    huella = {"datos": [{"subtotal": "50.00"}], "firma": "abc123"}
    interrupcion = _interrupcion(
        {"tipo": "confirmar_venta", "preview": {"total": "50.00"}, "huella": huella},
        id="ii-1",
    )

    estado = traducir_estado(_snapshot(tasks=[_tarea([interrupcion])]))

    assert estado["confirmaciones_pendientes"] == [
        {
            "tipo": "confirmar_venta",
            "preview": {"total": "50.00"},
            "huella": huella,
            "interrupt_id": "ii-1",
        }
    ]


def test_an_answered_interrupt_is_not_pending_anymore():
    """La parte facil de equivocar, y la razon por la que esto NO lee
    `snapshot.interrupts`: esa lista sigue trayendo las pausas YA respondidas
    mientras el paso no termine (verificado contra langgraph 1.0.3). Lo que las
    distingue es la tarea -- la respondida tiene `result`, la que sigue
    esperando lo tiene en `None`.

    Sin esta regla, el panel mostraria al recargar una tarjeta de confirmacion
    que la persona ya aprobo, y aprobarla otra vez daria 409."""
    respondida = _tarea([_interrupcion({"tipo": "confirmar_venta"}, id="vieja")], result={"ok": 1})
    abierta = _tarea([_interrupcion({"tipo": "confirmar_compra"}, id="nueva")])

    estado = traducir_estado(_snapshot(tasks=[respondida, abierta]))

    assert [c["interrupt_id"] for c in estado["confirmaciones_pendientes"]] == ["nueva"]


def test_an_interrupt_whose_payload_is_not_a_dict_still_carries_its_id():
    """Mismo criterio que `_payload` en `streaming.py`: una herramienta futura
    que interrumpa con un string no puede hacer reventar la traduccion entera y
    dejar al panel sin forma de responder la pausa."""
    estado = traducir_estado(_snapshot(tasks=[_tarea([_interrupcion("algo raro", id="ii-9")])]))

    assert estado["confirmaciones_pendientes"] == [{"valor": "algo raro", "interrupt_id": "ii-9"}]


@pytest.mark.asyncio
async def test_the_history_and_the_live_stream_speak_the_same_vocabulary():
    """El test anti-deriva, y el motivo por el que este modulo puede vivir
    aparte de `streaming.py`.

    Los dos traducen lo mismo para audiencias distintas -- el stream en vivo y
    el historial de un refresh -- y tienen que coincidir en tres cosas: como se
    llama la actividad de herramienta y su campo, de donde sale el texto del
    asistente, y la forma de una confirmacion pendiente. Si alguien cambia una
    sola de las dos, el panel muestra el historial distinto de lo que la persona
    acababa de ver.
    """
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.prebuilt import create_react_agent
    from langgraph.types import interrupt

    from app.agent.signing import firmar
    from app.agent.streaming import eventos_sse
    from tests.test_agent_graph import FakeToolCallingModel

    @tool
    def previsualizar_venta() -> str:
        """Pide confirmacion."""
        interrupt({"tipo": "confirmar_venta", "huella": firmar([{"subtotal": "50.00"}])})
        return "listo"

    graph = create_react_agent(
        FakeToolCallingModel(
            scripted_tool_calls=[{"name": "previsualizar_venta", "args": {}, "id": "1"}]
        ),
        [previsualizar_venta],
        checkpointer=MemorySaver(),
        version="v2",
    )
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    eventos = [e async for e in eventos_sse(graph, {"messages": [("user", "vendi")]}, config)]
    estado = traducir_estado(await graph.aget_state(config))

    # 1. La actividad de herramienta: mismo nombre de tipo, mismo campo.
    herramienta_en_vivo = [e["data"]["nombre"] for e in eventos if e["event"] == "herramienta"]
    herramienta_en_historial = [
        m["nombre"] for m in estado["mensajes"] if m["rol"] == "herramienta"
    ]
    assert herramienta_en_vivo == herramienta_en_historial == ["previsualizar_venta"]

    # 2. La confirmacion pendiente: misma forma, y la huella IDENTICA -- si el
    #    historial la reformateara, el panel no podria aprobar tras un refresh.
    confirmacion_en_vivo = [e["data"] for e in eventos if e["event"] == "confirmacion"]
    assert len(confirmacion_en_vivo) == 1
    assert estado["confirmaciones_pendientes"] == confirmacion_en_vivo
