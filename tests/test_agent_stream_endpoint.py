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
"""

import asyncio
import contextlib
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

    El test de la desconexion (`test_a_disconnected_client_cancels_the_run`)
    usa la fixture `app`, no `client`, y monkeypatchea `eventos_sse` entero
    -- nunca llega a tocar este grafo, asi que la sustitucion es inofensiva
    ahi. El test del 503 pisa `app.state.agent_graph` a `None` en su propio
    cuerpo, despues de que `client` ya corrio el lifespan.
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


class _EnvoltorioDesconexionInmediata:
    """Hace que `httpx.ASGITransport` simule un cliente real que lee UN
    evento y se va a mitad de un stream -- cosa que, verificado de forma
    empirica (ver el informe de esta task), `httpx.ASGITransport` NO hace
    por si solo.

    `ASGITransport.handle_async_request` (httpx 0.28.1) corre la app ASGI
    ENTERA hasta el final antes de devolverle el control a quien la llamo:
    `ASGIResponseStream.__aiter__` hace `yield b"".join(self._body)` UNA
    sola vez, sobre el cuerpo YA COMPLETO. Confirmado con un cronometro: un
    generador de juguete que tarda 5 segundos en terminar hace que
    `async with ac.stream(...) as resp:` tarde esos mismos 5 segundos en
    ENTRAR al bloque -- para cuando el test lee la primera linea y hace
    `break`, la corrida entera ya paso, sin que nada la haya podido
    interrumpir. Ninguna forma de escribir el endpoint cambia esto: el
    cliente de prueba no tiene forma de "irse a mitad" de algo que ya
    termino.

    Este envoltorio le da a la prueba lo que un servidor ASGI real (como
    hypercorn) SI provee: corre la app real en una tarea propia, y reenvia
    los primeros dos mensajes ASGI (`http.response.start` y el PRIMER
    `http.response.body`) a httpx forzando `more_body=False` en el segundo
    -- asi httpx da la respuesta por terminada apenas llega el primer
    evento, exactamente lo que un cliente real veria si cortara la conexion
    ahi. Como el `receive()` que le pasa a la app real es el MISMO closure
    de httpx (no uno propio), y ese closure resuelve a `http.disconnect` en
    cuanto httpx considera la respuesta completa (`response_complete.wait()`
    en `httpx/_transports/asgi.py`), `request.is_disconnected()` -- que es
    lo que el endpoint sondea -- empieza a ver la desconexion DE VERDAD
    apenas se fuerza ese `more_body=False`. Verificado con un probe
    standalone antes de integrarlo aca: sin este envoltorio, el generador de
    juguete corre las 5 segundos completas; con el, se cancela en
    milisegundos.

    No cambia el endpoint ni el cuerpo del test -- es infraestructura de
    prueba, nada mas: un cliente real (o `client`, el `TestClient` sincrono
    que usan los otros siete tests de este archivo) nunca pasa por aca.
    """

    def __init__(self, real_app):
        self._real_app = real_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._real_app(scope, receive, send)
            return

        salida: asyncio.Queue = asyncio.Queue()

        async def _send_a_la_cola(message):
            await salida.put(message)

        tarea_app = asyncio.ensure_future(self._real_app(scope, receive, _send_a_la_cola))

        inicio = await salida.get()
        await send(inicio)

        primer_cuerpo = dict(await salida.get())
        primer_cuerpo["more_body"] = False
        await send(primer_cuerpo)

        # httpx ya considera la respuesta terminada -- el mismo `receive()`
        # que la app real recibio arriba resuelve a `http.disconnect` de
        # aca en mas. La tarea de la app sigue viva (esta en medio del
        # SEGUNDO evento): darle tiempo a que su propio sondeo de
        # `is_disconnected()` la agarre y se cierre sola.
        try:
            await asyncio.wait_for(tarea_app, timeout=2)
        except asyncio.TimeoutError:
            tarea_app.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tarea_app


@pytest.fixture
def app(db_session):
    """Como `client` (`tests/fixtures_http.py`), pero entrega una app ASGI
    envuelta en `_EnvoltorioDesconexionInmediata` en vez de pasar por
    `TestClient`: el test de la desconexion necesita manejar el transporte
    el mismo con `httpx.ASGITransport`, que es directo y asincronico -- por
    eso el comentario del test dice "va por ASGI directo" -- y ese
    transporte, sin el envoltorio, no puede simular una desconexion a
    mitad de un stream (ver el docstring de la clase de arriba).

    No dispara el lifespan real (no hace falta: el unico test que usa esta
    fixture monkeypatchea `eventos_sse` entero, asi que nunca toca
    `app.state.agent_graph` de verdad) -- solo necesita que no sea `None`
    para pasar el chequeo de 503.
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
    anterior = main.app.state.agent_graph
    main.app.state.agent_graph = object()
    try:
        yield _EnvoltorioDesconexionInmediata(main.app)
    finally:
        main.app.dependency_overrides.clear()
        main.app.state.agent_graph = anterior


async def _create_thread_async(app, user: User, title) -> dict:
    """Equivalente async de `_create_thread_as`, para el unico test que no
    puede pasar por `TestClient`."""
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
    main.app.state.agent_graph = None

    resp = client.post(
        "/api/v1/agent/stream",
        json={"thread_id": hilo["id"], "mensaje": "hola"},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 503


# ---------------------------------------------------------------------
# La desconexion cancela la corrida -- no la deja huerfana
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disconnected_client_cancels_the_run(app, seeded_user, monkeypatch):
    """Falla numero 2 del Review Focus: la corrida no queda huerfana gastando modelo.

    Con TestClient no se puede cortar a mitad, asi que va por ASGI directo: se
    lee un evento y se cierra el contexto del stream.  El grafo de juguete marca
    en `cancelado` cuando recibe CancelledError.
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
        async with ac.stream(
            "POST", "/api/v1/agent/stream",
            json={"thread_id": hilo["id"], "mensaje": "hola"},
            headers=_auth(seeded_user),
        ) as resp:
            async for _linea in resp.aiter_lines():
                break  # leimos el primer evento y nos vamos

    await asyncio.sleep(0.1)  # dejar que la cancelacion se propague
    assert cancelado["si"], "la corrida siguio despues de que el panel se fue"


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
