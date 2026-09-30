# El agente servido desde FastAPI — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Servir el grafo del agente desde la propia app FastAPI, con streaming SSE, hilos con dueño y autorización, eliminando el segundo proceso.

**Architecture:** El grafo corre dentro del proceso de FastAPI. El modelo se llama de forma asíncrona; las siete herramientas siguen siendo sincrónicas y LangChain las despacha a hilos de trabajo. El estado vive en `AsyncPostgresSaver` sobre el schema `agent`. La identidad vuelve a salir de `get_current_user`.

**Tech Stack:** Python 3.13, FastAPI, LangGraph 1.0.3, `langgraph-checkpoint-postgres` (`AsyncPostgresSaver`), psycopg 3, SQLAlchemy 2.0, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-agente-en-proceso-design.md`

## Global Constraints

- **Nunca escribir en `db_dev`.** Es el respaldo congelado. Los scripts lo rechazan sin `REVENEW_ALLOW_DB_DEV=1`. **Nunca setear esa variable.**
- **Tests sólo contra el Postgres desechable de `localhost:55432`.** Nunca un host `*.rlwy.net`.
- Base de desarrollo: `db_local` en `localhost:55433`. Ambas Postgres 17, igual que producción.
- **`venv/bin/pip` está roto.** Siempre `venv/bin/python -m pip`.
- **Dinero es `Decimal`** a 2 decimales; las cantidades son fraccionarias. Nunca `float`.
- **Las tablas del checkpointer van al schema `agent`**, nunca al de negocio: el `autogenerate` de Alembic las leería como deriva. `PostgresSaver` **no** crea el schema; hay que crearlo explícitamente.
- **La huella se guarda opaca y se devuelve byte por byte.** El panel no la recalcula.
- **El `user_id` se inyecta del lado del servidor**, nunca se lee del cuerpo del pedido.
- Baseline de la suite al empezar: **282 tests**.

## Review Focus

Cinco fallas que el spec implica y que ninguna task cubriría por defecto. Cada una tiene su test asignado.

1. **Un `thread_id` inexistente o de otro usuario** → 404 en ambos casos, sin revelar cuál es cuál → Task 2.
2. **El panel se desconecta a mitad del turno** → la corrida se cancela, no queda huérfana consumiendo el modelo → Task 6.
3. **Una excepción dentro de una herramienta a mitad del stream** → evento `error` y cierre limpio, no una excepción colgada ni un stream que nunca termina → Task 5.
4. **`user_id` en el cuerpo del pedido** → se ignora; la venta la firma quien dice el token → Task 6.
5. **Dos pedidos concurrentes sobre el mismo hilo** → el segundo no corrompe el estado del primero → Task 6.

---

## Estructura de archivos

| Archivo | Responsabilidad |
|---|---|
| `app/models/agent_thread.py` | El modelo `AgentThread` |
| `alembic/versions/<rev>_agent_threads.py` | Su migración |
| `app/repositories/agent_thread_repository.py` | CRUD |
| `app/services/agent_thread_service.py` | Dueño y autorización |
| `app/schemas/agent.py` | Entrada y salida de los endpoints |
| `app/agent/graph.py` | Modificar: checkpointer asíncrono, quitar el sincrónico |
| `app/agent/session.py` | Modificar: `user_id_from_config` lee una clave simple |
| `app/agent/streaming.py` | Traducir los eventos de `astream` a eventos SSE |
| `app/api/v1/endpoints/agent.py` | Los endpoints |
| `app/api/v1/router.py` | Montar el router nuevo |
| `app/main.py` | El checkpointer se crea UNA vez, en el lifespan |

---

## Task 1: El modelo de hilo y su migración

**Files:**
- Create: `app/models/agent_thread.py`, `app/repositories/agent_thread_repository.py`, `alembic/versions/<rev>_agent_threads.py`
- Modify: `app/models/__init__.py`
- Test: `tests/test_agent_thread_model.py`

**Interfaces:**
- Produces: `AgentThread` con `id: uuid.UUID`, `owner_id: uuid.UUID` (FK a `users.id`), `title: str`, `created_at`, `updated_at`. `AgentThreadRepository(db)` con `create(owner_id, title) -> AgentThread`, `get(thread_id) -> AgentThread | None`, `list_for_owner(owner_id) -> list[AgentThread]`, `rename(thread_id, title)`, `delete(thread_id)`.
- El `id` del hilo es **el mismo** que se le pasa al checkpointer como `thread_id`: una sola identidad por conversación.

- [ ] **Step 1: Write the failing test**

`tests/test_agent_thread_model.py`:

```python
"""El hilo es un objeto con dueno.  Su id es el mismo que usa el checkpointer:
una sola identidad por conversacion, no dos que haya que mantener en sincronia."""

