"""El endpoint HTTP que expone el agente al panel: `POST /api/v1/agent/stream`.

Ningun test de este archivo toca Anthropic ni el schema `agent` real de
Postgres -- `_grafo_de_juguete` (autouse) monkeypatchea `app.main.build_graph`
y `app.main.build_async_checkpointer` para que el lifespan REAL de `client`
(TestClient sobre `app.main.app`, que SI corre el lifespan de verdad) arme un
grafo de juguete en vez de conectarse de verdad. Sin esto, cada test que
`client` levanta pagaria una llamada real al modelo.

El espia de `_last_config()` vive en la FRONTERA del grafo -- se monkeypatchea
`graph.astream`, que es exactamente lo que `eventos_sse` llama -- y no dentro
del endpoint: un espia puesto adentro podria dar verde sin que el endpoint sea
realmente quien arma el config, que es justo lo que
`test_the_user_id_comes_from_the_token_and_not_from_the_body` tiene que
demostrar.

Fix Round 1 (sobre el commit `b8719df`) tiro abajo el mecanismo de
desconexion que tenia el endpoint (sondeo de `is_disconnected()` + tarea
propia con `asyncio.ensure_future`): medido contra hypercorn 0.18.0 real, ese
mecanismo NUNCA detectaba la desconexion y la corrida del grafo seguia viva
gastando modelo -- peor que no hacer nada. El reemplazo es iterar el
generador derecho y dejar que la cancelacion de Starlette se propague sola
(ver el docstring de `stream_agent` en `app/api/v1/endpoints/agent.py`). Los
dos tests de desconexion de aca abajo (`test_a_disconnected_client_...` y
`test_a_client_that_disconnects_before_the_first_byte_...`) manejan la app
por ASGI directo y cancelan la TAREA que corre el pedido -- asi es como
Starlette entrega una desconexion real (via `listen_for_disconnect`, que
cancela el task group entero) -- en vez de depender de que
`httpx.ASGITransport` simule un socket que corta a mitad, cosa que no hace
(corre la app entera a completo antes de devolver el control; confirmado
empiricamente y documentado en `task-6-report.md`).
"""

import asyncio
import contextlib
import threading
import uuid

import httpx
import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

import app.main as main
from app.api.dependencies import bearer_scheme, get_current_user, get_db
from app.api.v1.endpoints import agent as agent_endpoints
from app.models import User
from app.services.agent_thread_service import AgentThreadService
from tests.fixtures_http import _auth, _create_thread_as
from tests.test_agent_graph import FakeToolCallingModel

_ultimo_config: dict = {"valor": None}


def _last_config():
    return _ultimo_config["valor"]


def _construir_grafo_de_juguete():
    """Grafo de juguete, con `MemorySaver` (no Postgres) y sin ninguna tool
    real -- este archivo prueba el endpoint, no el grafo ni las
    herramientas, que ya tienen sus propios tests."""
    model = FakeToolCallingModel(scripted_tool_calls=[])
    graph = create_react_agent(model, [], checkpointer=MemorySaver(), version="v2")

    original_astream = graph.astream

    def _astream_espia(entrada, config, **kwargs):
        _ultimo_config["valor"] = config
        return original_astream(entrada, config, **kwargs)

    # Monkeypatch de INSTANCIA, no de clase: cada test arma su propio grafo,
    # asi que esto no se filtra a otros tests.
    graph.astream = _astream_espia
    return graph


@pytest.fixture(autouse=True)
def _grafo_de_juguete(monkeypatch):
    """Intercepta la construccion del grafo DENTRO del lifespan real de
    `app.main` (`build_async_checkpointer` + `build_graph`, los nombres tal
    como los importa `app/main.py`), para que `client` -- que SI dispara ese
    lifespan al entrar al `with TestClient(...)` -- termine con un grafo de
    juguete en `app.state.agent_graph` en vez de uno conectado a Anthropic y
    al schema `agent` real.

    Los tests de desconexion usan la fixture `app`, no `client`, y
    monkeypatchean `eventos_sse` entero -- nunca llegan a tocar este grafo,
    asi que la sustitucion es inofensiva ahi. El test del 503 pisa
    `app.state.agent_graph` a `None` en su propio cuerpo (y lo restaura),
    despues de que `client` ya corrio el lifespan.
    """
    graph = _construir_grafo_de_juguete()

    async def _fake_build_async_checkpointer(*_args, **_kwargs):
        return None

    def _fake_build_graph(*_args, **_kwargs):
        return graph

    monkeypatch.setattr("app.main.build_async_checkpointer", _fake_build_async_checkpointer)
    monkeypatch.setattr("app.main.build_graph", _fake_build_graph)
    yield graph
    _ultimo_config["valor"] = None


