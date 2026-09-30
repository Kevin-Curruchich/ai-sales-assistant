# El agente conversacional

`app/agent/` es un grafo de LangGraph (`create_react_agent`, `version="v2"`)
con siete herramientas -- tres de lectura, tres de escritura que pausan a
pedir confirmacion humana, y `buscar_cliente`. Se sirve **en el mismo
proceso** que el backend FastAPI de este repositorio: el grafo se construye
una vez en el `lifespan` de `app/main.py` y vive en `app.state.agent_graph`
mientras dure el proceso. No hay un segundo servicio, ni un segundo
`Dockerfile`, ni un protocolo de streaming aparte -- el panel habla con el
grafo por un endpoint REST comun, `POST /api/v1/agent/stream`
(`app/api/v1/endpoints/agent.py`), que devuelve Server-Sent Events.

Este documento cubre como correrlo local, como se despliega, por que el
checkpointer vive en su propio schema de Postgres, el contrato de eventos
que el panel tiene que entender, el token de Firebase en cada request y la
huella al aprobar una escritura, y el checklist antes de desplegar el
enganche de caja. Si estas por tocar las herramientas de escritura, lee
primero los docstrings de modulo de `app/agent/tools/write.py` y
`app/agent/graph.py` -- son mas largos que el codigo que documentan a
proposito, y este archivo asume que los leiste.

## Como esta servido

- **Construccion del grafo**: `app/main.py::lifespan` llama a
  `build_async_checkpointer` y `build_graph` (ambas en `app/agent/graph.py`)
  una sola vez al arrancar, y guarda el resultado en `app.state.agent_graph`.
  Si esa construccion falla -- tipicamente porque Postgres no responde al
  momento de arrancar -- el lifespan no propaga la excepcion: deja
  `app.state.agent_graph = None`, agrega `"agent_graph_init_failed"` a la
  lista `startup_issues` de `app/main.py`, y sigue sirviendo el resto de la
  API (clientes, ventas, reportes). `GET /health` refleja eso devolviendo
  `{"status": "degraded", "issues": [...]}` en vez de `{"status": "ok"}`. Un
  operador que ve un `503` en `/stream` (mas abajo) tiene que mirar
  `/health`: si `agent_graph_init_failed` esta en `issues`, el problema es
  que el grafo nunca se construyo, no un fallo puntual de esa corrida.
- **Checkpointer**: `AsyncPostgresSaver` de `langgraph-checkpoint-postgres`
  sobre un `AsyncConnectionPool` (`min_size=1, max_size=3`), apuntado al
  schema `agent` de la misma base Postgres que usa el negocio (ver mas
  abajo). Se abre una vez en el lifespan y se cierra ahi tambien, al salir
  del `yield`.
- **Endpoint**: `POST /api/v1/agent/stream`, en
  `app/api/v1/endpoints/agent.py` -- el mismo archivo que tiene los
  endpoints de hilos (`GET/POST /agent/threads`, etc.). Autenticado con la
  misma dependencia `get_current_user` (`app/api/dependencies.py`) que usa
  el resto del panel: valida el token de Firebase del header `Authorization`
  y devuelve el `User` local.
- **Traduccion a SSE**: `app/agent/streaming.py::eventos_sse` es la unica
  capa que traduce lo que `graph.astream(...)` emite a los cinco eventos que
  el panel entiende (`token`/`herramienta`/`confirmacion`/`fin`/`error`, ver
  mas abajo).

## Correrlo local

### Variables de entorno

Ademas de las que ya pide el backend (ver `docs/desarrollo-local.md`), el
agente necesita estas, porque las lee **la API misma** -- no hay ya un
servicio aparte que las consuma:

| Variable | Para que |
|---|---|
| `ANTHROPIC_API_KEY` | El modelo (`ChatAnthropic`, `claude-sonnet-5`, `app/agent/graph.py::default_model`). Sin esto, la construccion del grafo falla en el lifespan y la API arranca en `degraded` (ver "Como esta servido"). |
| `AGENT_HUELLA_SECRET` | Firma la huella de aprobacion (ver mas abajo). Sin esto, el agente se niega a emitir o verificar una aprobacion. |
| `LANGSMITH_TRACING` | `true`/`false`. Activa el trazado de cada corrida en LangSmith. Opcional para desarrollo, util para depurar una conversacion completa (incluidas las pausas de `interrupt()`). |
| `LANGSMITH_API_KEY` | Credencial de LangSmith. Solo hace falta si `LANGSMITH_TRACING=true`. |
| `LANGSMITH_PROJECT` | Nombre del proyecto en LangSmith donde aparecen esas trazas. |

