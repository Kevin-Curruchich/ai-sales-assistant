import asyncio
import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent.streaming import eventos_sse
from app.api.dependencies import get_db, get_current_user
from app.models import User
from app.schemas.agent import AgentThreadCreate, AgentThreadRename, AgentThreadResponse
from app.services.agent_thread_service import AgentThreadService

router = APIRouter(prefix="/agent", tags=["Agente"])

DEFAULT_TITLE = "Conversación nueva"

# El titulo sale del primer mensaje, truncado -- ver docstring del diseño
# ("el titulo sale del primer mensaje truncado y se puede renombrar").
TITLE_MAX_LENGTH = 60

# Registro en memoria de los `thread_id` con una corrida activa. Un `set`
# alcanza -- pero SOLO porque el proceso es uno solo: `entrypoint.sh` termina
# en `exec hypercorn app.main:app --bind "0.0.0.0:${PORT:-8000}"`, sin
# `--workers`, y el default de hypercorn es un unico worker. Con mas de un
# proceso, dos workers verian cada uno su PROPIO `set` (memoria no
# compartida) y dos corridas podrian pisarse los checkpoints del mismo hilo
# sin que este registro se entere -- exactamente la corrupcion que el 409
# existe para evitar. El dia que se suba `--workers` en esa linea, este
# mecanismo deja de alcanzar y el registro tiene que mudarse a la base (una
# fila con un lock), no a otro `set` en memoria.
_hilos_en_curso: set[str] = set()