@pytest.fixture(autouse=True)
def _hilos_en_curso_limpios():
    """`_hilos_en_curso` es un `set` a nivel de modulo en el endpoint --
    sobrevive entre tests si nadie lo limpia. Sin esto, un test que revienta
    a mitad (o `_marcar_ocupado`, que lo toca directo) podria dejar un
    `thread_id` marcado como ocupado para el resto de la suite."""
    agent_endpoints._hilos_en_curso.clear()
    yield
    agent_endpoints._hilos_en_curso.clear()


def _consume(resp):
    """Agota un streaming response de `TestClient.stream(...)`."""
    for _ in resp.iter_lines():
        pass


def _marcar_ocupado(thread_id) -> None:
    """Mismo registro que usa el endpoint -- no una copia."""
    agent_endpoints._hilos_en_curso.add(str(thread_id))


@pytest.fixture
def app(db_session):
    """Como `client` (`tests/fixtures_http.py`), pero entrega la app ASGI
    cruda en vez de envolverla en `TestClient`: los tests de desconexion
    necesitan manejar el transporte ellos mismos con `httpx.ASGITransport` y
    cancelar la tarea que corre el pedido a mano -- `TestClient` no expone
    esa tarea.

    No dispara el lifespan real (no hace falta: los tests que usan esta
    fixture monkeypatchean `eventos_sse` entero, asi que nunca tocan
    `app.state.agent_graph` de verdad) -- solo necesita que no sea `None`
    para pasar el chequeo de 503. `getattr(..., None)`, no acceso directo:
    si esta fixture corre antes que `client` en algun orden de tests, el
    lifespan real todavia no puso `agent_graph` en `app.state` y el acceso
    directo tira `KeyError` (Fix Round 1, FIX 5 -- reproducido con
    `pytest -k disconnected`).
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

    main.app.dependency_overrides[get_db] = _override_get_db
    main.app.dependency_overrides[get_current_user] = _override_get_current_user
    anterior = getattr(main.app.state, "agent_graph", None)
    main.app.state.agent_graph = object()
    try:
        yield main.app
    finally:
        main.app.dependency_overrides.clear()
        main.app.state.agent_graph = anterior


async def _create_thread_async(app, user: User, title) -> dict:
    """Equivalente async de `_create_thread_as`, para los tests que no
    pueden pasar por `TestClient`."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.post(
            "/api/v1/agent/threads", json={"title": title}, headers=_auth(user)
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------
# La identidad: la unica linea de defensa (ver docstring de `stream_agent`)
# ---------------------------------------------------------------------


def test_the_user_id_comes_from_the_token_and_not_from_the_body(
    client, seeded_user, second_user
):
    """Falla numero 4 del Review Focus.  Mandar otro user_id no cambia quien firma."""
    hilo = _create_thread_as(client, seeded_user, "mio")

    with client.stream(
        "POST",
        "/api/v1/agent/stream",
        json={"thread_id": hilo["id"], "mensaje": "hola", "user_id": str(second_user.id)},
        headers=_auth(seeded_user),
    ) as resp:
        _consume(resp)

    # el config que recibio el grafo lleva el id del token, no el del cuerpo
    assert _last_config()["configurable"]["user_id"] == str(seeded_user.id)


