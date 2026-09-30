# El agente servido desde FastAPI

**Fecha:** 2026-09-29
**Estado:** diseño aprobado, pendiente de plan de implementación
**Reemplaza la topología de:** `docs/superpowers/specs/2026-09-24-agente-runtime-conversacional-design.md` (pieza 2A)
**Operación actual:** `docs/agente.md`

## Problema

La pieza 2A construyó el agente y lo dejó funcionando: las siete herramientas,
la confirmación humana con huella firmada, la idempotencia, el prompt. Lo que
no quedó resuelto es **cómo se sirve**.

El spec anterior eligió dos procesos —un servidor de LangGraph aparte— para que
`useStream` del SDK de JS funcionara sin adaptadores. Al intentar desplegarlo
aparecieron dos hechos que esa decisión no conocía.

## Lo que se descubrió

**El servidor de LangGraph elige su runtime con `LANGGRAPH_RUNTIME_EDITION`, y
sólo una de las tres ediciones está en PyPI público.**

| Edición | PyPI público |
|---|---|
| `inmem` | sí (0.35.1) |
| `postgres` | 404 |
| `community` | 404 |

La única instalable guarda su estado en **archivos locales**, con un bucle de
flush cada diez segundos que sus propios autores registran como
`"Starting dev persistence flush loop"`. En Railway eso significa una sola
instancia y **toda aprobación pendiente muerta en cada despliegue** — inaceptable
para un agente cuyo mecanismo central es pausar esperando confirmación.

Y hay un segundo efecto: bajo el servidor completo, `graph.py:416-422` hace
`graph_obj.copy(update={"checkpointer": ...})`. El `PostgresSaver` que la pieza
2A construyó y probó **se descarta**; persiste el del servidor.

**Los precios** (verificado en langchain.com/pricing el 2026-09-29): Developer
$0 sin despliegue; Plus $39/asiento/mes con un despliegue *serverless* — o sea
el agente corriendo en infraestructura de LangChain, con el Postgres de Railway
expuesto a su nube; self-hosted figura como característica de **Enterprise**.
La página no desglosa las modalidades de LangGraph Platform por nombre, así que
sobre una modalidad específica hay que preguntarles.

**Conclusión:** tener el agente en infraestructura propia, con la base propia y
sin cuota recurrente, exige no usar el servidor de LangGraph.

## Lo que no cambia

El grafo **ya funciona en proceso**. Los tests lo ejecutan con `invoke()` y lo
reanudan con `Command(resume=...)`, y el test de idempotencia lo probó con un
`PostgresSaver` real **entre dos procesos distintos**. Toda la pausa y la
reanudación son del grafo, no del servidor.

Sobreviven intactas: las siete herramientas, la huella firmada, la idempotencia,
el `interrupt()` con su replay, el prompt, el schema `agent`. Lo que se
reemplaza es sólo la capa HTTP.

## Decisiones

| Decisión | Elegido | Descartado y por qué |
|---|---|---|
| Quién sirve el grafo | La propia app FastAPI | El servidor de LangGraph cuesta una edición de desarrollo en producción, o Enterprise |
| Forma de respuesta | Streaming token por token (SSE) | Sin streaming, registrar una venta son varios segundos de pantalla quieta |
| Endpoints de streaming | Uno, con dos formas de entrada | Dos duplican la maquinaria de SSE, que es la parte con aristas |
| Hilos | Objeto propio, con dueño | Un hilo derivado del usuario no permite varias conversaciones |
| Compartir | **Fuera de alcance** | Arrastra preguntas (qué ve un lector, cómo se revoca, qué pasa al borrar) que hoy no bloquean |
| Autorización | Sólo el dueño | Hoy cualquier autenticado reanuda cualquier hilo; con dos usuarios deja de ser aceptable |

## Arquitectura

Un solo proceso. El grafo corre dentro de la app que ya desplegás.

```
panel ──POST /api/v1/agent/stream──► FastAPI
                                       │ get_current_user  →  user_id
                                       │ verificar dueño del thread_id
                                       │ config = {thread_id, user_id}
                                       ▼
                                   graph.astream(...)
                                       │
                          ┌────────────┴────────────┐
                          ▼                         ▼
                    modelo (async)          herramientas (sync)
                                            en hilos de trabajo
                                       │
                                       ▼
                          SSE ◄── token · herramienta · confirmacion · fin
```

### El límite async/sync es seguro, y está medido

El modelo se llama de forma asíncrona. Las siete herramientas son sincrónicas y
hacen I/O bloqueante de base, pero `BaseTool.ainvoke` las despacha por
`run_in_executor`. Medido: durante una herramienta que bloquea un segundo, el
event loop siguió latiendo nueve veces, y la herramienta corrió en `asyncio_0`,
no en `MainThread`. **La API no se congela mientras el agente trabaja.**

Cada herramienta abre su propia sesión con `agent_session()`, que es lo correcto
para hilos: las sesiones de SQLAlchemy no son thread-safe y cada una recibe la
suya.

### El checkpointer pasa a `AsyncPostgresSaver`

Verificado: `PostgresSaver` **no implementa** los métodos asíncronos — los hereda
de `BaseCheckpointSaver`, que levanta `NotImplementedError`. Con `astream` hay
que usar el asíncrono. Usa `psycopg` v3, ya instalado y pineado.

Sigue sobre el schema `agent`, que se crea explícitamente (`PostgresSaver` no lo
crea; eso ya está resuelto y documentado). Y ahora **se usa de verdad**, en vez
de ser reemplazado por el del servidor.