import uuid

from app.models import AgentThread
from app.repositories.agent_thread_repository import AgentThreadRepository


def test_a_thread_belongs_to_the_user_that_created_it(db_session, seeded_user):
    repo = AgentThreadRepository(db_session)
    thread = repo.create(owner_id=seeded_user.id, title="vendi dos cartones")

    assert thread.owner_id == seeded_user.id
    assert isinstance(thread.id, uuid.UUID)
    assert thread.title == "vendi dos cartones"


def test_listing_returns_only_the_owners_threads(db_session, seeded_user, second_user):
    repo = AgentThreadRepository(db_session)
    mio = repo.create(owner_id=seeded_user.id, title="mio")
    repo.create(owner_id=second_user.id, title="del otro")

    listados = repo.list_for_owner(seeded_user.id)

    assert [t.id for t in listados] == [mio.id]


def test_renaming_and_deleting(db_session, seeded_user):
    repo = AgentThreadRepository(db_session)
    thread = repo.create(owner_id=seeded_user.id, title="sin titulo")

    repo.rename(thread.id, "ventas de la semana")
    assert repo.get(thread.id).title == "ventas de la semana"

    repo.delete(thread.id)
    assert repo.get(thread.id) is None
```

- [ ] **Step 2: Add the `second_user` fixture**

En `tests/fixtures_domain.py`, junto a `seeded_user`. Hace falta para todo test de autorización de aquí en adelante; sin un segundo usuario, "sólo el dueño" no se puede probar.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_agent_thread_model.py -v`
Expected: FAIL — `AgentThread` no existe.

- [ ] **Step 4: Write the model**

`app/models/agent_thread.py`, siguiendo el estilo de `app/models/cash_movement.py`:

```python
class AgentThread(Base):
    """Una conversacion con el agente.

    El `id` es tambien el `thread_id` que recibe el checkpointer de LangGraph,
    a proposito: dos identidades para la misma conversacion se desincronizan.
    """

    __tablename__ = "agent_threads"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )
```

Exportalo en `app/models/__init__.py` junto a los demás.

- [ ] **Step 5: Generate and review the migration**

```bash
POSTGRES_SCHEMA=db_local venv/bin/alembic revision --autogenerate -m "agent threads"
```

Revisá el archivo generado antes de aplicarlo: `autogenerate` no debe tocar ninguna otra tabla. Si emite cambios sobre tablas existentes, **parate** — significa que hay deriva entre los modelos y la base, y eso es un problema aparte.

```bash
POSTGRES_SCHEMA=db_local venv/bin/alembic upgrade head
```

- [ ] **Step 6: Write the repository**

`app/repositories/agent_thread_repository.py`, siguiendo el patrón de `app/repositories/cash_movement_repository.py`. `create` hace `add`/`flush`/`refresh` y **no** commitea: el service decide el límite de transacción, que es la convención que la rama anterior estableció.

- [ ] **Step 7: Run the tests and the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: PASS, 282 + los nuevos.

- [ ] **Step 8: Commit**

```bash
git add app/models/ app/repositories/agent_thread_repository.py alembic/versions/ tests/
git commit -m "feat: Add agent threads as an owned object"
```

