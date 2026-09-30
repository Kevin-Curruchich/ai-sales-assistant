"""La traduccion de los eventos del grafo a los que el panel entiende.

Cuatro grafos de juguete, ninguno toca la base: `FakeToolCallingModel` (de
`tests/test_agent_graph.py`) emite en el primer turno las tool_calls con las
que se lo construye y despues cierra con texto plano, asi que el grafo
termina la corrida sin pedir mas herramientas. Las herramientas de estos
grafos son propias de este archivo, no las reales de `app/agent/tools`:
las reales pegan contra Postgres via `agent_session()` y esta task no
necesita, ni debe, tocar ninguna base.

`grafo_que_interrumpe` firma su huella con `firmar()` de
`app/agent/signing.py` DENTRO de la herramienta de juguete -- exactamente
como lo hacen las herramientas de escritura reales. Eso prueba que
`eventos_sse` pasa la huella tal cual, sin firmarla ella misma: firmar ahi
firmaria datos que la capa de streaming nunca valido.

Los cuatro grafos son FIXTURES, no globales de modulo (Fix Round 1, H/M3 de
la revision): `FakeToolCallingModel` lleva un contador `n` y solo emite sus
`scripted_tool_calls` en la PRIMERA invocacion -- un grafo de modulo,
compartido entre corridas, se convierte en el trivial ("listo" + `fin
completo`, sin herramienta/confirmacion/error) en cualquier segundo uso.
Con una fixture de scope por-test, cada test arma su propio modelo y su
propio `MemorySaver` desde cero.
"""

import asyncio

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from langgraph.types import interrupt

from app.agent.signing import firmar, verificar
from app.agent.streaming import eventos_sse
from tests.test_agent_graph import FakeToolCallingModel

ENTRADA = {"messages": [("user", "hola")]}
CONFIG = {"configurable": {"thread_id": "streaming-tests"}}


@tool
def previsualizar_venta(cliente_id: str) -> dict:
    """Tool de juguete: calcula (finge calcular) una venta sin tocar la base."""
    return {"cliente_id": cliente_id, "preview": True}


@tool
def confirmar_algo() -> dict:
    """Tool de juguete que pausa el grafo pidiendo confirmacion, con una
    huella firmada de verdad -- tal como hacen las herramientas de
    escritura reales en `app/agent/tools/write.py`."""
    huella = firmar({"monto": "100.00"})
    decision = interrupt({"tipo": "confirmar_algo", "huella": huella})
    return {"estado": "listo", "decision": decision}


@tool
def revienta() -> dict:
    """Tool de juguete que siempre levanta una excepcion comun."""
    raise ValueError("la herramienta exploto")


def _grafo_de_juguete(scripted_tool_calls, tools):
    model = FakeToolCallingModel(scripted_tool_calls=scripted_tool_calls)
    return create_react_agent(model, tools, checkpointer=MemorySaver(), version="v2")


@pytest.fixture
def grafo_simple():
    return _grafo_de_juguete([], [])


@pytest.fixture
def grafo_con_herramienta():
    return _grafo_de_juguete(
        [{"name": "previsualizar_venta", "args": {"cliente_id": "cli-1"}, "id": "call_1"}],
        [previsualizar_venta],
    )


@pytest.fixture
def grafo_que_interrumpe():
    return _grafo_de_juguete(
        [{"name": "confirmar_algo", "args": {}, "id": "call_2"}],
        [confirmar_algo],
    )


@pytest.fixture
def grafo_que_revienta():
    return _grafo_de_juguete(
        [{"name": "revienta", "args": {}, "id": "call_3"}],
        [revienta],
    )


@pytest.mark.asyncio
async def test_a_plain_answer_streams_tokens_then_ends(grafo_simple):
    eventos = [e async for e in eventos_sse(grafo_simple, ENTRADA, CONFIG)]
    tipos = [e["event"] for e in eventos]

    assert tipos[0] == "token"
    assert tipos[-1] == "fin"
    assert eventos[-1]["data"]["estado"] == "completo"


@pytest.mark.asyncio
async def test_a_tool_call_announces_itself(grafo_con_herramienta):
    eventos = [e async for e in eventos_sse(grafo_con_herramienta, ENTRADA, CONFIG)]
    herramientas = [e for e in eventos if e["event"] == "herramienta"]

    assert herramientas[0]["data"]["nombre"] == "previsualizar_venta"