Ninguna de las cinco pasa por `app/core/config.py` (`Settings`, pydantic) --
`ChatAnthropic`, la firma de la huella y el SDK de LangSmith las leen directo
de `os.environ`. Van en `.env` como cualquier otra variable local;
`.env.example` las trae comentadas.

### Levantarlo

No hay un venv ni un proceso aparte para el agente. Se levanta con el resto
de la API, en `venv/`:

```bash
docker compose -f docker-compose.dev.yml up -d
venv/bin/alembic upgrade head        # si todavia no corriste esto
venv/bin/hypercorn app.main:app --reload
```

`GET /health` confirma que el grafo se construyo (`{"status": "ok"}`, sin
`agent_graph_init_failed` en `issues`). `POST /api/v1/agent/stream` exige el
mismo token de Firebase que el resto del panel -- sin `Authorization` valido,
401, antes de que el streaming arranque.

## Desplegarlo

Un solo servicio de Railway, el que ya existe -- no hay un segundo servicio
ni un segundo `Dockerfile` que crear. `entrypoint.sh` corre
`alembic upgrade head` y despues `exec hypercorn app.main:app --bind
"0.0.0.0:${PORT:-8000}"`. Lo que agrega el agente al checklist de ese
servicio:

- **`AGENT_HUELLA_SECRET` y `ANTHROPIC_API_KEY` en el servicio que ya
  existe.** No hay un segundo lugar donde cargarlas. Las `LANGSMITH_*` son
  opcionales, igual que en local.
- **No agregues `--workers` al comando de `entrypoint.sh` sin resolver esto
  primero.** El endpoint `/stream` rechaza una segunda corrida sobre el
  mismo `thread_id` con `409` mientras la primera sigue viva
  (`_hilos_en_curso`, un `set` en memoria en
  `app/api/v1/endpoints/agent.py`). Ese `set` es correcto **solo con un
  proceso**: `entrypoint.sh` termina sin `--workers`, y el default de
  hypercorn es un unico worker. Con dos o mas workers, cada uno ve su propio
  `set` -- memoria no compartida -- y dos corridas sobre el mismo hilo
  podrian pisarse los checkpoints entre si; el estado resultante no seria el
  de ninguna de las dos, y el `409` que hoy evita eso dejaria de dispararse
  sin que nada lo avise. Si algun dia hace falta subir workers, este
  mecanismo tiene que mudarse a la base (una fila con un lock) antes, no
  despues.
- **El schema `agent` se crea solo**, en el primer arranque del grafo (ver
  abajo) -- no hace falta ningun paso manual para eso.
- El resto de las variables de `.env.production`
  (`POSTGRES_*`/`DATABASE_URL`, credenciales de Firebase) ya las necesitaba
  la API para todo lo demas; el agente las reusa tal cual -- valida el mismo
  token de Firebase, lee y escribe con los mismos modelos y el mismo
  `POSTGRES_SCHEMA`.

## El schema `agent`

Las tablas del checkpointer de LangGraph (`checkpoints`, `checkpoint_writes`,
`checkpoint_blobs`, etc. -- el estado que le permite a una conversacion
sobrevivir una pausa de `interrupt()` y una reanudacion dias despues) viven
en un schema de Postgres separado, `agent`, **nunca** en el schema del
negocio (`POSTGRES_SCHEMA`, ej. `db_local`/`db_dev`).

La razon es Alembic, no gusto: `alembic/env.py` genera migraciones
comparando el schema del negocio contra los modelos declarados en
`app/models`. Las tablas del checkpointer no tienen modelo declarado ahi --
si vivieran en ese mismo schema, cada `alembic revision --autogenerate` las
leeria como deriva no declarada y emitiria `drop_table` para cada una, en
cada migracion, para siempre. El schema `agent` esta fuera del radar de
Alembic a proposito. La misma tabla `agent.tool_writes` que usa
`app/agent/idempotency.py` (ver mas abajo) vive ahi por la misma razon.

**`AsyncPostgresSaver.setup()` NO crea ese schema.** Las migraciones internas
del saver son DDL sin calificar (`CREATE TABLE checkpoints`, no
`CREATE TABLE agent.checkpoints`) que resuelve donde aterriza por el
`search_path` de la conexion -- ningun `CREATE SCHEMA` en ninguna parte de
esas migraciones. Contra una base fresca, `SET search_path TO "agent"` con
el schema `agent` inexistente no falla ahi (Postgres permite un
`search_path` que nombra un schema que todavia no existe) -- falla recien en
el primer `CREATE TABLE` de `setup()`, con
`InvalidSchemaName: no schema has been selected to create in`.