---

## Task 2: Autorización y los endpoints de hilos

**Files:**
- Create: `app/services/agent_thread_service.py`, `app/schemas/agent.py`, `app/api/v1/endpoints/agent.py`
- Modify: `app/api/v1/router.py`
- Test: `tests/test_agent_thread_endpoints.py`

**Interfaces:**
- Consumes: `AgentThreadRepository` de la Task 1.
- Produces: `AgentThreadService(db)` con `create(owner_id, title)`, `list_for(owner_id)`, `get_owned(thread_id, user_id) -> AgentThread` — **levanta `HTTPException(404)` si no existe O si no es del usuario**, con el mismo código en ambos casos. `rename_owned`, `delete_owned`.
- Endpoints bajo `/api/v1/agent/threads`: `GET` (listar), `POST` (crear), `PATCH /{id}` (renombrar), `DELETE /{id}`.

- [ ] **Step 1: Write the failing test**

```python
"""Solo el dueno.  Hoy cualquier usuario autenticado puede reanudar cualquier
hilo -- la revision de la rama anterior lo marco y se dejo pasar porque habia un
solo usuario.  Con dos, deja de ser aceptable."""


def test_a_user_cannot_read_another_users_thread(client, seeded_user, second_user):
    ajeno = _create_thread_as(client, second_user, "del otro")

    resp = client.patch(
        f"/api/v1/agent/threads/{ajeno['id']}",
        json={"title": "secuestrado"},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 404


def test_a_thread_that_does_not_exist_answers_the_same_as_one_that_is_not_yours(
    client, seeded_user, second_user
):
    """El mismo codigo en los dos casos: un 403 revelaria que ese hilo existe."""
    inexistente = uuid.uuid4()
    ajeno = _create_thread_as(client, second_user, "del otro")

    a = client.get(f"/api/v1/agent/threads/{inexistente}", headers=_auth(seeded_user))
    b = client.get(f"/api/v1/agent/threads/{ajeno['id']}", headers=_auth(seeded_user))

    assert a.status_code == b.status_code == 404


def test_listing_shows_only_my_threads(client, seeded_user, second_user):
    _create_thread_as(client, seeded_user, "mio")
    _create_thread_as(client, second_user, "del otro")

    resp = client.get("/api/v1/agent/threads", headers=_auth(seeded_user))

    assert [t["title"] for t in resp.json()] == ["mio"]
```

`_auth` y `client` siguen el patrón que ya usan los tests de endpoints de este repo — leelos antes de inventar uno nuevo. Si no existe un `client` autenticado reutilizable, creálo en `tests/fixtures_domain.py` para que las tasks siguientes lo usen.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_agent_thread_endpoints.py -v`
Expected: FAIL — la ruta no existe.

- [ ] **Step 3: Write the service with the single authorization point**

```python
def get_owned(self, thread_id: uuid.UUID, user_id: uuid.UUID) -> AgentThread:
    """El unico lugar donde se decide si alguien puede tocar un hilo.

    Mismo 404 para "no existe" y para "no es tuyo": un 403 le diria a quien
    pregunta que ese hilo existe y es de otro.

    Cuando se agregue lectura compartida, se agrega ACA y en ningun otro lado.
    """
    thread = self.repo.get(thread_id)
    if thread is None or thread.owner_id != user_id:
        raise HTTPException(status_code=404, detail="Hilo no encontrado")
    return thread