@pytest.mark.asyncio
async def test_an_interrupt_becomes_a_confirmacion_carrying_its_huella(grafo_que_interrumpe):
    eventos = [e async for e in eventos_sse(grafo_que_interrumpe, ENTRADA, CONFIG)]
    conf = [e for e in eventos if e["event"] == "confirmacion"][0]

    # `verificar(...)`, no `set(huella) == {"datos", "firma"}` (Fix Round 1,
    # L1 de la revision): la forma sola no distingue un pass-through de una
    # re-firma -- `firmar` es determinista, asi que una capa que re-firmara
    # los mismos `datos` producira el mismo sobre y la asercion de forma
    # pasaria igual. `verificar` ademas confirma que la firma es VALIDA
    # contra el secreto del entorno, y que los datos son los que la tool de
    # juguete firmo -- no cualquier `dict` con esas dos claves.
    assert verificar(conf["data"]["huella"]) == (True, {"monto": "100.00"})
    assert eventos[-1]["data"]["estado"] == "pausado"


@pytest.mark.asyncio
async def test_a_confirmacion_carries_the_id_of_its_interrupt(grafo_que_interrumpe):
    """Sin el id, el panel no puede decir a CUAL pausa responde -- y con dos
    pausas pendientes LangGraph exige la forma de mapa
    `Command(resume={id: decision})`, asi que descartar el id dejaba el hilo
    sin ninguna entrada posible (ni aprobar ni cancelar). El id tiene que ser
    el de la interrupcion, no uno inventado por esta capa: se compara contra
    el que trae el estado del grafo."""
    eventos = [e async for e in eventos_sse(grafo_que_interrumpe, ENTRADA, CONFIG)]
    conf = [e for e in eventos if e["event"] == "confirmacion"][0]

    estado = grafo_que_interrumpe.get_state(CONFIG)
    pendientes = [i.id for t in estado.tasks for i in t.interrupts]
    assert conf["data"]["interrupt_id"] in pendientes
    # El payload de la herramienta sigue llegando entero al lado del id.
    assert conf["data"]["tipo"] == "confirmar_algo"


@pytest.mark.asyncio
async def test_an_interrupt_payload_that_is_not_a_dict_still_carries_its_id():
    """Las tres herramientas de escritura pasan un dict, pero una capa que
    hace `{**payload, "interrupt_id": ...}` revienta con cualquier otra cosa
    -- y esa `TypeError` saldria como el evento `error` generico ("hubo un
    problema"), escondiendo un problema de contrato y dejando al panel sin id
    con que responder."""

    @tool
    def confirmar_texto() -> dict:
        """Tool de juguete que interrumpe con un payload que no es dict."""
        return {"decision": interrupt("¿confirmas?")}

    grafo = _grafo_de_juguete(
        [{"name": "confirmar_texto", "args": {}, "id": "call_texto"}], [confirmar_texto]
    )
    eventos = [e async for e in eventos_sse(grafo, ENTRADA, CONFIG)]
    conf = [e for e in eventos if e["event"] == "confirmacion"][0]

    assert conf["data"]["valor"] == "¿confirmas?"
    assert conf["data"]["interrupt_id"]
    assert eventos[-1] == {"event": "fin", "data": {"estado": "pausado"}}


@pytest.mark.asyncio
async def test_an_exception_inside_a_tool_becomes_an_error_event_and_closes(grafo_que_revienta):
    """Falla numero 3 del Review Focus: ni excepcion colgada ni stream infinito."""
    eventos = [e async for e in eventos_sse(grafo_que_revienta, ENTRADA, CONFIG)]

    assert eventos[-1]["event"] == "error"
    assert "mensaje" in eventos[-1]["data"]


# ---------------------------------------------------------------------
# Fix Round 1 -- H1: `content` es una lista de bloques con el modelo real
# ---------------------------------------------------------------------