`build_async_checkpointer` (`app/agent/graph.py`) lo resuelve explicito,
antes de fijar el `search_path` y antes de llamar a `setup()`:

```python
await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
```

El mismo patron que `alembic/env.py` usa para el schema del negocio. No hace
falta ningun paso manual antes de desplegar por esto -- se crea solo, la
primera vez que se construye el grafo en el proceso (local, produccion, CI).

## El contrato del panel: el token de Firebase

El panel tiene que mandar el token de Firebase del usuario en el header
`Authorization` de **cada** request a `/api/v1/agent/stream`, igual que en
cualquier otro endpoint de la API:

```
Authorization: Bearer <id token de Firebase>
```

No hay una credencial nueva ni un segundo login. `stream_agent`
(`app/api/v1/endpoints/agent.py`) depende de `get_current_user`
(`app/api/dependencies.py`) -- la misma dependencia que usa el resto del
panel -- para resolver el `User` local a partir de ese token, y arma
`config["configurable"]["user_id"]` **exclusivamente** con `current_user.id`.
El cuerpo del request (`AgentStreamRequest`) usa `extra="ignore"`: un
`user_id` o un `configurable` sueltos que un cliente intente colar en el
body se descartan en silencio, nunca llegan al `config` que arma el
servidor. Las tres herramientas de escritura firman la huella de aprobacion
con ese `user_id` -- es la unica linea de defensa entre un cliente y una
venta firmada a nombre de otro usuario, ver el docstring de `stream_agent`.

Un token ausente, vacio, vencido o invalido es un **401**, resuelto por
`get_current_user` antes de que la conversacion llegue a existir -- antes
incluso de que el endpoint compruebe que el hilo es del usuario, y muy
antes de que arranque el streaming.

**Orden de las validaciones dentro del endpoint:** `get_owned` (el hilo
existe y es del usuario) corre ANTES de tocar el grafo o de devolver el
`StreamingResponse`. Con SSE el `200 OK` sale con el primer byte del stream,
y despues de eso el codigo de estado HTTP ya no se puede cambiar -- por eso
un hilo ajeno o inexistente tiene que resolverse como **404** normal, antes
de que arranque el stream, y no puede convertirse en un evento del stream
mas adelante.

**No hay autorizacion mas alla de "el hilo es tuyo".** Una vez autenticado,
un usuario puede leer y reanudar cualquier hilo **propio** -- `get_owned`
filtra por `user_id` -- pero no hay roles distintos dentro del agente: para
un negocio de un solo dueño con un puñado de usuarios de confianza es
aceptable.

## El contrato del panel: los eventos SSE

`app/agent/streaming.py::eventos_sse` traduce `graph.astream(entrada, config,
stream_mode=["messages", "updates"])` a cinco tipos de evento, cada uno
`{"event": str, "data": dict}`, mandados por el endpoint como una linea SSE
(`event: <tipo>\ndata: <json>\n\n`).

### `token`

Un fragmento de texto que el modelo va generando. Ejemplo:

```
event: token
data: {"texto": "Para confirmar, "}
```

### `herramienta`

El modelo pidio una llamada a herramienta. Trae solo el nombre y un estado
fijo -- no los argumentos: los de una escritura ya viajan en la
`confirmacion` que sigue, mandarlos tambien aca seria un dato de mas (y para
`registrar_venta`, una lista de items entera) que nadie pidio. Ejemplo:

```
event: herramienta
data: {"nombre": "registrar_venta", "estado": "llamando"}
```

### `confirmacion`

Una herramienta de escritura se detuvo en `interrupt()` y necesita
aprobacion humana. `data` es exactamente el payload que la herramienta le
paso a `interrupt()` -- incluida la `huella` (ver la seccion siguiente), sin
que esta capa la toque ni la firme de nuevo. Ejemplo:

```
event: confirmacion
data: {"tipo": "confirmar_venta", "huella": {"datos": {...}, "firma": "..."}, "preview": {...}}
```

