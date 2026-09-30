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
"""

import pytest
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from langgraph.types import interrupt

from app.agent.signing import firmar
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


grafo_simple = _grafo_de_juguete([], [])

grafo_con_herramienta = _grafo_de_juguete(
    [{"name": "previsualizar_venta", "args": {"cliente_id": "cli-1"}, "id": "call_1"}],
    [previsualizar_venta],
)

grafo_que_interrumpe = _grafo_de_juguete(
    [{"name": "confirmar_algo", "args": {}, "id": "call_2"}],
    [confirmar_algo],
)

grafo_que_revienta = _grafo_de_juguete(
    [{"name": "revienta", "args": {}, "id": "call_3"}],
    [revienta],
)


@pytest.mark.asyncio
async def test_a_plain_answer_streams_tokens_then_ends():
    eventos = [e async for e in eventos_sse(grafo_simple, ENTRADA, CONFIG)]
    tipos = [e["event"] for e in eventos]

    assert tipos[0] == "token"
    assert tipos[-1] == "fin"
    assert eventos[-1]["data"]["estado"] == "completo"


@pytest.mark.asyncio
async def test_a_tool_call_announces_itself():
    eventos = [e async for e in eventos_sse(grafo_con_herramienta, ENTRADA, CONFIG)]
    herramientas = [e for e in eventos if e["event"] == "herramienta"]

    assert herramientas[0]["data"]["nombre"] == "previsualizar_venta"


@pytest.mark.asyncio
async def test_an_interrupt_becomes_a_confirmacion_carrying_its_huella():
    eventos = [e async for e in eventos_sse(grafo_que_interrumpe, ENTRADA, CONFIG)]
    conf = [e for e in eventos if e["event"] == "confirmacion"][0]

    assert set(conf["data"]["huella"]) == {"datos", "firma"}
    assert eventos[-1]["data"]["estado"] == "pausado"


@pytest.mark.asyncio
async def test_an_exception_inside_a_tool_becomes_an_error_event_and_closes():
    """Falla numero 3 del Review Focus: ni excepcion colgada ni stream infinito."""
    eventos = [e async for e in eventos_sse(grafo_que_revienta, ENTRADA, CONFIG)]

    assert eventos[-1]["event"] == "error"
    assert "mensaje" in eventos[-1]["data"]
