"""El grafo del agente conversacional: modelo, herramientas y checkpointer.

Limite heredado de Task 9, documentado en el modulo docstring de
`app/agent/tools/write.py`: `ToolNode` corre todas las llamadas a
herramienta de UN mismo mensaje del modelo como UNA sola tarea de Pregel.
Si una escritura de ese archivo ya se completo y comiteo dentro de esa
tarea, y una HERMANA en el mismo paso todavia esta pausada en un
`interrupt()`, reanudar ese paso vuelve a correr TODO el paso desde el
principio -- incluida la escritura que ya se habia completado. Confirmado
empiricamente contra langgraph 1.0.3 con un `ToolNode` cableado a mano como
un solo nodo.

Este grafo no tiene ese problema, y no porque las herramientas lo eviten
(no pueden: el docstring de write.py explica por que) sino porque
`create_react_agent` en `version="v2"` (el default en langgraph 1.0.3, se
fija explicito aca para no depender de que el default no cambie) NO manda
todas las tool_calls de un mensaje a una sola invocacion del nodo "tools":
reparte cada una por separado con `Send("tools",
ToolCallWithContext(tool_call=call, ...))` (ver
`langgraph/prebuilt/chat_agent_executor.py::should_continue`). Cada `Send`
es su propia tarea de Pregel, con su propio registro de "pending writes" en
el checkpoint -- si una tarea termina y otra (hermana, del mismo mensaje)
se queda pausada en `interrupt()`, reanudar esa hermana NO vuelve a
ejecutar la tarea que ya termino: LangGraph reusa su resultado cacheado.
`tests/test_agent_graph.py::test_a_completed_write_tool_does_not_replay_when_a_sibling_write_tool_is_still_interrupted`
lo prueba de punta a punta contra un checkpointer real, con las DOS
herramientas de escritura reales (`registrar_movimiento_caja` y
`registrar_venta`) en el mismo mensaje del modelo.

Eso vale SOLO para el camino de batching -- para la hermana pausada. No
cubre la otra forma de repeticion, que es peor porque no depende de que
haya dos herramientas en el mismo mensaje: las herramientas comitean a
Postgres FUERA de la transaccion de LangGraph, y entre ese commit y el
momento en que LangGraph anota el resultado de la tarea en el checkpoint
hay una ventana. Un proceso que muera ahi deja un checkpoint que no sabe
que la tarea termino, y reanudar el hilo vuelve a correr el cuerpo entero.
Eso lo cierra `app/agent/idempotency.py` -- no este grafo -- marcando cada
escritura con el id de la tarea de Pregel, dentro de la misma transaccion
que la escritura. Su docstring explica la clave y lo que queda afuera.
"""

from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.prebuilt import create_react_agent

from app.agent.prompt_runtime import prompt_con_fecha
from app.agent.tools import ALL_TOOLS

MODEL_NAME = "claude-sonnet-5"


def checkpointer_schema() -> str:
    """Schema de Postgres para las tablas de LangGraph -- NO el del negocio.

    El autogenerate de Alembic recorre el schema del negocio (`POSTGRES_SCHEMA`,
    ej. `db_dev`) comparandolo contra los modelos declarados en
    `app/models`. Las tablas que crea el checkpointer de LangGraph
    (`checkpoints`, `checkpoint_writes`, etc.) no tienen modelo declarado
    ahi -- Alembic las leeria como deriva y emitiria `drop_table` para cada
    una en cada migracion. Viven en un schema separado, `agent`, que Alembic
    nunca mira.
    """
    return "agent"


def build_graph(model, checkpointer: BaseCheckpointSaver):
    """Arma el grafo: el modelo, las diez herramientas del agente, y el
    checkpointer que persiste el estado entre pausas de `interrupt()`.

    `version="v2"` es lo que reparte cada tool_call del mismo mensaje como
    una tarea de Pregel separada -- ver el docstring del modulo.
    """
    return create_react_agent(
        model,
        ALL_TOOLS,
        # Callable, no el string: `create_react_agent` lo invoca EN CADA
        # TURNO, y la fecha de hoy tiene que resolverse ahi. El grafo se
        # construye una sola vez en el `lifespan`, asi que una fecha
        # interpolada al construirlo se congela el dia del despliegue.
        prompt=prompt_con_fecha,
        checkpointer=checkpointer,
        version="v2",
    )


