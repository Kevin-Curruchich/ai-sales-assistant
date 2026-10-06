"""Harness de tests HTTP autenticados: un `client` (TestClient sobre
app.main.app) con `get_db` y `get_current_user` apuntados a la sesion de
prueba (mismo schema desechable que siembra `db_session`), mas `_auth` y
`_create_thread_as`.

Este repo no tenia ni un test de endpoint autenticado -- `test_startup.py`
solo golpea `/health`, que es publico. La Task 6 consume estos nombres; no
son un detalle de implementacion de esta task.
"""

import uuid

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

import app.main as main
from app.agent.graph import build_async_checkpointer as _build_checkpointer_real
from app.agent.graph import build_graph as _build_graph_real
from app.api.dependencies import bearer_scheme, get_current_user, get_db
from app.models import User


class _ModeloQueNoLlamaANadie(BaseChatModel):
    """Contesta una frase fija. No pide herramientas, no toca Anthropic.

    No se reusa `FakeToolCallingModel` (`tests/test_agent_graph.py`) para no
    hacer que este harness de fixtures dependa de un modulo de tests, y porque
    lo unico que hace falta aca es que el grafo exista."""

    @property
    def _llm_type(self) -> str:
        return "sin-modelo"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="listo"))]
        )


@pytest.fixture
def client(db_session, monkeypatch):
    """TestClient sobre app.main.app.

    `get_db` se overridea para devolver la misma `db_session` que siembra
    `seeded_user`/`second_user` (schema desechable, puerto 55432) -- la
    `SessionLocal` de la app apunta por defecto a la base de desarrollo
    (puerto 55433) y nunca veria esos datos.

    `get_current_user` se overridea para resolver el usuario directo contra
    esa misma sesion, usando el id de usuario que viaja como si fuera el
    token (ver `_auth`), sin pasar por Firebase.

    Y el grafo del agente se intercepta: entrar al `with TestClient(...)`
    corre el lifespan REAL de `app.main`, que sin esto abre un
    `AsyncConnectionPool` de verdad contra el puerto 55432, corre
    `AsyncPostgresSaver.setup()` y construye un `ChatAnthropic` -- una vez por
    test que use esta fixture. Local es rapido, pero ese `setup()` toma su lock
    sobre la tabla de migraciones del checkpointer, y es exactamente la forma
    de contencion que aparece cuando dos procesos de pytest corren contra la
    misma base. El reemplazo es un grafo de juguete con `MemorySaver`: alcanza
    para que `app.state.agent_graph` no sea `None` (el chequeo de 503 del
    endpoint) y no toca ni Postgres ni Anthropic.

    Un test que necesite otro grafo -- con herramientas reales, o con un espia
    en `astream` -- no pierde nada: esta fixture solo sustituye si nadie mas lo
    hizo (compara contra las funciones reales de `app.agent.graph`), asi que el
    parcheo de `tests/test_agent_stream_endpoint.py`, que corre antes por ser
    autouse, gana. La otra via es asignar `main.app.state.agent_graph` despues
    de que el lifespan corrio.
    """

    def _override_get_db():
        yield db_session

    def _override_get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    ) -> User:
        try:
            user_id = uuid.UUID(credentials.credentials)
        except ValueError:
            raise HTTPException(status_code=401, detail="token de prueba invalido")
        user = db_session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="usuario de prueba desconocido")
        return user

    async def _checkpointer_de_juguete(*_args, **_kwargs):
        # `None`: el lifespan lo guarda para cerrarlo al salir, y solo llama a
        # `.conn.close()` si no es `None`. Un doble con `close()` seria un
        # objeto de mas sin nada que cerrar.
        return None

    def _grafo_de_juguete(*_args, **_kwargs):
        return create_react_agent(
            _ModeloQueNoLlamaANadie(), [], checkpointer=MemorySaver(), version="v2"
        )

    if main.build_async_checkpointer is _build_checkpointer_real:
        monkeypatch.setattr(
            "app.main.build_async_checkpointer", _checkpointer_de_juguete
        )
    if main.build_graph is _build_graph_real:
        monkeypatch.setattr("app.main.build_graph", _grafo_de_juguete)

    main.app.dependency_overrides[get_db] = _override_get_db
    main.app.dependency_overrides[get_current_user] = _override_get_current_user
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()


def _auth(user: User) -> dict:
    """Cabeceras de autenticacion para un usuario de prueba: el id del
    usuario viaja como si fuera el token; el override de `get_current_user`
    en `client` lo resuelve directo contra la sesion de prueba."""
    return {"Authorization": f"Bearer {user.id}"}


def _create_thread_as(client: TestClient, user: User, title: str) -> dict:
    resp = client.post(
        "/api/v1/agent/threads",
        json={"title": title},
        headers=_auth(user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
