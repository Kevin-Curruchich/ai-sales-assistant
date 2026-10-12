"""El checkpointer asincrono crea su schema y persiste entre pausas."""

import uuid

import pytest
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command, interrupt
from sqlalchemy import text

from app.agent.graph import build_async_checkpointer
from tests.conftest import TEST_DATABASE_URL
from tests.test_agent_graph import FakeToolCallingModel


def _drop_schema(test_engine, schema: str) -> None:
    with test_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _tables_in(test_engine, schema: str) -> set[str]:
    with test_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"
            ),
            {"schema": schema},
        )
        return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_the_async_checkpointer_creates_its_schema(test_engine):
    schema = f"agent_test_{uuid.uuid4().hex[:8]}"
    _drop_schema(test_engine, schema)

    saver = await build_async_checkpointer(TEST_DATABASE_URL, schema)
    try:
        tablas = _tables_in(test_engine, schema)
        assert "checkpoints" in tablas
    finally:
        await saver.conn.close()


@pytest.mark.asyncio
async def test_an_interrupt_survives_and_resumes_on_the_async_saver(test_engine):
    """El mecanismo entero, en async: pausar, reanudar, y que lo aprobado valga.

    Es el equivalente asincrono del test que la rama anterior ya tiene contra el
    saver sincronico.  Vale escribirlo de nuevo porque el saver es OTRO: que el
    sincronico persista un interrupt no dice nada sobre este.

    Decisivo, no solo de nombre: la reanudacion corre contra un SEGUNDO saver y
    un SEGUNDO grafo, construidos de cero sobre el mismo schema, despues de
    cerrar el primero del todo (pool incluido). Con el mismo saver/grafo de
    principio a fin, un estado en memoria del proceso alcanzaria para pasar el
    test sin que el checkpoint haya viajado por Postgres -- que es exactamente
    la propiedad que esta task vino a probar.
    """
    schema = f"agent_test_{uuid.uuid4().hex[:8]}"
    _drop_schema(test_engine, schema)
    primer_saver = await build_async_checkpointer(TEST_DATABASE_URL, schema)

    visto = {}

    @tool
    def pregunta() -> str:
        """Se detiene a preguntar."""
        respuesta = interrupt({"que": "seguimos?"})
        visto["respuesta"] = respuesta
        return f"dijiste {respuesta}"

    primer_grafo = create_react_agent(
        FakeToolCallingModel(
            scripted_tool_calls=[{"name": "pregunta", "args": {}, "id": "1"}]
        ),
        [pregunta],
        checkpointer=primer_saver,
        version="v2",
    )
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    try:
        primero = await primer_grafo.ainvoke({"messages": [("user", "dale")]}, config)
        assert "__interrupt__" in primero
    finally:
        # Cerramos el primer saver del todo -- pool incluido -- antes de
        # reanudar: lo que sigue depende exclusivamente de lo que quedo
        # persistido en Postgres, no de nada que este proceso todavia tenga
        # en memoria.
        await primer_saver.conn.close()

    segundo_saver = await build_async_checkpointer(TEST_DATABASE_URL, schema)
    try:
        segundo_grafo = create_react_agent(
            FakeToolCallingModel(),
            [pregunta],
            checkpointer=segundo_saver,
            version="v2",
        )

        segundo = await segundo_grafo.ainvoke(Command(resume="si"), config)

        assert visto["respuesta"] == "si"
        assert "__interrupt__" not in segundo
        assert segundo["messages"][-1].content == "listo"
    finally:
        await segundo_saver.conn.close()