```

- [ ] **Step 4: Write the endpoints**

`app/api/v1/endpoints/agent.py`, siguiendo el patrón de `app/api/v1/endpoints/follow_ups.py`: `router = APIRouter(prefix="/agent", tags=["Agente"])`, `Depends(get_db)` y `Depends(get_current_user)`.

El título al crear: si el cuerpo no trae uno, `"Conversación nueva"`. El primer mensaje lo renombra — eso va en la Task 6.

- [ ] **Step 5: Mount the router**

En `app/api/v1/router.py`, `api_router.include_router(agent.router)` junto a los demás.

- [ ] **Step 6: Run the tests and the full suite, then commit**

```bash
venv/bin/python -m pytest -q
git add app/services/agent_thread_service.py app/schemas/agent.py app/api/v1/endpoints/agent.py app/api/v1/router.py tests/
git commit -m "feat: Add thread endpoints, owned by the user that created them"
```

---

## Task 3: La identidad vuelve a la del servidor

**Files:**
- Modify: `app/agent/session.py`
- Delete: `app/agent/auth_hook.py`, `tests/test_agent_auth_hook.py`, `langgraph.json`
- Test: `tests/test_agent_session.py`

**Interfaces:**
- Produces: `user_id_from_config(config)` lee `config["configurable"]["user_id"]` — una clave simple — y levanta `AgentAuthError` si falta o no es un UUID.

**Por qué se puede simplificar:** la clave reservada `langgraph_auth_user_id` existía porque un servidor externo recibía el `configurable` del cliente y había que impedir que el cliente lo escribiera. Ahora el `configurable` lo arma tu endpoint, en tu proceso, con el `user_id` que salió de `get_current_user`. El cliente no puede tocarlo.

- [ ] **Step 1: Write the failing test**

```python
def test_reads_the_user_id_the_endpoint_injected():
    config = {"configurable": {"user_id": str(UN_UUID)}}
    assert user_id_from_config(config) == UN_UUID


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"configurable": {}},
        {"configurable": {"user_id": None}},
        {"configurable": {"user_id": ""}},
        {"configurable": {"user_id": "no-es-un-uuid"}},
    ],
)
def test_refuses_anything_that_is_not_a_user_id(config):
    with pytest.raises(AgentAuthError):
        user_id_from_config(config)
```

- [ ] **Step 2: Run to verify the first fails**

Run: `venv/bin/python -m pytest tests/test_agent_session.py -v`
Expected: el primero FALLA — hoy lee `langgraph_auth_user_id`.

- [ ] **Step 3: Simplify the function**

Quitá la lectura de la clave reservada y del objeto de usuario del servidor; dejá `config["configurable"]["user_id"]`. Actualizá el docstring: ya no hay un servidor externo, y decir que lo hay confundiría al próximo que lea.

- [ ] **Step 4: Delete what no longer has a reason to exist**

```bash
git rm app/agent/auth_hook.py tests/test_agent_auth_hook.py langgraph.json
```

Y quitá de `requirements.txt` lo que sólo servía al hook, si algo quedó.

- [ ] **Step 5: Run the full suite and commit**

Los tests de las herramientas de escritura usan `_config(...)`; puede que haya que ajustar esa función a la clave nueva. Es un cambio de una línea en el helper, no en las aserciones.

```bash
venv/bin/python -m pytest -q
git add -u && git commit -m "refactor: Take the user id from the endpoint, not from a server hook"
```

---

## Task 4: El checkpointer asíncrono

**Files:**
- Modify: `app/agent/graph.py`, `app/main.py`
- Test: `tests/test_agent_graph_async.py`

**Interfaces:**
- Produces: `async def build_async_checkpointer(conn_string, schema) -> AsyncPostgresSaver` que crea el schema y corre `setup()`; `build_graph(model, checkpointer)` no cambia. El checkpointer se construye **una vez**, en el lifespan de la app, y se guarda en `app.state`.

**Por qué en el lifespan y no por pedido:** construirlo por pedido abre una conexión nueva cada vez y no la cierra. Esa fuga ya se arregló una vez en esta rama; no la reintroduzcas.

**Verificado:** `PostgresSaver` no implementa los métodos asíncronos — los hereda de `BaseCheckpointSaver`, que levanta `NotImplementedError`. Con `astream` hace falta `AsyncPostgresSaver`.

- [ ] **Step 1: Write the failing test**

```python
"""El checkpointer asincrono crea su schema y persiste entre pausas."""

