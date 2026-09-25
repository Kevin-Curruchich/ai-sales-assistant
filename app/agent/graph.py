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
"""

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


def _default_checkpointer() -> BaseCheckpointSaver:
    """`PostgresSaver` apuntado al schema `agent` (ver `checkpointer_schema`).

    No hay parametro de schema en `PostgresSaver`: fija el schema con
    `search_path` a nivel de conexion, igual que `app/core/database.py` hace
    para el schema del negocio. `setup()` crea/migra las tablas del
    checkpointer si hace falta -- es idempotente, seguro de llamar en cada
    arranque del proceso.
    """
    from psycopg import Connection
    from psycopg.rows import dict_row

    from langgraph.checkpoint.postgres import PostgresSaver

    conn = Connection.connect(
        settings.SQLALCHEMY_DATABASE_URI,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options=f"-c search_path={checkpointer_schema()}",
    )
    checkpointer = PostgresSaver(conn)
    checkpointer.setup()
    return checkpointer


def graph(config: dict | None = None):
    """Fabrica del grafo de produccion, expuesta a `langgraph.json`.

    Es una funcion, no el grafo ya construido a nivel de modulo: construirlo
    a nivel de modulo abriria una conexion a Postgres e instanciaria
    `ChatAnthropic` (que valida que haya una API key) en el momento en que
    CUALQUIER cosa importe `app.agent.graph` -- incluidos los tests, que no
    tienen ni la base de desarrollo local levantada ni una API key. LangGraph
    llama a esta fabrica reci en cuando de verdad va a correr el grafo.
    """
    return build_graph(_default_model(), _default_checkpointer())
