"""Una escritura por tarea, aunque la tarea se re-ejecute.

## El problema

Las herramientas de escritura comitean a Postgres por su cuenta, FUERA de la
transaccion de LangGraph. Entre ese commit y el momento en que LangGraph
anota el resultado de la tarea en el checkpoint hay una ventana: si el
proceso muere ahi, el checkpoint no sabe que la tarea termino, y reanudar el
hilo vuelve a correr el cuerpo ENTERO desde el principio -- incluida la
escritura que ya se comiteo. El docstring de modulo de
`app/agent/tools/write.py` enumera lo que eso produce por herramienta
(compra duplicada con su stock y su salida de caja; movimiento de caja
duplicado siempre; venta duplicada cuando el lote da de sobra).

Nada dentro de esas funciones puede distinguir esa repeticion de una
solicitud nueva con los mismos valores. Hace falta una clave que viaje POR
FUERA de ellas y que sobreviva a la reanudacion.

## La clave

`config["configurable"]["__pregel_task_id"]` -- el id de la tarea de Pregel
que esta corriendo la tool call. Verificado empiricamente contra langgraph
1.0.3, con `create_react_agent(version="v2")` y un checkpointer real:

- es DISTINTO para cada tool_call del mismo mensaje del modelo (cada una es
  su propia tarea, repartida con `Send()`), y
- es EL MISMO antes y despues de una pausa de `interrupt()`: en la
  reanudacion, la tarea re-ejecutada vuelve a ver el mismo id.

Esa segunda propiedad es la que importa, y no es casualidad: el id se deriva
del checkpoint (id del checkpoint + nodo + camino), no de nada en memoria,
asi que se recalcula identico despues de un reinicio del proceso.

Se prefiere `checkpoint_ns` (`"tools:<task_id>"`, una clave publica de
LangGraph) cuando esta, y se cae a `__pregel_task_id` si no. La clave se
compone con el `thread_id` para que sea legible en la tabla y para no
depender de que el id de tarea sea globalmente unico.

Si no hay ninguna de las dos -- una invocacion directa de la herramienta,
fuera de un grafo -- no hay reanudacion posible y no hay nada que
desduplicar: la guardia se desactiva sola.

## Donde se anota

En `agent.tool_writes`, en el MISMO schema `agent` que ya aloja las tablas
del checkpointer, y por la misma razon: Alembic recorre el schema del
negocio comparandolo contra `app/models`, y una tabla sin modelo declarado
ahi se leeria como deriva y generaria un `drop_table` en cada migracion.
`agent` esta fuera de su radar a proposito (ver `docs/agente.md`).

La fila se inserta en la MISMA sesion y por lo tanto en la MISMA transaccion
que la escritura del negocio, y sin commit propio: la comitea el `commit()`
del service. O quedan las dos o no queda ninguna. Anotarla en una
transaccion aparte seria volver a tener el mismo agujero, un nivel mas
adentro.

## Lo que NO cierra

Ver `docs/agente.md`, seccion "Idempotencia": `registrar_compra` escribe en
DOS transacciones (`create()` y despues `confirm()`), y la marca viaja con
la primera. Un proceso que muera ENTRE las dos deja un borrador sin
confirmar que la reanudacion ya no vuelve a tocar.
"""

import threading

from sqlalchemy import text

#: Mismo schema que el checkpointer de LangGraph -- fuera del radar de
#: Alembic. Ver `app/agent/graph.py::checkpointer_schema`.
AGENT_SCHEMA = "agent"
TABLE = "tool_writes"
QUALIFIED = f'"{AGENT_SCHEMA}"."{TABLE}"'

_ddl_done = False
_ddl_lock = threading.Lock()


def write_key(config, thread_scope: bool = True) -> str | None:
    """La clave estable de esta tool call, o None si no hay grafo detras.

    None significa "no hay reanudacion posible" (una invocacion directa de la
    herramienta), no "no se pudo": la guardia se desactiva y la herramienta
    escribe como siempre.
    """
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None

    task = configurable.get("checkpoint_ns") or configurable.get("__pregel_task_id")
    if not task:
        return None

    thread = configurable.get("thread_id") if thread_scope else None
    return f"{thread}:{task}" if thread else str(task)


def ensure_table(db) -> None:
    """Crea `agent.tool_writes` si falta. Una vez por proceso.

    DDL propio, comiteado aparte y ANTES de que empiece el trabajo del
    negocio: no puede compartir transaccion con la escritura que va a
    proteger. En produccion el schema `agent` ya existe (lo crea
    `build_async_checkpointer` al construir el grafo, con las mismas
    credenciales); el `CREATE SCHEMA IF NOT EXISTS` de aca es para que un
    entorno que arranque distinto -- o un test -- no dependa de ese orden.
    """
    global _ddl_done
    if _ddl_done:
        return
    with _ddl_lock:
        if _ddl_done:
            return
        db.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{AGENT_SCHEMA}"'))
        db.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {QUALIFIED} ("
                "  write_key text PRIMARY KEY,"
                "  tool text NOT NULL,"
                "  entity_id text,"
                "  created_at timestamptz NOT NULL DEFAULT now()"
                ")"
            )
        )
        db.commit()
        _ddl_done = True


def find(db, key: str) -> dict | None:
    """La marca de una escritura ya hecha, o None."""
    row = db.execute(
        text(f"SELECT tool, entity_id FROM {QUALIFIED} WHERE write_key = :k"),
        {"k": key},
    ).first()
    if row is None:
        return None
    return {"tool": row[0], "entity_id": row[1]}


def claim(db, key: str, tool: str) -> None:
    """Anota la marca SIN comitear: la comitea la escritura del negocio.

    `ON CONFLICT DO NOTHING` por si dos intentos corren de verdad a la vez;
    el `find()` de arriba es el que decide, esto solo evita que una carrera
    reviente con una violacion de clave primaria.
    """
    db.execute(
        text(
            f"INSERT INTO {QUALIFIED} (write_key, tool) VALUES (:k, :t) "
            "ON CONFLICT (write_key) DO NOTHING"
        ),
        {"k": key, "t": tool},
    )


def record_entity(db, key: str, entity_id: str) -> None:
    """Guarda que fila quedo escrita, para poder reportarla en un replay.

    Va DESPUES del commit del negocio, en su propia transaccion, y es
    deliberadamente best-effort: si se pierde, la marca sigue estando y la
    repeticion se sigue evitando -- lo unico que se pierde es poder decir
    "ya se registro, es esta". La correccion no depende de esta linea; el
    detalle del mensaje si.
    """
    try:
        db.execute(
            text(f"UPDATE {QUALIFIED} SET entity_id = :e WHERE write_key = :k"),
            {"k": key, "e": entity_id},
        )
        db.commit()
    except Exception:
        db.rollback()
