import asyncio
import contextlib
import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
    """

    thread_id: uuid.UUID
    mensaje: Optional[str] = None
    decision: Optional[dict] = None

    model_config = {"extra": "ignore"}


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
    """
    service = AgentThreadService(db)
    thread = service.get_owned(data.thread_id, current_user.id)

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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya hay una corrida en curso para este hilo.",
        )

    if data.mensaje is not None:
        entrada = {"messages": [("user", data.mensaje)]}
        if thread.title == DEFAULT_TITLE:
            # El titulo sale del primer mensaje, truncado. Se detecta "primer
            # mensaje" por el titulo seguir siendo el default con el que
            # `create_thread` lo sembro -- un hilo ya renombrado por el
            # usuario no se pisa.
            service.rename_owned(thread.id, current_user.id, data.mensaje[:TITLE_MAX_LENGTH])
    elif data.decision is not None:
        entrada = Command(resume=data.decision)
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Mandá 'mensaje' o 'decision'.",
        )

    # El config lo arma este endpoint, del lado del servidor, exclusivamente
    # con la identidad del token -- ver el docstring de arriba.
    config = {
        "configurable": {
            "thread_id": thread_key,
            "user_id": str(current_user.id),
        }
    }

    _hilos_en_curso.add(thread_key)

    async def generar():
        # `aclosing`, no un `try/finally` armado a mano sobre `agen`: cubre
        # los DOS caminos por los que este generador puede terminar. (1) El
        # sondeo de `is_disconnected()` de abajo lo detecta y hace `return`.
        # (2) Un servidor ASGI real (hypercorn, nunca este harness de tests
        # con `httpx.ASGITransport`, que no reporta la desconexion de un
        # socket que no existe) cancela la tarea que corre este generador
        # ANTES de que el sondeo alcance a correr -- ahi `agen` queda
        # suspendido sin que nada lo cierre, hasta que lo junte el
        # recolector (Fix Round 1 de `eventos_sse`, mismo riesgo). `aclosing`
        # garantiza `await agen.aclose()` en cualquiera de los dos casos.
        async with contextlib.aclosing(eventos_sse(graph, entrada, config)) as agen:
            try:
                while True:
                    tarea = asyncio.ensure_future(agen.__anext__())
                    # Entre eventos (que pueden tardar -- FIFO contra
                    # Postgres, tokens del modelo) se sondea la desconexion
                    # del cliente sin bloquear: si el panel se fue, se
                    # cancela la TAREA del grafo (no solo este generador) y
                    # se espera esa cancelacion antes de terminar.
                    while not tarea.done():
                        if await request.is_disconnected():
                            tarea.cancel()
                            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                                await tarea
                            return
                        await asyncio.sleep(0.01)
                    try:
                        evento = tarea.result()
                    except StopAsyncIteration:
                        return
                    yield _sse_line(evento)
                    # No se atrapa `GeneratorExit`/`CancelledError` en
                    # ningun lado de este generador: si Starlette decide
                    # cerrar el stream, esa cancelacion tiene que
                    # propagarse tal cual hasta `agen` (que `aclosing` va a
                    # cerrar) y no convertirse en otra cosa.
            finally:
                _hilos_en_curso.discard(thread_key)

    return StreamingResponse(generar(), media_type="text/event-stream")