def default_model() -> ChatAnthropic:
    return ChatAnthropic(model=MODEL_NAME)


async def build_async_checkpointer(conn_string: str, schema: str) -> AsyncPostgresSaver:
    """`AsyncPostgresSaver` sobre un `AsyncConnectionPool` apuntado a `schema`,
    para el grafo servido en proceso por FastAPI.

    Se construye UNA VEZ, en el lifespan de la app (ver `app/main.py`), y se
    guarda en `app.state`: construirlo por pedido abriria una conexion nueva
    por request y no la cerraria nadie -- la misma fuga que tenia la fabrica
    de `langgraph-api` (ver historia de este modulo) y que ya se arreglo una
    vez en esta rama.

    `PostgresSaver` (el saver sincronico) no implementa los metodos
    asincronos: los hereda de `BaseCheckpointSaver`, que levanta
    `NotImplementedError`. El grafo servido con `astream`/`ainvoke` necesita
    `AsyncPostgresSaver`.

    ## Por que un pool y no una conexion suelta

    `AsyncPostgresSaver` acepta tanto una `AsyncConnection` como un
    `AsyncConnectionPool` (`langgraph/checkpoint/postgres/_ainternal.py::Conn`
    es la union de ambos). Con una conexion suelta, si esa conexion muere --
    un reinicio de Postgres, un idle timeout, un blip de red en Railway --
    nada la reabre: el checkpointer queda roto por el resto de la vida del
    proceso y la unica salida es un redeploy. Un `AsyncConnectionPool` abre una
    conexion de reemplazo cuando la anterior se muere, y ademas recicla
    conexiones SANAS por su cuenta: con los defaults de `psycopg_pool` 3.2.8,
    `max_lifetime` (3600 s, con jitter de -5%) cierra y reemplaza cada
    conexion al cumplir ese tiempo, y `max_idle` (600 s) encoge el pool por
    encima de `min_size`. Las dos cosas significan lo mismo para este codigo:
    la conexion que el checkpointer usa NO es la misma para siempre -- toda
    conexion se recicla al menos una vez por hora, sana o no -- asi que el
    `search_path` tiene que fijarse en CADA conexion que el pool abra (ver
    `configure`, mas abajo) y no una vez al construirlo. Y sigue siendo una
    unica cosa construida en el lifespan que se cierra con `.close()`: no
    reintroduce la fuga de "una conexion nueva por pedido" que este modulo ya
    arreglo una vez.

    `min_size=1, max_size=3`: nada mas comparte este pool. El
    `asyncio.Lock` interno de `AsyncPostgresSaver` (`aio.py::_cursor`) ya
    serializa toda su E/S sobre UNA conexion prestada del pool a la vez, pool
    o no -- eso no lo resuelve tener mas conexiones disponibles, asi que el
    pool no elimina el head-of-line blocking del checkpointer. Lo que si
    resuelve es no quedar con una unica conexion muerta para siempre.

    `configure` fija el `search_path` en CADA conexion que el pool abre --
    inicial o de reemplazo tras una reconexion -- porque `search_path` es
    estado de sesion: no viaja con el pool, y sin esto una conexion de
    reemplazo apuntaria a `public`. Mismo procedimiento que el saver
    sincronico y por la misma razon (ver `checkpointer_schema`): `setup()`
    corre DDL sin calificar y no crea el schema, asi que el schema se crea
    aca, explicito, con una conexion ya abierta, ANTES de `setup()`.
    """
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    async def _set_search_path(conn) -> None:
        await conn.execute(f'SET search_path TO "{schema}"')

    pool = AsyncConnectionPool(
        conn_string,
        open=False,
        min_size=1,
        max_size=3,
        configure=_set_search_path,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    # Si el `open()`, el `CREATE SCHEMA` o el `setup()` revientan, el pool no
    # debe quedar abierto y sin dueño: nadie tendria una referencia para
    # cerrarlo y el servidor de Postgres se comeria sus conexiones hasta que
    # muera el proceso.
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
    except BaseException:
        await pool.close()
        raise
    return checkpointer