@router.get("/threads", response_model=list[AgentThreadResponse])
def list_threads(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = AgentThreadService(db)
    return service.list_for(current_user.id)


@router.post("/threads", response_model=AgentThreadResponse, status_code=status.HTTP_201_CREATED)
def create_thread(
    data: AgentThreadCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = AgentThreadService(db)
    return service.create(current_user.id, data.title or DEFAULT_TITLE)


@router.get("/threads/{thread_id}", response_model=AgentThreadResponse)
def get_thread(
    thread_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = AgentThreadService(db)
    return service.get_owned(thread_id, current_user.id)


@router.patch("/threads/{thread_id}", response_model=AgentThreadResponse)
def rename_thread(
    thread_id: uuid.UUID,
    data: AgentThreadRename,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = AgentThreadService(db)
    return service.rename_owned(thread_id, current_user.id, data.title)


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thread(
    thread_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = AgentThreadService(db)
    service.delete_owned(thread_id, current_user.id)


class AgentStreamRequest(BaseModel):
    """Cuerpo de `POST /stream`.

    `extra="ignore"`: un cliente puede mandar `user_id` o `configurable`
    tratando de hacerse pasar por otro usuario (ver el docstring de
    `stream_agent`). Pydantic los descarta en silencio en vez de fallar --
    es exactamente lo que se quiere: ignorarlos, no rechazar el pedido
    entero por un campo de mas.

    `interrupt_id` acompaña a `decision` y es OBLIGATORIO con ella: es el
    `interrupt_id` que vino en el evento `confirmacion` que se esta
    respondiendo (ver `app/agent/streaming.py`). Sin el no hay forma de decir
    a cual pausa responde la decision, y un mensaje del modelo puede dejar
    dos pausas abiertas. Campo hermano de `decision`, no una clave adentro:
    `decision` es el payload que la herramienta de escritura lee tal cual
    (`decision.get("accion")`, `decision.get("huella")` en
    `app/agent/tools/write.py`) y meterle una clave de transporte adentro
    mezclaria el sobre con la carta.
    """

    thread_id: uuid.UUID
    mensaje: Optional[str] = None
    decision: Optional[dict] = None
    interrupt_id: Optional[str] = None

    model_config = {"extra": "ignore"}


async def _pausas_pendientes(graph, config) -> set[str]:
    """Los `interrupt_id` que este hilo tiene pendientes de respuesta, ahora.

    `snapshot.interrupts` NO sirve para esto: sigue trayendo las pausas que YA
    se respondieron mientras el paso no termine (verificado contra langgraph
    1.0.3 -- despues de reanudar una de dos hermanas, la respondida sigue
    apareciendo ahi). Lo que distingue una de otra es la tarea: una tarea cuya
    pausa se respondio ya tiene `result`, la que sigue esperando lo tiene en
    `None`.
    """
    snapshot = await graph.aget_state(config)
    return {
        interrupcion.id
        for tarea in snapshot.tasks
        if tarea.result is None
        for interrupcion in tarea.interrupts
    }


def _sse_line(evento: dict) -> str:
    return f"event: {evento['event']}\ndata: {json.dumps(evento['data'], ensure_ascii=False)}\n\n"


@router.post("/stream")
async def stream_agent(
    data: AgentStreamRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Corre un turno del agente y devuelve sus eventos por SSE.

    ## La identidad es la unica linea de defensa

    Las herramientas de escritura leen `user_id` de `config["configurable"]`
    y firman la huella de aprobacion con ese valor. `user_id_from_config`
    (ver `app/agent/tools/write.py`) acepta y firma con CUALQUIER UUID
    sintacticamente valido que reciba -- no puede verificar que corresponda
    a una sesion autenticada, porque no tiene forma de saberlo. Antes habia
    un hook de autenticacion en el servidor de LangGraph que garantizaba
    esto; una task anterior lo borro junto con el servidor. Hoy este
    endpoint es lo UNICO que se interpone entre un cliente malicioso y una
    venta firmada a nombre de otro usuario.

    Por eso `config["configurable"]["user_id"]` sale EXCLUSIVAMENTE de
    `current_user.id` (la identidad que devolvio `get_current_user`, resuelta
    del token) y jamas de `data` -- ni de un `user_id` suelto en el cuerpo,
    ni de un `configurable` completo que el cliente intente colar. El
    `AgentStreamRequest` de arriba ni siquiera los retiene (`extra="ignore"`):
    no hay forma de que lleguen hasta aca.

    ## Orden de las validaciones

    `get_owned` corre ANTES de tocar el grafo o devolver el `StreamingResponse`:
    con SSE, el `200 OK` sale con el primer byte, y despues de eso el codigo
    de estado ya no se puede cambiar. Un 404 (hilo ajeno o inexistente) tiene
    que salir como codigo HTTP normal, antes de que arranque el stream.

    ## Por que el I/O de SQLAlchemy va por `run_in_threadpool`

    Esta funcion es `async def`, pero `AgentThreadService` usa una `Session`
    sincronica -- `get_owned`/`rename_owned` son llamadas bloqueantes de
    verdad. Los otros endpoints del archivo son `def` (no `async def`), asi
    que Starlette los manda solos al threadpool; este, al ser `async def`,
    corre en el event loop, y con hypercorn sin `--workers` (ver el
    docstring de `_hilos_en_curso`) ese es el UNICO event loop del proceso:
    una consulta lenta a Postgres bloqueando ahi frena TODOS los pedidos en
    vuelo, incluidos los streams de otros usuarios. `run_in_threadpool` (el
    mismo mecanismo que Starlette usa por debajo para los endpoints `def`)
    saca esas llamadas del loop.
    """
    service = AgentThreadService(db)
    thread = await run_in_threadpool(service.get_owned, data.thread_id, current_user.id)

    graph = request.app.state.agent_graph
    if graph is None:
        # El lifespan (`app/main.py`) deja `agent_graph = None` y sigue
        # sirviendo el resto de la API si Postgres no respondio al arrancar.
        # Sin este chequeo, `eventos_sse(None, ...)` explota con un
        # `AttributeError` que el cliente veria como un 500 opaco.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El agente no esta disponible en este momento. Intenta de nuevo en unos minutos.",
        )

    thread_key = str(thread.id)
    if thread_key in _hilos_en_curso:
        # DECISION: un hilo corre de a una corrida (ver el docstring de
        # `_hilos_en_curso`). Dos corridas sobre el mismo `thread_id`
        # escriben checkpoints que se pisan, y el resultado no es el de
        # ninguna de las dos -- rechazar es honesto, encolar seria construir
        # algo que nadie pidio.
        #
        # Chequeo y reserva (la linea de `_hilos_en_curso.add` de abajo) NO
        # tienen ningun `await` en el medio -- son dos operaciones puramente
        # en memoria sobre un event loop cooperativo de un solo hilo, asi
        # que dos pedidos concurrentes para el MISMO `thread_id` no pueden
        # entrelazarse entre el chequeo y la reserva. El `await
        # run_in_threadpool(...)` de arriba (para `get_owned`) queda ANTES
        # de este chequeo a proposito, para no romper esa atomicidad, y la
        # reserva quedo pegada aca abajo por lo mismo: la validacion de
        # `interrupt_id` lee el checkpoint (`await`) y meterla en el medio
        # habria abierto justo esa ventana.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya hay una corrida en curso para este hilo.",
        )

    _hilos_en_curso.add(thread_key)
    # Liberar el hilo NO puede depender de que el generador de mas abajo
    # llegue a correr ni una sola vez: si el cliente corta la conexion
    # ANTES de que Starlette pida el primer chunk (un panel que se
    # refresca, la red que se cae a mitad del POST), el cuerpo de
    # `generar()` nunca arranca y un `finally` puesto ahi adentro no
    # sirve de nada -- el `thread_id` quedaria reservado para siempre
    # (reproducido con hypercorn real). Por eso la reserva se ata al
    # CICLO DE VIDA DE LA TAREA que procesa este pedido -- la misma tarea
    # de asyncio corre el endpoint Y despues maneja la `StreamingResponse`
    # entera -- y no al generador: pase lo que pase (200 completo, error,
    # cancelacion por desconexion), cuando esa tarea termina, se libera.
    tarea_del_pedido = asyncio.current_task()
    if tarea_del_pedido is not None:
        tarea_del_pedido.add_done_callback(lambda _t: _hilos_en_curso.discard(thread_key))

    # El config lo arma este endpoint, del lado del servidor, exclusivamente
    # con la identidad del token -- ver el docstring de arriba.
    config = {
        "configurable": {
            "thread_id": thread_key,
            "user_id": str(current_user.id),
        }
    }

    if data.mensaje is not None:
        entrada = {"messages": [("user", data.mensaje)]}
    elif data.decision is not None:
        if not data.interrupt_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Mandá 'interrupt_id' junto con 'decision': es el id que vino en el "
                    "evento 'confirmacion' que estás respondiendo."
                ),
            )
        pendientes = await _pausas_pendientes(graph, config)
        if data.interrupt_id not in pendientes:
            # Un id que no corresponde a ninguna pausa pendiente -- inventado,
            # de otro hilo, o de una pausa que ya se respondio (un doble click
            # en "aprobar", una pestaña vieja). Sin este chequeo, LangGraph no
            # se queja: guarda el resume bajo un id que ninguna tarea reclama,
            # la tarea que seguia pausada vuelve a interrumpirse y el panel
            # recibe OTRA `confirmacion` como si nunca hubiera aprobado nada
            # -- verificado contra langgraph 1.0.3. Un 409 antes del primer
            # byte dice la verdad y es accionable: volve a leer las
            # confirmaciones pendientes de este hilo.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Esa confirmación no está pendiente en este hilo: o ya fue respondida, "
                    "o el 'interrupt_id' no corresponde. Volvé a pedir el estado del hilo "
                    "y respondé la confirmación que siga abierta."
                ),
            )
        # SIEMPRE la forma de mapa, tambien con una sola pausa pendiente. Un
        # resume escalar (`Command(resume=data.decision)`, lo que habia aca)
        # funciona con una pausa y levanta `RuntimeError` con dos
        # (`langgraph/pregel/_loop.py`), que `eventos_sse` traduce al evento
        # `error` generico: el hilo quedaba sin ninguna entrada posible, ni
        # aprobar ni cancelar. Un unico camino, en vez de uno que funciona y
        # otro que no.
        entrada = Command(resume={data.interrupt_id: data.decision})
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Mandá 'mensaje' o 'decision'.",
        )

    if data.mensaje is not None and thread.title == DEFAULT_TITLE:
        # El titulo sale del primer mensaje, truncado. HEURISTICA, no una
        # deteccion real de "primer mensaje": se compara el titulo actual
        # contra el default con el que `create_thread` siembra un hilo sin
        # titulo (`data.title or DEFAULT_TITLE`, y `title` es NOT NULL, asi
        # que no hay un estado NULL que preguntar en su lugar). Limite
        # conocido y aceptado: un hilo renombrado por el usuario a
        # EXACTAMENTE ese string default se pisa una vez, en su proximo
        # mensaje. El costo es cosmetico (un titulo, no un dato de negocio)
        # y no tiene test dedicado.
        await run_in_threadpool(
            service.rename_owned, thread.id, current_user.id, data.mensaje[:TITLE_MAX_LENGTH]
        )

    async def generar():
        # Iteracion directa, sin tarea propia ni sondeo de
        # `is_disconnected()`. Medido contra hypercorn 0.18.0 real (no
        # asumido): con `asyncio.ensure_future(agen.__anext__())` +
        # `is_disconnected()` en un bucle -- la version anterior de este
        # codigo -- el sondeo NUNCA ve la desconexion (hay un solo mensaje
        # `http.disconnect` y Starlette ya esta bloqueado esperandolo en
        # `listen_for_disconnect`; para cuando este sondeo llama a
        # `receive()` la cola esta vacia y el `CancelScope` ya cancelado de
        # `Request.is_disconnected` devuelve `False` para siempre) y la
        # corrida del grafo -- en OTRA tarea, la que crea `ensure_future`,
        # fuera del alcance de la cancelacion de Starlette -- sigue viva
        # gastando modelo despues de que el cliente se fue: 28 iteraciones
        # de mas en la medicion. Con este `async for` derecho, la
        # cancelacion que Starlette entrega (via el mismo mecanismo con el
        # que corta `stream_response`) llega directo a donde este generador
        # esta suspendido -- adentro de `eventos_sse`, que ya deja pasar
        # `CancelledError` sin convertirla en un evento `error`
        # (`tests/test_agent_streaming.py`) -- y la corrida muere ahi: 0
        # iteraciones de mas, medido. No se atrapa `GeneratorExit` ni
        # `CancelledError` en ningun lado de esta funcion.
        async for evento in eventos_sse(graph, entrada, config):
            yield _sse_line(evento)

    return StreamingResponse(generar(), media_type="text/event-stream")