@pytest.mark.asyncio
async def test_the_async_checkpointer_creates_its_schema(test_engine):
    schema = f"agent_test_{uuid.uuid4().hex[:8]}"
    _drop_schema(test_engine, schema)

    saver = await build_async_checkpointer(TEST_DATABASE_URL, schema)

    tablas = _tables_in(test_engine, schema)
    assert "checkpoints" in tablas


@pytest.mark.asyncio
async def test_an_interrupt_survives_and_resumes_on_the_async_saver(test_engine):
    """El mecanismo entero, en async: pausar, reanudar, y que lo aprobado valga.

    Es el equivalente asincrono del test que la rama anterior ya tiene contra el
    saver sincronico.  Vale escribirlo de nuevo porque el saver es OTRO: que el
    sincronico persista un interrupt no dice nada sobre este.
    """
    schema = f"agent_test_{uuid.uuid4().hex[:8]}"
    _drop_schema(test_engine, schema)
    saver = await build_async_checkpointer(TEST_DATABASE_URL, schema)

    visto = {}

    @tool
    def pregunta() -> str:
        """Se detiene a preguntar."""
        respuesta = interrupt({"que": "seguimos?"})
        visto["respuesta"] = respuesta
        return f"dijiste {respuesta}"

    grafo = create_react_agent(
        FakeToolCallingModel([{"name": "pregunta", "args": {}, "id": "1"}]),
        [pregunta],
        checkpointer=saver,
        version="v2",
    )
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    primero = await grafo.ainvoke({"messages": [("user", "dale")]}, config)
    assert "__interrupt__" in primero

    await grafo.ainvoke(Command(resume="si"), config)
    assert visto["respuesta"] == "si"
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/test_agent_graph_async.py -v`
Expected: FAIL — la función no existe.

- [ ] **Step 3: Write the async builder**

Igual que `_postgres_checkpointer` pero asíncrono: `CREATE SCHEMA IF NOT EXISTS`, `SET search_path`, `AsyncPostgresSaver(conn)`, `await saver.setup()`. **Cerrá la conexión si algo falla entre abrirla y devolverla** — el sincrónico tenía esa fuga y quedó anotada como menor.

- [ ] **Step 4: Wire it into the lifespan**

En `app/main.py`, dentro del `lifespan` existente: construir el checkpointer y el grafo una vez, guardarlos en `app.state.agent_graph`, y cerrar la conexión al apagar.

- [ ] **Step 5: Delete the sync checkpointer**

`_postgres_checkpointer` y su test quedan sin llamadores. Borralos en el mismo commit.

- [ ] **Step 6: Run the full suite and commit**

---

## Task 5: Traducir `astream` a eventos SSE

**Files:**
- Create: `app/agent/streaming.py`
- Test: `tests/test_agent_streaming.py`

**Interfaces:**
- Produces: `async def eventos_sse(graph, entrada, config) -> AsyncIterator[dict]`, que devuelve dicts `{"event": str, "data": dict}` con los tipos `token`, `herramienta`, `confirmacion`, `fin`, `error`.

**Esta task es el corazón y se puede probar sin HTTP ni base**: un grafo de juguete con un modelo falso alcanza para fijar la traducción.

- [ ] **Step 1: Write the failing test**

```python
"""La traduccion de los eventos del grafo a los que el panel entiende."""

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
```

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Write the translation**

`graph.astream(entrada, config, stream_mode=["messages", "updates"])`. Los chunks de mensaje dan los `token`; las actualizaciones de nodo dan las llamadas a herramienta; `__interrupt__` da la `confirmacion`. Envolvé todo en un `try/except` que emita `error` y **termine el generador** — una excepción que escapa deja el stream colgado y el panel esperando para siempre.

- [ ] **Step 4: Run the tests and commit**

---

## Task 6: El endpoint de streaming

**Files:**
- Modify: `app/api/v1/endpoints/agent.py`
- Test: `tests/test_agent_stream_endpoint.py`

**Interfaces:**
- Consumes: `AgentThreadService.get_owned` (Task 2), `app.state.agent_graph` (Task 4), `eventos_sse` (Task 5).
- Produces: `POST /api/v1/agent/stream`, que devuelve `text/event-stream`.

- [ ] **Step 1: Write the failing tests**

```python
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