### La identidad vuelve a ser la normal

`get_current_user` como en cualquier endpoint. El `user_id` se inyecta en
`config["configurable"]` **del lado del servidor**, donde el cliente no lo toca.

## El contrato del panel

### `POST /api/v1/agent/stream`

El hilo se crea **antes** de la primera conversación, con `POST /api/v1/agent/threads`,
que devuelve su id. `/stream` nunca crea un hilo implícitamente: un `thread_id`
que no existe, o que no es del usuario autenticado, responde 404 — el mismo
código en los dos casos, para no revelar qué hilos existen.

Dos formas de entrada, ambas con `thread_id`:

```json
{"thread_id": "...", "mensaje": "vendí dos cartones a Aurita"}
{"thread_id": "...", "decision": {"accion": "aprobar", "huella": <el sobre recibido>}}
```

Por debajo es la misma llamada a `astream`, con `{"messages": [...]}` o
`Command(resume=...)`.

### Los eventos

```
event: token
data: {"texto": "Vendiste medio"}

event: herramienta
data: {"nombre": "previsualizar_venta", "estado": "llamando"}

event: confirmacion
data: {"tipo": "confirmar_venta", "preview": {...}, "huella": {"datos": ..., "firma": "..."}}

event: fin
data: {"estado": "pausado"}

event: error
data: {"mensaje": "..."}
```

`token` es lo que el panel escribe. `herramienta` le permite mostrar
«consultando lotes…» en vez de una pantalla quieta, y es justo el tramo largo
porque ahí corre FIFO contra la base. `confirmacion` trae la tarjeta y la
huella. `fin` dice si el agente terminó o quedó pausado.

### Los errores son eventos, no códigos

Con SSE el `200 OK` ya salió con el primer byte. Un fallo a mitad del turno
**no puede** ser un código HTTP: es un evento `error` y se cierra el flujo. El
panel tiene que tratar "la respuesta arrancó bien y después falló" como un caso
normal.

### La huella no cambió

Se guarda **opaca** y se devuelve **byte por byte**. Un panel que la recalcule
produce una firma que no verifica y la escritura se rechaza con
`huella_no_valida`. Esa regla es la que sostiene toda la protección.

## Hilos

**Tabla `agent_threads`:** id (el mismo que usa el checkpointer, así que no hay
dos identidades para la misma conversación), dueño, título, fechas. El título
sale del primer mensaje truncado y se puede renombrar; sin eso la lista es una
columna de fechas.

**Una sola regla: sólo el dueño.** Leer, escribir, aprobar, renombrar, borrar.

| | Dueño | Cualquier otro |
|---|---|---|
| Leer | sí | no |
| Escribir y aprobar | sí | no |
| Renombrar y borrar | sí | no |

Hoy cualquier usuario autenticado puede reanudar cualquier hilo — la revisión de
la rama anterior lo marcó y se dejó pasar porque había un solo usuario. Esto lo
cierra.

La comprobación vive en **un solo lugar**, una función que hoy responde "¿sos el
dueño?". Agregar lectura compartida después es aditivo.

**Endpoints:** listar, crear, renombrar, borrar.

## Lo que se borra

Código probado que deja de tener sentido. Se enumera porque borrar trabajo
verificado merece decirse en voz alta:

| Se va | Por qué |
|---|---|
| `langgraph.json` | No hay servidor que lo lea |
| `app/agent/auth_hook.py` y sus tests | La autenticación vuelve a `get_current_user` |
| La lógica de la clave reservada en `user_id_from_config` | El `configurable` lo arma el endpoint; no hay servidor externo al que engañar |
| `_postgres_checkpointer` sincrónico | Reemplazado por el asíncrono |
| `.venv-agent/` y su sección en `docs/agente.md` | Un solo entorno, un solo conjunto de dependencias |

Con eso desaparece también el conflicto de starlette que impedía compartir
imagen: `langgraph-api` exigía `starlette>=1.3.1` y FastAPI `<0.51.0`.

## Pruebas

**Sin cambios:** los tests de las herramientas, la firma, la idempotencia y el
grafo. Nada de eso depende de quién lo expone.

**Nuevos, sobre el endpoint:**

- Un mensaje produce la secuencia de eventos esperada.
- Una interrupción aparece como `confirmacion`, con su huella.
- Reanudar con una aprobación escribe.
- Un fallo a mitad del flujo sale como evento `error`, no como excepción colgada.
- **El `user_id` sale del token y no del cuerpo**: mandar `{"user_id": "otro"}`
  no cambia quién firma la venta.
- **Un usuario no puede leer ni escribir el hilo de otro.** Sin este test la
  autorización es una intención.

## Fuera de alcance

- **Compartir hilos** — decisión diferida; el modelo deja lugar.
- **El panel** — vive en su propio repo.
- **La pieza 2B** — planificador y bandeja de notificaciones.
- **Reanudar conversaciones muy largas sin costo creciente** — cada turno reenvía
  el historial; cuando duela, se resuelve con resumen o poda.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| Un turno largo ocupa un hilo de trabajo por herramienta | El pool de hilos de anyio tiene tope; con un usuario no se alcanza, y el síntoma sería latencia, no corrupción |
| `AsyncPostgresSaver` abre su propio pool además del de SQLAlchemy | Dos pools en un proceso; vigilar el contador de conexiones en las primeras corridas |
| Una conversación larga encarece cada turno | Declarado fuera de alcance; crear un hilo nuevo es la salida manual |
| El panel deja de recibir eventos y el turno sigue corriendo | El endpoint detecta la desconexión y cancela la corrida |