**Dos herramientas hermanas que interrumpen en el mismo turno producen DOS
eventos `confirmacion`** -- una por cada `interrupt()`, en el orden en que
vienen en la lista `__interrupt__` del chunk de `"updates"` -- **y un solo
`fin`** con `estado: "pausado"` al final, no uno por confirmacion. Esto es
en parte un hecho probado y en parte uno derivado, y vale distinguirlos:
`tests/test_agent_graph.py::test_a_completed_write_tool_does_not_replay_when_a_sibling_write_tool_is_still_interrupted`
prueba, contra las dos herramientas de escritura reales en el mismo mensaje
del modelo, que ese `__interrupt__` trae efectivamente dos entradas
(`len(interrupts) == 2`) -- pero lo hace invocando `graph.invoke` directo,
no a traves de `eventos_sse`. Que esa lista de dos se traduzca en dos
eventos `confirmacion` y un solo `fin` es una lectura del codigo de
`eventos_sse` (el `for interrupcion in interrupciones: yield ...` esta dentro
del bucle principal, y el `yield fin` esta una sola vez, al final de toda la
funcion, fuera de ese bucle) -- **no hay un test que corra dos herramientas
hermanas a traves de `eventos_sse` y cuente los eventos que salen.**

**Al reanudar, las llamadas a herramienta que quedaron pendientes NO se
vuelven a anunciar.** El evento `herramienta` sale del nodo del modelo (el
`AIMessage` con `tool_calls` que decide que llamar); reanudar un
`interrupt()` con `Command(resume=...)` continua la tarea de Pregel que
quedo pausada dentro del nodo de herramientas, no vuelve a invocar al
modelo -- asi que no hay un `AIMessage` nuevo del que salga un `herramienta`
repetido para esas llamadas. Un panel que cuenta "cuantas herramientas se
anunciaron" no debe esperar volver a ver las que ya estaban pendientes antes
de la pausa. **Este parrafo tambien es una lectura del codigo, no un hecho
con test dedicado**: los tests de herramientas hermanas
(`test_agent_graph.py`, arriba) reanudan con `graph.invoke` crudo, nunca a
traves de `eventos_sse`, asi que ningun test de la suite verifica hoy que
el evento `herramienta` no se repita al reanudar. Si el panel llega a
depender de este comportamiento, este es el punto donde falta un test antes
de confiar en el.

### `fin`

Cierra el stream. `estado` es `"completo"` (la corrida termino sin
pausarse) o `"pausado"` (hubo uno o mas `interrupt()`). Ejemplo:

```
event: fin
data: {"estado": "completo"}
```

### `error`

**Un error a mitad del turno es un evento `error`, nunca un codigo HTTP.**
Con SSE el `200 OK` ya salio con el primer byte de la respuesta -- para
cuando algo revienta adentro del `astream` (una herramienta que levanta una
excepcion, un problema de conexion a Postgres a mitad de una escritura), no
hay forma de cambiar el codigo de estado. `eventos_sse` atrapa la excepcion,
loguea el traceback completo del lado del servidor (`logger.exception`, para
diagnostico) y manda un evento `error` con un mensaje fijo en español, sin
nombre de excepcion ni detalle de implementacion -- quien vende no necesita
saber que fue un `ValueError`, y un `str(exc)` crudo de psycopg/SQLAlchemy
arrastraria SQL y datos de conexion hasta el panel. Ejemplo:

```
event: error
data: {"mensaje": "Hubo un problema y no se registro nada. Intenta de nuevo."}
```

`error` es siempre el ultimo evento del stream cuando aparece -- no hay
`fin` despues.

### Contrato del generador, en una linea

Siempre termina, y termina en un evento terminal: `fin` (completo o
pausado) o `error`. Nunca los dos. Un stream que no cierra deja al panel
esperando para siempre; uno que sigue vivo despues de un terminal manda
eventos que ya nadie deberia leer.

### El agente no disponible: `503`

Si el grafo no se pudo construir al arrancar (ver "Como esta servido" --
tipicamente Postgres caido en ese momento), `request.app.state.agent_graph`
es `None` y `/stream` responde **503** con un mensaje fijo, antes de tocar
`eventos_sse`, antes de devolver el `StreamingResponse` -- este es un codigo
HTTP normal porque pasa antes del primer byte del stream, a diferencia del
caso de `error` de arriba. `GET /health` en ese momento reporta
`agent_graph_init_failed` en `startup_issues`: un operador que ve el 503
tiene que mirar ahi para confirmar que la causa es que el grafo nunca
arranco, no un fallo puntual de esa corrida.

### El hilo ocupado: `409`

