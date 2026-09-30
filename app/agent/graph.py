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

import threading

from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.prebuilt import create_react_agent

from app.agent.prompt import SYSTEM_PROMPT
from app.agent.tools import ALL_TOOLS
from app.core.config import settings

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
    """Arma el grafo: el modelo, las siete herramientas del agente, y el
    checkpointer que persiste el estado entre pausas de `interrupt()`.

    `version="v2"` es lo que reparte cada tool_call del mismo mensaje como
    una tarea de Pregel separada -- ver el docstring del modulo.
    """
    return create_react_agent(
        model,
        ALL_TOOLS,
        prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
        version="v2",
    )


def _default_model() -> ChatAnthropic:
    return ChatAnthropic(model=MODEL_NAME)


def _postgres_checkpointer(conn_string: str, schema: str) -> BaseCheckpointSaver:
    """`PostgresSaver` apuntado a `schema`. Factorizada aparte de
    `_default_checkpointer()` (que la llama con la URL/schema de produccion)
    para poder probarla contra la base de test desechable sin tocar
    `settings.SQLALCHEMY_DATABASE_URI` -- ver
    `tests/test_agent_graph.py::test_postgres_checkpointer_creates_its_schema_before_setup`.

    `PostgresSaver` no tiene parametro de schema, y `setup()` corre DDL SIN
    calificar (`CREATE TABLE checkpoints`, no `CREATE TABLE agent.checkpoints`)
    -- resuelve donde aterriza por el `search_path` de la conexion, igual que
    `app/core/database.py` hace para el schema del negocio. Y, a diferencia
    de lo que un borrador anterior de esta spec afirmaba, `setup()` NO crea
    el schema: sus `MIGRATIONS` no tienen ningun `CREATE SCHEMA` (se puede
    confirmar leyendo `langgraph.checkpoint.postgres.PostgresSaver.MIGRATIONS`).
    Contra una base fresca -- un `db_local` nuevo, produccion, CI --
    `SET search_path TO "agent"` con `agent` inexistente no falla en el
    `SET` (Postgres lo permite: un schema del `search_path` que no existe
    simplemente se salta), sino recien en el primer `CREATE TABLE` de
    `setup()`, con `InvalidSchemaName: no schema has been selected to
    create in` -- confirmado corriendo ese `SET` + ese `CREATE TABLE`
    exactos contra una base sin el schema `agent`. Por eso el schema se crea
    aca, explicito, ANTES de fijar `search_path` y ANTES de `setup()` --el
    mismo `CREATE SCHEMA IF NOT EXISTS` que `alembic/env.py` corre para el
    schema del negocio, no algo que LangGraph hace por su cuenta.
    """
    from psycopg import Connection
    from psycopg.rows import dict_row

    from langgraph.checkpoint.postgres import PostgresSaver

    conn = Connection.connect(
        conn_string,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    )
    # Si algo de aca abajo revienta (el `CREATE SCHEMA` sin permisos, el
    # `setup()` contra una base a la que le falta una extension), la conexion
    # quedaba abierta y sin dueño: nadie tiene una referencia para cerrarla y
    # el pool del servidor de Postgres se la come hasta que muera el proceso.
    # Un arranque que falla y reintenta las iba acumulando de a una por
    # intento.
    try:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        conn.execute(f'SET search_path TO "{schema}"')
        checkpointer = PostgresSaver(conn)
        checkpointer.setup()
    except BaseException:
        conn.close()
        raise
    return checkpointer


def _default_checkpointer() -> BaseCheckpointSaver:
    """`PostgresSaver` de produccion, apuntado al schema `agent` (ver
    `checkpointer_schema`)."""
    return _postgres_checkpointer(settings.SQLALCHEMY_DATABASE_URI, checkpointer_schema())


_graph_singleton = None
_graph_lock = threading.Lock()


def graph(config: dict | None = None):
    """Fabrica del grafo de produccion, expuesta a `langgraph.json`.

    Es una funcion, no el grafo ya construido a nivel de modulo: construirlo
    a nivel de modulo abriria una conexion a Postgres e instanciaria
    `ChatAnthropic` (que valida que haya una API key) en el momento en que
    CUALQUIER cosa importe `app.agent.graph` -- incluidos los tests, que no
    tienen ni la base de desarrollo local levantada ni una API key.

    ## Por que se cachea

    `langgraph-api` llama a esta fabrica **una vez por corrida**, no una vez
    por proceso. Leido del fuente de langgraph-api 0.15.1, no supuesto:
    `GRAPHS[graph_id]` guarda la FABRICA (no el grafo); `graph.py:404` hace
    `value = invoke_factory(value, graph_id, config, factory_runtime)` dentro
    de `get_graph`, que es un `@asynccontextmanager`; y `stream.py:182-194`
    lo entra con `stack.enter_async_context(...)` en cada corrida. No hay
    ningun cache en el medio.

    Sin el cache de abajo, entonces, cada corrida construia un
    `_default_checkpointer()` nuevo: una `psycopg.Connection` nueva, un
    `CREATE SCHEMA`, un `PostgresSaver.setup()` completo -- y esa conexion
    no la cierra nadie, porque nada devuelve un asa para cerrarla. Una
    conexion de Postgres filtrada por corrida, hasta que el `max_connections`
    de la base termina el servicio. (`_postgres_checkpointer` cierra la
    conexion si `setup()` revienta; esta era la fuga del camino de EXITO, que
    corre siempre.)

    ## Lo que el servidor hace con lo que devolvemos

    Le cambia el checkpointer: `graph.py:416-422` hace
    `graph_obj.copy(update={"checkpointer": checkpointer, "store": store})`
    con el suyo. Asi que bajo el servidor completo, el `PostgresSaver` que
    arma `_default_checkpointer()` **no es el que persiste los checkpoints**
    -- lo hace el del servidor, con su propia configuracion de Postgres. El
    nuestro sigue siendo el que usan `langgraph dev` sin ese reemplazo, una
    construccion directa del grafo, y los tests. La seccion "El schema
    `agent`" de `docs/agente.md` describe el nuestro y lo dice ahi tambien.

    ## `config`

    Se acepta y se IGNORA. El servidor SI lo pasa -- una fabrica de un solo
    parametro recibe el `config` de la corrida
    (`_factory_utils.py::_classify_factory`) -- pero como el grafo se
    construye una sola vez y lo comparten todas las corridas, nada por
    corrida puede quedar horneado aca, y menos que nada la identidad de quien
    escribe. Esa identidad viaja por corrida, en el `configurable` que arma
    quien invoca el grafo, y la leen las herramientas con
    `user_id_from_config`.
    """
    global _graph_singleton
    if _graph_singleton is None:
        with _graph_lock:
            if _graph_singleton is None:
                _graph_singleton = build_graph(_default_model(), _default_checkpointer())
    return _graph_singleton