class FakeBlockContentModel(BaseChatModel):
    """Emula la forma real de `ChatAnthropic` con herramientas bindeadas.

    `FakeToolCallingModel` no puede exponer H1 de la revision: devuelve
    `content` como `str`, y esa es exactamente la forma que el modelo real
    NUNCA produce en este grafo -- `app/agent/graph.py` bindea las siete
    herramientas de `ALL_TOOLS`, y con herramientas bindeadas
    `langchain_anthropic.chat_models` fija `coerce_content_to_string =
    not _tools_in_params(payload) and ...`, que es siempre `False` ahi. Cada
    `AIMessage` real trae en cambio una LISTA de bloques (`text`, `tool_use`,
    `input_json_delta`, `thinking`). Este doble replica esa forma a mano --
    sin bindear nada de verdad, sin tocar Anthropic -- para que un test
    pueda ver el bug que un doble mas simple que la realidad dejaba pasar.
    """

    scripted_tool_calls: list[dict] = []
    n: int = 0

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self) -> str:
        return "fake-block-content"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.n += 1
        if self.n == 1 and self.scripted_tool_calls:
            llamada = self.scripted_tool_calls[0]
            msg = AIMessage(
                content=[
                    {"type": "text", "text": "Dale, ", "index": 0},
                    {
                        "type": "tool_use",
                        "id": llamada["id"],
                        "name": llamada["name"],
                        "input": llamada["args"],
                        "index": 1,
                    },
                    {
                        "type": "input_json_delta",
                        "partial_json": '{"cliente_id":"cli-1"}',
                        "index": 1,
                    },
                ],
                tool_calls=self.scripted_tool_calls,
            )
        else:
            msg = AIMessage(content=[{"type": "text", "text": "listo", "index": 0}])
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.mark.asyncio
async def test_token_events_carry_plain_text_even_when_content_is_a_block_list():
    """Pin de H1 de la revision. Con `content` como lista de bloques (la
    forma real, nunca la de `FakeToolCallingModel`), los `token` tienen que
    seguir siendo texto plano -- nunca la lista cruda, y nunca el JSON
    parcial de una tool_call disfrazado de respuesta del asistente."""
    model = FakeBlockContentModel(
        scripted_tool_calls=[
            {"name": "previsualizar_venta", "args": {"cliente_id": "cli-1"}, "id": "call_x"}
        ]
    )
    grafo = create_react_agent(
        model, [previsualizar_venta], checkpointer=MemorySaver(), version="v2"
    )
    config = {"configurable": {"thread_id": "bloques"}}

    eventos = [e async for e in eventos_sse(grafo, ENTRADA, config)]
    tokens = [e for e in eventos if e["event"] == "token"]
    textos = [t["data"]["texto"] for t in tokens]

    assert textos, "no se emitio ningun token"
    for texto in textos:
        assert isinstance(texto, str), f"el texto viajo como {type(texto)!r}, no str"
        assert "partial_json" not in texto
        assert "input_json_delta" not in texto
        assert texto != ""
    assert "Dale, " in textos
    assert "listo" in textos


# ---------------------------------------------------------------------
# Fix Round 1 -- H2: una `CancelledError` tiene que atravesar el generador,
# nunca volverse un evento `error`
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancelled_run_dies_instead_of_becoming_an_error_event():
    """El test de la Task 6 (`test_a_disconnected_client_cancels_the_run`)
    monkeypatchea `eventos_sse` entero -- nunca ejecuta el `except` de esta
    funcion. Si alguien cambiara `except Exception` por `except
    BaseException` aca, ese test seguiria en verde y la corrida quedaria
    huerfana gastando modelo. Este test corre la funcion REAL."""
    pasos = {"n": 0}

    @tool
    async def larga() -> str:
        """Corre mucho, para poder cancelarla a mitad."""
        for _ in range(40):
            await asyncio.sleep(0.1)
            pasos["n"] += 1
        return "fin"

    grafo = _grafo_de_juguete([{"name": "larga", "args": {}, "id": "c1"}], [larga])
    recibidos = []

    async def consumir():
        async for e in eventos_sse(grafo, ENTRADA, CONFIG):
            recibidos.append(e)

    t = asyncio.create_task(consumir())
    await asyncio.sleep(0.35)
    antes = pasos["n"]
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    await asyncio.sleep(0.5)

    assert t.cancelled()
    assert not any(e["event"] == "error" for e in recibidos)
    assert pasos["n"] == antes, "la corrida siguio despues de la cancelacion"