def test_the_user_id_does_not_come_from_a_spoofed_configurable_in_the_body(
    client, seeded_user, second_user
):
    """Segunda forma del mismo ataque (agregada por decision explicita para
    esta task, no parte del texto literal del brief): un cliente que quiera
    hacerse pasar por otro probaria mandar el `configurable` COMPLETO antes
    que un `user_id` suelto -- es la forma mas obvia de intentarlo, dado que
    es tal cual la forma que arma el endpoint."""
    hilo = _create_thread_as(client, seeded_user, "mio")

    with client.stream(
        "POST",
        "/api/v1/agent/stream",
        json={
            "thread_id": hilo["id"],
            "mensaje": "hola",
            "configurable": {"user_id": str(second_user.id), "thread_id": "otro-hilo"},
        },
        headers=_auth(seeded_user),
    ) as resp:
        _consume(resp)

    assert _last_config()["configurable"]["user_id"] == str(seeded_user.id)
    assert _last_config()["configurable"]["thread_id"] == hilo["id"]


def test_streaming_on_someone_elses_thread_is_a_404(client, seeded_user, second_user):
    ajeno = _create_thread_as(client, second_user, "del otro")

    resp = client.post(
        "/api/v1/agent/stream",
        json={"thread_id": ajeno["id"], "mensaje": "hola"},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 404


def test_the_first_message_titles_the_thread(client, seeded_user, db_session):
    hilo = _create_thread_as(client, seeded_user, None)

    with client.stream("POST", "/api/v1/agent/stream",
                       json={"thread_id": hilo["id"], "mensaje": "vendi dos cartones a Aurita"},
                       headers=_auth(seeded_user)) as resp:
        _consume(resp)

    # Trampa del harness: `client` le entrega la MISMA `db_session` a todos
    # los pedidos, asi que una relectura via el identity map de SQLAlchemy
    # podria devolver el objeto ya mutado en memoria SIN que el UPDATE se
    # haya comiteado de verdad. `expire_all()` fuerza a que el GET de abajo
    # dispare un SELECT real contra Postgres -- si el endpoint se hubiera
    # olvidado de comitear, esto lo agarraria; sin el expire, un bug asi
    # pasaria en verde igual.
    db_session.expire_all()

    assert client.get(f"/api/v1/agent/threads/{hilo['id']}",
                      headers=_auth(seeded_user)).json()["title"].startswith("vendi dos cartones")


def test_streaming_without_mensaje_or_decision_is_a_422(client, seeded_user):
    """Fix Round 1, FIX 4: comportamiento agregado fuera del brief
    (`stream_agent` no sabe que `entrada` armar si el cuerpo no trae ninguno
    de los dos) que no tenia test -- comportamiento sin test es
    comportamiento que nadie prometio mantener."""
    hilo = _create_thread_as(client, seeded_user, "mio")

    resp = client.post(
        "/api/v1/agent/stream",
        json={"thread_id": hilo["id"]},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 422


# ---------------------------------------------------------------------
# El grafo no disponible: 503, no un 500 opaco (decision fuera del brief)
# ---------------------------------------------------------------------


def test_streaming_returns_503_when_the_graph_is_not_available(client, seeded_user):
    """Una task anterior cambio el lifespan para que un Postgres caido al
    arrancar no tumbe la API: agrega `agent_graph_init_failed` a
    `startup_issues` y deja `app.state.agent_graph = None`, sirviendo el
    resto de la API igual. Sin este chequeo, `eventos_sse(None, ...)`
    explotaria con un `AttributeError` que el cliente veria como un 500
    opaco."""
    hilo = _create_thread_as(client, seeded_user, "mio")
    anterior = main.app.state.agent_graph
    main.app.state.agent_graph = None
    try:
        resp = client.post(
            "/api/v1/agent/stream",
            json={"thread_id": hilo["id"], "mensaje": "hola"},
            headers=_auth(seeded_user),
        )
        assert resp.status_code == 503
    finally:
        # Fix Round 1, FIX 7: sin restaurar, este test deja
        # `app.state.agent_graph` en `None` para lo que corra despues en el
        # mismo proceso. Hoy es inofensivo porque `client` vuelve a correr
        # el lifespan en cada test, pero es una dependencia de orden
        # esperando su turno -- se restaura aca para no depender de eso.
        main.app.state.agent_graph = anterior


# ---------------------------------------------------------------------
# La desconexion cancela la corrida -- no la deja huerfana
#
# Fix Round 1, FIX 1: la version anterior de estos tests usaba
# `httpx.ASGITransport` + `async with ac.stream(...) as resp: ... break`,
# confiando en que el cliente "se fuera" a mitad del stream. Eso NUNCA pasa
# de verdad: `ASGITransport.handle_async_request` corre la app entera hasta
# el final antes de devolverle el control a quien la llamo, asi que para
# cuando el test lee la primera linea, la corrida ya termino sin que nada
# la interrumpiera. El reemplazo -- sugerido en la revision, verificado acá
# -- es manejar la app por ASGI directo y CANCELAR LA TAREA que corre el
# pedido: es exactamente el mecanismo que Starlette usa para propagar una
# desconexion real (`StreamingResponse.__call__` corre `stream_response` y
# `listen_for_disconnect` en un mismo task group; cancelar la tarea que los
# contiene a ambos cancela el grupo entero), asi que cancelar la tarea de
# `ac.post(...)` reproduce ese camino sin necesitar un socket real.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disconnected_client_cancels_the_run(app, seeded_user, monkeypatch):
    """Falla numero 2 del Review Focus: la corrida no queda huerfana gastando modelo.

    El grafo de juguete marca en `cancelado` cuando recibe `CancelledError`.
    Mutacion verificada (evidencia en `task-6-report.md`): con el mecanismo
    de sondeo que tenia este endpoint antes de Fix Round 1 (una tarea propia
    con `asyncio.ensure_future` + `is_disconnected()`), este mismo test
    queda rojo -- la corrida sigue viva. Con la iteracion directa
    (`async for evento in eventos_sse(...): yield ...`), queda verde.
    """
    cancelado = {"si": False}

    async def grafo_lento(*_a, **_kw):
        try:
            yield {"event": "token", "data": {"texto": "pensando"}}
            await asyncio.sleep(5)
            yield {"event": "fin", "data": {"estado": "completo"}}
        except asyncio.CancelledError:
            cancelado["si"] = True
            raise

    monkeypatch.setattr("app.api.v1.endpoints.agent.eventos_sse", grafo_lento)
    hilo = await _create_thread_async(app, seeded_user, "mio")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        tarea_pedido = asyncio.ensure_future(
            ac.post(
                "/api/v1/agent/stream",
                json={"thread_id": hilo["id"], "mensaje": "hola"},
                headers=_auth(seeded_user),
            )
        )
        await asyncio.sleep(0.2)  # dejar salir el primer evento ("token")
        tarea_pedido.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await tarea_pedido

    await asyncio.sleep(0.1)  # dejar que la cancelacion termine de propagarse
    assert cancelado["si"], "la corrida siguio despues de que el panel se fue"


@pytest.mark.asyncio
async def test_a_client_that_disconnects_before_the_first_byte_still_frees_the_thread(
    app, seeded_user, monkeypatch
):
    """Fix Round 1, FIX 2 (regresion). `_hilos_en_curso.add(...)` corre en
    el cuerpo sincronico de `stream_agent`, ANTES de que `generar()` exista
    siquiera -- todavia queda un `await run_in_threadpool(rename_owned...)`
    en el medio (el titulo del primer mensaje). Si el pedido se cancela
    justo ahi -- reservado, pero el generador ni construido -- un `finally`
    puesto DENTRO del generador (como tenia este codigo antes del fix) no
    corre nunca, y el hilo queda trabado para siempre.

    Este test fuerza esa ventana exacta con un `threading.Event` en vez de
    confiar en el timing de una desconexion real: el `rename_owned` de
    juguete se cuelga adentro del threadpool hasta que el test lo libera,
    dando una ventana determinista en la que la reserva YA paso pero el
    `StreamingResponse` (y su generador) todavia no se construyo.
    """
    entro_al_rename = threading.Event()
    seguir = threading.Event()
    termino_el_rename = threading.Event()
    rename_real = AgentThreadService.rename_owned

    def rename_que_se_cuelga(self, thread_id, user_id, title):
        entro_al_rename.set()
        try:
            if not seguir.wait(timeout=5):
                raise AssertionError("el test nunca libero el rename colgado")
            return rename_real(self, thread_id, user_id, title)
        finally:
            termino_el_rename.set()

    monkeypatch.setattr(AgentThreadService, "rename_owned", rename_que_se_cuelga)

    # titulo None -> DEFAULT_TITLE, para que el endpoint dispare el rename
    hilo = await _create_thread_async(app, seeded_user, None)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            tarea_pedido = asyncio.ensure_future(
                ac.post(
                    "/api/v1/agent/stream",
                    json={"thread_id": hilo["id"], "mensaje": "hola"},
                    headers=_auth(seeded_user),
                )
            )

            # Esperar (en un hilo aparte, para no bloquear el loop) a que el
            # rename de juguete confirme que arranco.
            loop = asyncio.get_running_loop()
            llego = await loop.run_in_executor(None, entro_al_rename.wait, 5)
            assert llego, "el rename nunca arranco -- este test no prueba la ventana que dice"

            # Reservado, pero `generar()` ni se definio: la linea textual
            # `_hilos_en_curso.add(...)` corre antes del `await
            # run_in_threadpool(rename_owned...)`; el `return
            # StreamingResponse(generar(), ...)` esta DESPUES.
            assert hilo["id"] in agent_endpoints._hilos_en_curso

            tarea_pedido.cancel()
            seguir.set()  # destrabar el hilo de threadpool para que la tarea pueda cerrar
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tarea_pedido

            # Esperar EXPLICITAMENTE a que el hilo de threadpool termine del
            # todo -- mas alla de lo que la cancelacion de `tarea_pedido`
            # ya haya resuelto -- antes de soltar `async with ac` y la
            # fixture `db_session`. `run_in_threadpool` no mata el hilo real
            # al cancelar (`abandon_on_cancel=False` es el default de
            # anyio); sin este `wait` explicito, un teardown de fixture que
            # cierre la sesion mientras el hilo todavia esta a mitad de su
            # propio `commit()` sobre la MISMA sesion compartida es una
            # carrera de verdad -- da igual lo que haga `tarea_pedido`, no
            # es lo que este assert quiere ejercitar.
            terminado = await loop.run_in_executor(None, termino_el_rename.wait, 5)
            assert terminado, "el hilo de threadpool nunca termino"
    finally:
        seguir.set()  # por si algo arriba salio antes de llegar a destrabarlo

    assert hilo["id"] not in agent_endpoints._hilos_en_curso, "el hilo quedo trabado para siempre"


# ---------------------------------------------------------------------
# Un hilo corre de a una corrida
# ---------------------------------------------------------------------


def test_a_second_run_on_a_busy_thread_is_rejected(client, seeded_user, monkeypatch):
    """Falla numero 5 del Review Focus.

    DECISION: un hilo corre de a una.  El segundo pedido recibe 409, no se
    encola ni se entrelaza.  Dos corridas sobre un mismo `thread_id` escriben
    checkpoints que se pisan, y el estado resultante no es el de ninguna de las
    dos.  Rechazar es honesto; encolar seria construir una cola que nadie pidio.
    """
    hilo = _create_thread_as(client, seeded_user, "mio")
    _marcar_ocupado(hilo["id"])  # el mismo registro que usa el endpoint

    resp = client.post(
        "/api/v1/agent/stream",
        json={"thread_id": hilo["id"], "mensaje": "hola"},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 409
    assert "en curso" in resp.json()["detail"].lower()


def test_the_thread_is_released_when_the_run_ends(client, seeded_user):
    """Y se libera pase lo que pase: sin esto, un turno que falla deja el hilo
    trabado para siempre y el usuario sin forma de destrabarlo."""
    hilo = _create_thread_as(client, seeded_user, "mio")

    with client.stream("POST", "/api/v1/agent/stream",
                       json={"thread_id": hilo["id"], "mensaje": "hola"},
                       headers=_auth(seeded_user)) as resp:
        _consume(resp)

    segunda = client.post(
        "/api/v1/agent/stream",
        json={"thread_id": hilo["id"], "mensaje": "otra vez"},
        headers=_auth(seeded_user),
    )
    assert segunda.status_code != 409