Un segundo pedido sobre un `thread_id` que ya tiene una corrida en curso
responde **409**, tambien antes del primer byte -- ver la seccion
"Desplegarlo" arriba para la restriccion de un solo proceso de la que
depende esto.

### El cuerpo sin `mensaje` ni `decision`: `422`

El cuerpo de `/stream` (`AgentStreamRequest`) trae `thread_id` y, opcionales,
`mensaje` (arranca o continua la conversacion con texto del usuario) y
`decision` (resuelve un `interrupt()` pendiente -- ver "El contrato del
panel: la huella"). Un pedido que no manda ninguno de los dos no tiene nada
que decirle al grafo: responde **422**, tambien antes del primer byte, con
`detail: "Mandá 'mensaje' o 'decision'."`. Es el unico de los cuatro codigos
de esta seccion que un panel en desarrollo va a pisar seguido, mientras
todavia arma la forma exacta del cuerpo del pedido -- por ejemplo, un envio
que solo manda `thread_id` porque el campo con el mensaje del usuario
todavia no se cableo del lado del panel. Cubierto por
`tests/test_agent_stream_endpoint.py::test_streaming_without_mensaje_or_decision_is_a_422`.

### Un cliente que se desconecta cancela la corrida de verdad

El endpoint itera `eventos_sse` directo (`async for evento in
eventos_sse(...): yield ...`), sin una tarea propia ni sondeo de
`is_disconnected()`. Cuando Starlette detecta que el cliente se fue,
cancela la tarea que esta sirviendo el pedido, y esa cancelacion llega
directo a donde el generador esta suspendido -- adentro de `eventos_sse`,
que deja pasar `asyncio.CancelledError` sin convertirla en un evento
`error` (es `except Exception`, no `except BaseException`, a proposito). El
turno muere ahi: no sigue gastando modelo contra un cliente que ya no esta
escuchando. Probado con una cancelacion real de la tarea ASGI en
`tests/test_agent_stream_endpoint.py::test_a_disconnected_client_cancels_the_run`
y, contra la funcion real (no un doble), en
`tests/test_agent_streaming.py::test_a_cancelled_run_dies_instead_of_becoming_an_error_event`.

## El contrato del panel: la huella

> **La huella va firmada.** El sobre que el panel recibe es
> `{"datos": ..., "firma": "<hmac-sha256>"}`. **Para el panel no cambia
> nada**: lo sigue guardando opaco y lo sigue devolviendo tal cual. Lo que
> cambia es que el servidor puede verificar que esa huella la emitio el.
>
> Antes solo podia comparar el valor recibido contra el estado actual de la
> base: un panel que RECALCULARA la huella en vez de guardarla apagaba la
> comparacion **en silencio** y era indetectable. Ahora esa falla devuelve
> `"estado": "huella_no_valida"` la primera vez.
>
> Requiere `AGENT_HUELLA_SECRET` en el entorno. Si falta, el agente **se
> niega** a emitir o verificar una aprobacion en vez de degradar a sin
> firma. Rotar el secreto invalida las aprobaciones pendientes en ese
> momento: hay que volver a aprobarlas.
>
> Los estados de una aprobacion: `registrado` (se escribio), `cancelado`
> (no se escribio nada, a pedido), `ya_registrado` (esta tarea ya habia
> escrito antes -- ver "Idempotencia"), `recalculado` (el inventario
> cambio entre el preview y la aprobacion), `aprobacion_sin_huella` (la
> aprobacion no trajo huella utilizable), `huella_no_valida` (vino una
> huella que este servidor no emitio).

Esta es la seccion mas importante de este documento. Un panel que la
ignora no falla ruidosamente -- deja pasar aprobaciones sin ninguna
garantia, en silencio, y nada del lado del servidor puede detectarlo.

### Por que existe

Las tres herramientas de escritura (`registrar_venta`, `registrar_compra`,
`registrar_movimiento_caja`) se detienen con `interrupt()` antes de tocar la
base, y le muestran al humano una vista previa (costo, lotes FIFO
consumidos, margen, total) para que apruebe.

`interrupt()` **no congela un frame de Python vivo**. Al reanudar con
`Command(resume=...)`, LangGraph vuelve a correr la funcion de la
herramienta ENTERA desde el principio -- el valor que llega en `resume` se
empareja por indice de llamada a `interrupt()`, no reanuda la ejecucion en
el punto exacto donde se pauso. Concretamente: si la herramienta calcula un
preview, lo manda a `interrupt()`, y despues -- ya reanudada -- vuelve a
calcular ese mismo preview para comparar, los DOS calculos ocurren DESPUES
de la pausa, microsegundos aparte, sobre el mismo estado de la base. Nunca
sobre lo que la persona efectivamente vio antes de aprobar. Comparar un
recalculo local contra otro recalculo local no prueba nada -- la
comparacion es estructuralmente siempre verdadera.

Lo unico que cruza la pausa intacto es el valor que trae el `resume`. Por
eso cada escritura arma una **huella** -- un digesto de las cifras
aprobadas (costo unitario, subtotal, que lotes FIFO se consumieron y
cuanto, advertencias de stock insuficiente para una venta; subtotal por
item, si el producto sigue activo, total para una compra) -- y la mete
DENTRO del payload de `interrupt()`. Esa huella viaja al panel, se congela
en el checkpoint junto con el resto del estado pausado, y es la unica
evidencia de lo que la persona realmente vio antes de decir que si.

### Lo que el panel tiene que hacer

Al aprobar, el panel manda:

```json
{"accion": "aprobar", "huella": <exactamente el valor de "huella" que vino en el payload de interrupt()>}
```

**Guardar la huella de forma opaca y devolverla byte por byte.** El panel no
la interpreta, no la reformatea, no la recalcula a partir del estado actual
-- la recibe, la guarda tal cual (en el estado de React, en localStorage, da
igual), y la reenvia exactamente igual al aprobar.

**Un panel que la recalcula en vez de devolverla derrota la guardia en
silencio.** Si el panel, en vez de guardar la huella que recibio, vuelve a
pedir una vista previa fresca al momento de aprobar y manda ESA como
`huella`, la comparacion del lado del servidor va a "coincidir" siempre --
contra lo que el mismo panel acaba de calcular, no contra lo que se le
mostro a la persona. Ningun cambio real de inventario entre el preview y la
aprobacion se va a detectar nunca. El servidor no tiene forma de distinguir
esto de un echo honesto cuando nada cambio en el medio -- la huella
recalculada y la huella echoada son indistinguibles cuando coinciden por
casualidad. Es un bug que no se manifiesta en pruebas casuales y que solo
importa el dia que si importa.

### Las otras formas de resume

Ademas de `aprobar`, el payload de `resume` puede traer:

```json
{"accion": "cancelar"}
```

No se escribe nada. La herramienta devuelve `{"estado": "cancelado"}`.

```json
{"accion": "corregir", "valores": {"cantidad": "3", "precio_unitario": "45.00"}}
```

`valores` trae solo los campos que cambian -- se mezclan sobre los
originales. La herramienta rearma la vista previa con los valores
corregidos y vuelve a pausar con una `huella` nueva (correspondiente a los
valores corregidos) -- no reanuda la conversacion ni devuelve el control al
modelo. Para una venta o una compra esto importa: cambiar la cantidad
cambia de que lotes sale y a que costo, asi que hay que volver a calcular
todo, no solo el campo que cambio.

### `aprobacion_sin_huella`

Si la aprobacion llega sin una huella utilizable -- ausente, `None`, o
vacia (`[]`/`{}`) -- la herramienta devuelve un estado distinto de
`recalculado`:

```json
{"estado": "aprobacion_sin_huella", "mensaje": "..."}
```

Esto existe para no confundir a quien depure un panel viejo o mal armado.
Un panel que nunca manda huella (por ejemplo uno construido contra un
contrato anterior, de antes de que la huella existiera) recibiria
`"recalculado"` -- "el inventario cambio" -- en CADA aprobacion, para
siempre, sin ninguna pista de que el problema es el contrato del panel y no
el inventario del negocio. `aprobacion_sin_huella` separa ese caso: dice,
sin ambiguedad, que el panel no devolvio lo que se le mostro, no que algo
haya cambiado en la base.

Nota: `registrar_movimiento_caja` no calcula huella -- un movimiento de caja
no deriva de inventario ni de lotes, es un hecho que la persona afirma
("el socio puso Q500"), no un calculo que pueda desactualizarse entre el
preview y la aprobacion. Sus resumes son solo `aprobar` (sin `huella`),
`cancelar` y `corregir`.

## Idempotencia: una escritura por tarea, y lo que queda afuera

Las tres herramientas de escritura comitean a Postgres **fuera** de la
transaccion de LangGraph. Entre ese commit y el momento en que LangGraph
anota en el checkpoint que la tarea termino hay una ventana: si el proceso
muere ahi, el checkpoint no sabe que la escritura ocurrio, y reanudar el
hilo vuelve a correr el cuerpo entero de la herramienta -- incluida la
escritura ya comiteada.

Eso lo cierra `app/agent/idempotency.py`. Cada tool call se identifica con
el id de su tarea de Pregel (via `checkpoint_ns`), que es **estable a traves
de la reanudacion** y **distinto por tool_call hermana** del mismo mensaje
del modelo -- las dos propiedades verificadas contra un grafo y un
checkpointer reales, no supuestas. La marca se anota en `agent.tool_writes`,
en el mismo schema `agent` que ya aloja las tablas del checkpointer (y por
la misma razon: una tabla sin modelo en `app/models` dentro del schema del
negocio la leeria el autogenerate de Alembic como deriva y emitiria un
`drop_table` en cada migracion). La fila se inserta en la **misma
transaccion** que la escritura del negocio: o quedan las dos o no queda
ninguna, asi que un intento que reviente no deja la tarea marcada como "ya
escribio".

Cuando la marca ya esta, la herramienta contesta
`{"estado": "ya_registrado", "<x>_id": ..., "mensaje": ...}` **antes** de
volver a pausar -- no tiene sentido pedirle a una persona que apruebe algo
que ya esta en la base. El panel deberia tratar ese estado como un exito, no
como un error: nada se escribio de mas y no hay nada que corregir.

### Lo que NO cierra, con precision

- **`registrar_compra` escribe en dos transacciones.** `create()` comitea el
  borrador y `confirm()` comitea el stock y la salida de caja aparte. La
  marca viaja con la **segunda**, no con la primera: `confirm()` puede
  reventar por su cuenta (un 409 si el producto se desactivo, un 404 si
  desaparecio, un error de base) y con la marca en la primera transaccion
  esa falla dejaba la tarea marcada mientras el manejador borraba el
  borrador -- nada escrito y un `ya_registrado` mintiendo. Lo que queda: un
  proceso que muera **entre las dos** deja un borrador **huerfano**, sin
  confirmar. La reanudacion no lo reusa: crea uno nuevo y lo confirma, asi
  que la compra sí queda registrada, una sola vez, con su stock y su salida
  de caja. Lo unico de mas es ese borrador, que hay que borrar a mano desde
  el panel. Cerrarlo del todo pide que `create()` y `confirm()` compartan
  transaccion, que es un cambio en `PurchaseService`, no en el agente.
- **La tabla se crea sola, la primera vez que una herramienta escribe.** No
  hay migracion de Alembic (a proposito: el schema `agent` esta fuera de su
  radar), asi que el usuario de la base necesita permiso de `CREATE` --
  el mismo que ya necesita para que `build_async_checkpointer` cree el
  schema `agent` y las tablas del checkpointer al construir el grafo.
- **Una invocacion directa de la herramienta, fuera de un grafo, no se
  desduplica.** No hay id de tarea, no hay reanudacion posible, y dos
  llamadas son dos hechos distintos. La guardia se desactiva sola en vez de
  inventarse una equivalencia.
- **`entity_id` (que fila quedo escrita) se anota despues del commit, en su
  propia transaccion, y es best-effort.** Si se pierde, la repeticion se
  sigue evitando; lo unico que se pierde es poder decir *cual* fila fue.

## Checklist de despliegue: el enganche de caja

Toda venta pagada **cuyo total sea mayor que cero** -- registrada por el
agente o por el panel, es el mismo codigo -- escribe tambien un movimiento de
caja (`entrada`, `CashMovementType.ENTRADA`) ademas de la venta misma. Una
venta pagada de Q0.00 (un regalo, una muestra: los schemas aceptan
`unitPrice = 0` a proposito) se registra igual pero **no** escribe
movimiento: el libro registra dinero que realmente se movio, y cero
quetzales no se movieron. Misma regla en `PurchaseService.confirm()` para una
compra de total cero. Esto vive en
`SaleService.create()`, en `app/services/sales/orchestrator.py`: la venta y
el movimiento de caja se escriben en la misma transaccion (el movimiento se
crea con `commit=False`, y `create()` comitea las dos escrituras juntas al
final -- si una falla, ninguna queda). Esto cambia el comportamiento de un
endpoint que ya esta vivo: **desde el momento en que esto se despliega,
cada venta pagada registrada por el panel suma a la caja.**

No cito el commit que introdujo esto a proposito: es trabajo de rama
anterior a este plan (no una de sus diez tasks numeradas), y citar un hash
resulto fragil en un borrador anterior de este documento -- un
`git rebase`/reescritura de la rama le cambia la identidad a todo el rango.
Lo que importa para quien lea esto es el comportamiento y donde vive el
codigo, no que commit lo escribio; `git log -- app/services/sales/orchestrator.py`
lo encuentra si hace falta el historial exacto.

`CashService.running_balance()` (`app/services/cash_service.py`) no
almacena un saldo -- lo calcula sumando todos los movimientos de caja hasta
la fecha pedida. Si nunca se cargo un movimiento `saldo_inicial` con el
efectivo real que habia en caja al momento de activar esto, el "saldo" que
devuelve `running_balance()` no es un saldo: es la suma de lo que entro
DESDE que se prendio el enganche, un numero que se ve como un saldo de caja
y no lo es.

**Antes de desplegar esto:**

1. Contar el efectivo real que hay en caja al momento del corte.
2. Cargar un movimiento `tipo="saldo_inicial"` por ese monto (via
   `registrar_movimiento_caja` una vez que el agente este arriba, o
   directamente con `CashService.record(...)` desde una consola contra
   produccion si el agente todavia no esta desplegado).
3. Verificar que `CashService.running_balance()` devuelve exactamente ese
   numero -- sin ninguna venta ni compra todavia registrada encima.
4. Recien entonces desplegar el cambio (el grafo del agente, o el cambio
   del orquestador de ventas, si no se habia desplegado antes). A partir de
   ahi, cada venta pagada y cada compra confirmada se suman/restan sobre
   ese punto de partida real.

Hacerlo en otro orden -- desplegar primero, cargar `saldo_inicial` despues
-- deja una ventana donde `running_balance()` ya esta sumando ventas reales
sobre un punto de partida de cero, y el `saldo_inicial` que se cargue
despues no puede corregir retroactivamente los movimientos que ya se
sumaron en el medio con la base equivocada (quedarian todos corridos por el
efectivo real que faltaba contar). El orden de los tres pasos de arriba no
es una formalidad.

## `Revenew/`: resuelto, con un riesgo residual que vale nombrar

`Revenew/` (`AGENTS.md` y nueve skills) es el prompt del runtime anterior de
este proyecto -- uno que corria en Slack, escribia en Google Sheets y creaba
eventos en Google Calendar. Task 9 decidio, a proposito, no cargarlo en
tiempo de ejecucion: un contenedor desplegado no lo va a tener, y de todas
formas describe herramientas (Sheets, Calendar, Slack) que este agente no
tiene. En su lugar, `app/agent/prompt.py::SYSTEM_PROMPT` es una traduccion
escrita a mano de esas mismas reglas de negocio (FIFO, margenes, vocabulario
de caja, deteccion de precio habitual) a las siete herramientas reales de
este grafo -- ver el docstring de modulo de `prompt.py`.

`AGENTS.md` y las nueve skills estan versionados a proposito, como
referencia historica del dominio -- no como fuente de verdad activa.
`Revenew/README.md` lo dice explicito: el runtime que esos archivos
describen ya no existe, la fuente viva es `SYSTEM_PROMPT`, y nombra las
diferencias conocidas entre los dos (el modelo de un paso de
`monto_aporte_propio` en la skill de compra contra las dos llamadas reales
del codigo; el quinto tipo de movimiento de caja, `saldo_inicial`, que el
codigo tiene y la skill no). `config.json` y `tools.json` quedaron fuera del
repo (gitignorados) porque no describen el dominio -- solo identificadores
de la plataforma anterior (tenant/organizacion de LangSmith, proveedor
OAuth de Slack) -- y no hay que sacarlos de ahi.

Lo que ese README no resuelve, porque ningun README puede: nada fuerza que
`Revenew/AGENTS.md` y `SYSTEM_PROMPT` se mantengan sincronizados. Si mañana
cambia el margen objetivo de un producto, o la regla de cuando avisar de un
lote cruzado, no hay ningun mecanismo automatico que actualice los dos --
depende de que quien cambie la regla de negocio se acuerde de que existe un
documento historico y lo revise. Eso es aceptable para un documento marcado
como historico (es el punto de marcarlo asi: nadie deberia leerlo esperando
que este al dia), pero vale que quien mantenga `SYSTEM_PROMPT` sepa que el
README no es una garantia de sincronia, solo una advertencia de que no la
hay.