def test_streaming_on_someone_elses_thread_is_a_404(client, seeded_user, second_user):
    ajeno = _create_thread_as(client, second_user, "del otro")

    resp = client.post(
        "/api/v1/agent/stream",
        json={"thread_id": ajeno["id"], "mensaje": "hola"},
        headers=_auth(seeded_user),
    )

    assert resp.status_code == 404


def test_the_first_message_titles_the_thread(client, seeded_user):
    hilo = _create_thread_as(client, seeded_user, None)

    with client.stream("POST", "/api/v1/agent/stream",
                       json={"thread_id": hilo["id"], "mensaje": "vendi dos cartones a Aurita"},
                       headers=_auth(seeded_user)) as resp:
        _consume(resp)

    assert client.get(f"/api/v1/agent/threads/{hilo['id']}",
                      headers=_auth(seeded_user)).json()["title"].startswith("vendi dos cartones")


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
```

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Write the endpoint**

`StreamingResponse` con `media_type="text/event-stream"`. Antes de tocar el grafo: `get_owned(thread_id, current_user.id)` — así el 404 sale como código HTTP normal, **antes** de que empiece el stream y ya no se pueda cambiar el código.

El `config` lo arma el endpoint: `{"configurable": {"thread_id": str(hilo.id), "user_id": str(current_user.id)}}`. **Nunca de lo que venga en el cuerpo.**

Si el cuerpo trae `mensaje`, la entrada es `{"messages": [("user", mensaje)]}`; si trae `decision`, es `Command(resume=decision)`.

Para la desconexión: `await request.is_disconnected()` entre eventos, o el equivalente que use este FastAPI. Cancelá la tarea del grafo, no sólo el generador.

**Un hilo corre de a una corrida.** Mantené un registro en memoria de los
`thread_id` con una corrida activa —un `set` alcanza, con un proceso— y devolvé
409 si llega un segundo pedido sobre uno ocupado. Liberalo en un `finally`, para
que un turno que revienta no deje el hilo trabado sin forma de destrabarlo. Con
más de un proceso esto no alcanza y habría que llevarlo a la base; hoy hay uno
solo, y el plan lo dice en vez de fingir que escala.

- [ ] **Step 4: Run the tests, the full suite, and commit**

---

## Task 7: Documentación y limpieza

**Files:**
- Modify: `docs/agente.md`, `docs/desarrollo-local.md`, `.env.example`
- Delete: `.venv-agent/` (del disco; ya está en `.gitignore`)

- [ ] **Step 1: Rewrite `docs/agente.md`**

Lo que cambia: ya no hay dos procesos, ni `langgraph dev`, ni segundo servicio de Railway, ni conflicto de starlette, ni `langgraph.json`. Lo que se queda y hay que mover al contrato nuevo: la huella (opaca, byte por byte), los estados de una aprobación, el schema `agent`, y el checklist de despliegue con el `saldo_inicial`.

Agregá los eventos SSE con un ejemplo de cada uno, y la regla de que un error a mitad del turno es un evento y no un código.

- [ ] **Step 2: Update `.env.example`**

`ANTHROPIC_API_KEY` y `AGENT_HUELLA_SECRET` ahora las necesita **la API**, no un servicio aparte. Las `LANGSMITH_*` siguen igual.

- [ ] **Step 3: Note what the deploy needs**

Un solo servicio. `AGENT_HUELLA_SECRET` y `ANTHROPIC_API_KEY` en Railway, en el servicio que ya existe.

- [ ] **Step 4: Run the full suite and commit**
