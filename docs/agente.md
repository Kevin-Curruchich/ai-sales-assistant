# El agente conversacional

`app/agent/` es un grafo de LangGraph (`create_react_agent`, `version="v2"`)
con siete herramientas -- tres de lectura, tres de escritura que pausan a
pedir confirmacion humana, y `buscar_cliente`. Corre en un **proceso
separado** del backend FastAPI de este mismo repositorio, aunque comparte su
base de datos y sus modelos (`app/models`, `app/services`).

Este documento cubre como correrlo local, como se despliega, por que el
checkpointer vive en su propio schema de Postgres, el contrato que el panel
tiene que cumplir -- el token de Firebase en cada request y la huella al
aprobar una escritura -- y el checklist antes de desplegar el enganche de
caja. Si estas por tocar las herramientas de
escritura, lee primero los docstrings de modulo de
`app/agent/tools/write.py` y `app/agent/graph.py` -- son mas largos que el
codigo que documentan a proposito, y este archivo asume que los leiste.

## Por que dos procesos

El panel usa `useStream`, del LangGraph JS SDK, para hablar con el agente.
`useStream` necesita un **servidor LangGraph real** del otro lado (el
protocolo de streaming de LangGraph, no un endpoint REST comun) -- no hay
forma de servir eso desde dentro de la aplicacion FastAPI existente sin
adoptar ese protocolo ahi tambien.

Aparte de esa restriccion tecnica, hay una razon para mantenerlos separados
aunque no la hubiera: una corrida del agente que crashea -- un prompt mal
armado, una herramienta que revienta, un limite de la API de Anthropic -- no
tiene por que tirar abajo el backend del que depende el resto del panel
(clientes, ventas, reportes). Dos procesos, un crash en uno no toca al otro.

Los dos procesos:

- **API** (`hypercorn app.main:app`): lo que ya existia. Sirve el panel de
  administracion (clientes, ventas, compras, reportes).
- **Agente** (`langgraph dev` en local / el servidor de LangGraph en
  produccion): sirve el grafo conversacional que `useStream` consume.

Ambos leen la misma base de datos Postgres, con el mismo `POSTGRES_SCHEMA`
para las tablas del negocio -- pero el agente ademas usa el schema `agent`
para su propio estado (ver mas abajo). Ninguno de los dos importa al otro en
tiempo de ejecucion.

## Correrlo local

### Variables de entorno

Ademas de las que ya pide el backend (ver `docs/desarrollo-local.md`), el
agente necesita:

| Variable | Para que |
|---|---|
| `ANTHROPIC_API_KEY` | El modelo (`ChatAnthropic`, `claude-sonnet-5`). Sin esto, `graph()` falla al construirse -- ver el docstring de `app/agent/graph.py::graph`. |
| `LANGSMITH_TRACING` | `true`/`false`. Activa el trazado de cada corrida en LangSmith. Opcional para desarrollo, util para depurar una conversacion completa (incluidas las pausas de `interrupt()`). |
| `LANGSMITH_API_KEY` | Credencial de LangSmith. Solo hace falta si `LANGSMITH_TRACING=true`. |
| `LANGSMITH_PROJECT` | Nombre del proyecto en LangSmith donde aparecen esas trazas. |

Ninguna de las cuatro pasa por `app/core/config.py` (`Settings`, pydantic) --
`ChatAnthropic` y el SDK de LangSmith las leen directo de `os.environ`. Van
en `.env` como cualquier otra variable local; `.env.example` las trae
comentadas.

### Instalar `langgraph-cli`

**`langgraph-cli` no esta instalado en el venv de este repo.** No esta en
`requirements.txt` ni en `requirements-dev.txt` -- es una herramienta de
desarrollo para correr el servidor local, no una dependencia de lo que se
despliega (en produccion el servidor de LangGraph se levanta distinto, ver
"Desplegarlo" mas abajo). Instalalo aparte:

```bash
venv/bin/python -m pip install "langgraph-cli[inmem]"
```

(`venv/bin/pip` esta roto en este venv -- resuelve a un Python 3.14 vacio.
Usa siempre `venv/bin/python -m pip`, igual que en el resto del repo.)

### Levantarlo

Contra la base de desarrollo local (`db_local`, puerto 55433 --
`docker-compose.dev.yml`, ver `docs/desarrollo-local.md`):

```bash
docker compose -f docker-compose.dev.yml up -d
venv/bin/alembic upgrade head   # si todavia no corriste esto
langgraph dev
```

`langgraph.json` ya apunta a la fabrica del grafo
(`app/agent/graph.py:graph`) y carga `.env`. `langgraph dev` levanta un
servidor local con un explorador de hilos (LangGraph Studio) para probar
conversaciones y ver las pausas de `interrupt()` sin necesidad del panel.

**Advertencia:** `langgraph dev` nunca se corrio de verdad en esta rama.
Todo lo de arriba -- que `langgraph-cli` haga falta, que `langgraph.json`
este bien armado, que el grafo arranque limpio contra `db_local` -- se
infiere del codigo y de `langgraph.json`, no de haberlo visto correr. Es la
UNICA verificacion manual que falta antes de dar esta pieza por terminada;
quien la corra por primera vez deberia esperarse a tener que ajustar algo
(un import, una variable de entorno que falta, una version de `langgraph-cli`
incompatible con `langgraph==1.0.3`) y, si algo sale distinto de lo escrito
aca, actualizar este documento.

## Desplegarlo

La forma general esta clara: segundo servicio de Railway, mismo
repositorio, mismo `.env` de produccion (misma base de datos), proceso
separado del de la API. **El comando de arranque exacto -- y si de verdad
puede ser "el mismo `Dockerfile`, otro `CMD`" o necesita algo distinto --
todavia no esta determinado.** Esta seccion lo dice plano en vez de inventar
un comando prolijo: es el mismo tipo de hueco que la advertencia sobre
`langgraph dev` de arriba, pero un nivel mas grave, porque ahi no hay ni
siquiera un `langgraph dev` corrido una vez para apoyarse -- es el otro
lugar de todo este documento donde la confianza se habia adelantado a la
verificacion, y quedo corregido aca por la misma razon que aquella
advertencia existe: mejor decir "no se sabe" que afirmar un mecanismo que
nadie corrio.

Lo que si se puede afirmar, verificado contra este repo:

- **Ninguna dependencia de servidor de LangGraph esta en
  `requirements.txt`.** No hay `langgraph-cli` ni `langgraph-api` (el paquete
  que de verdad implementa el servidor -- `langgraph dev`/`langgraph up` lo
  usan por debajo). Sin uno de los dos instalado en la imagen de produccion,
  no hay nada que escuche el protocolo de streaming que `useStream` necesita.
- `langgraph-cli` (visto en PyPI, no instalado aca) expone `langgraph dev`
  (desarrollo, recarga en caliente, pensado para localhost) y, para
  produccion, `langgraph up`/`langgraph build`: los dos arman una imagen de
  Docker PROPIA a partir de `langgraph.json` (con su propio `Dockerfile`
  generado, no el de este repo) y, en el flujo documentado por LangChain,
  esperan a la vez Postgres (para el checkpointer, que ya tenemos) y
  colas/infra adicional de la plataforma LangGraph -- no simplemente "otro
  comando sobre la misma imagen".
- Instalar `langgraph-api` directo (el paquete que de verdad sirve el
  protocolo) es la otra via, mas cercana a "un proceso mas en el mismo
  `Dockerfile`" -- pero no esta probado aca, y sus dependencias (visto en su
  metadata de PyPI) incluyen piezas pensadas para el runtime en memoria de
  desarrollo (`langgraph-runtime-inmem`); un runtime de Postgres para
  produccion parece vivir en un paquete separado, no publico en PyPI al
  momento de escribir esto -- posiblemente detras de la licencia de
  LangGraph Platform. No se confirmo si eso aplica al uso que este proyecto
  le da (un solo grafo propio, sin multiinquilino).

Puntos a confirmar antes de poder desplegar el segundo servicio -- ninguno
verificado en esta rama:

- **Que paquete sirve el servidor y como se instala.** `langgraph-cli[inmem]`
  no esta pensado para produccion (su extra `inmem` lo dice: es el runtime
  de desarrollo). Hay que decidir entre `langgraph-api` instalado directo
  (si su runtime de Postgres esta disponible sin licencia adicional) o
  adoptar el flujo `langgraph build`/`langgraph up` de LangGraph Platform
  (que trae su propia imagen y, probablemente, sus propios requisitos de
  infraestructura mas alla de este Postgres).
- **El comando de arranque exacto**, una vez resuelto el punto anterior --
  no hay ninguno verificado hoy, ni en este documento ni en el repo
  (`entrypoint.sh` es especifico del servicio de la API).
- **Si el segundo servicio puede reusar el `Dockerfile` de este repo tal
  cual** (con un `CMD`/comando de arranque distinto en la configuracion de
  Railway) o si necesita su propia imagen -- generada por `langgraph build`
  o armada a mano -- porque el runtime de produccion trae dependencias que
  `requirements.txt` no tiene hoy.
- Las cuatro variables de entorno de la seccion anterior, cargadas en
  Railway igual que las demas (`ANTHROPIC_API_KEY` es la unica realmente
  obligatoria para que el grafo arranque; las tres de LangSmith son
  opcionales).
- El resto de las variables de `.env.production` (`POSTGRES_*`/`DATABASE_URL`,
  credenciales de Firebase) las necesita tambien el agente: valida el mismo
  token de Firebase que la API, en el hook de `app/agent/auth_hook.py` (que
  compone `app/agent/auth.py::resolve_user`), y lee/escribe con los mismos
  modelos y el mismo `POSTGRES_SCHEMA`. `FIREBASE_CREDENTIALS_PATH` no es
  opcional para este servicio: sin ella, `initialize_firebase()` falla y
  **todas** las escrituras del agente se rechazan con 401.
- **Solo un servicio debe correr `alembic upgrade head`.** El proceso del
  agente no deberia repetir esa migracion al arrancar -- ya la corre
  `entrypoint.sh` del lado de la API. Si el arranque que se elija para el
  agente en produccion tambien la dispara (por ejemplo si termina
  reusando `entrypoint.sh` tal cual), confirmar que correrla dos veces en
  paralelo, en dos deploys que arrancan casi al mismo tiempo, no genere una
  condicion de carrera contra `alembic_version`. Si el arranque del agente
  es un proceso propio (no `entrypoint.sh`), la solucion mas simple es que
  ese proceso directamente no corra `alembic upgrade head` -- que lo siga
  corriendo solo el servicio de la API, y que el del agente dependa de que
  ese deploy ya haya migrado el schema del negocio.
- El schema `agent` se crea solo, en el primer arranque del grafo (ver
  abajo) -- no hace falta nada manual para eso, sea cual sea el mecanismo de
  arranque que se termine eligiendo.

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
Alembic a proposito.

**`PostgresSaver.setup()` NO crea ese schema.** Un borrador anterior de la
spec de este proyecto asumia que si -- es falso, y vale la pena decirlo
explicito porque es facil de asumir lo contrario: las migraciones internas
de `PostgresSaver` (`langgraph.checkpoint.postgres.PostgresSaver.MIGRATIONS`)
son DDL sin calificar (`CREATE TABLE checkpoints`, no
`CREATE TABLE agent.checkpoints`) que resuelve donde aterriza por el
`search_path` de la conexion -- ningun `CREATE SCHEMA` en ninguna parte de
esas migraciones. Contra una base fresca, `SET search_path TO "agent"` con
el schema `agent` inexistente no falla ahi (Postgres permite un
`search_path` que nombra un schema que todavia no existe) -- falla recien en
el primer `CREATE TABLE` de `setup()`, con
`InvalidSchemaName: no schema has been selected to create in`. Un lector que
de por sentado que LangGraph resuelve esto solo va a perder una tarde
entera persiguiendo ese error contra una base nueva (un `db_local` recien
creado, un ambiente de CI, produccion en su primer deploy).

`_postgres_checkpointer` en `app/agent/graph.py` lo resuelve explicito,
antes de fijar el `search_path` y antes de llamar a `setup()`:

```python
conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
conn.execute(f'SET search_path TO "{schema}"')
checkpointer = PostgresSaver(conn)
checkpointer.setup()
```

El mismo patron que `alembic/env.py` usa para el schema del negocio. No hace
falta ningun paso manual antes de desplegar por esto -- se crea solo, en el
primer arranque del grafo, contra cualquier base (local, produccion, CI).

## El contrato del panel: el token de Firebase

Antes de la huella viene esto, porque sin esto no hay escritura posible: el
panel tiene que mandar el token de Firebase del usuario en el header
`Authorization` de **cada** request al servidor del agente.

```
Authorization: Bearer <id token de Firebase>
```

Con `useStream` del SDK de JS eso se configura una vez, en las opciones del
hook (`defaultHeaders` / `headers`), no request por request. El token es el
mismo que el panel ya manda al backend de FastAPI -- no hay una credencial
nueva ni un segundo login.

### Que hace el servidor con el

`langgraph.json` declara el hook:

```json
"auth": { "path": "./app/agent/auth_hook.py:auth" }
```

En cada request, el servidor corre el handler `@auth.authenticate` de ese
archivo. El handler valida el token contra Firebase (el mismo
`verify_firebase_token` que usa la API), resuelve o crea el `User` local
(`resolve_user`) y devuelve su `id` como `identity`. El servidor deja ese
valor en el `configurable` de la corrida bajo `langgraph_auth_user_id`, y ahi
lo lee `user_id_from_config` -- el unico canal por el que las tres
herramientas de escritura saben quien firma una venta o una compra.

**Esa clave es afirmada por el servidor, no por el panel.**
`langgraph_auth_user_id` (y `langgraph_auth_user`) estan en las claves
reservadas del validador del servidor: un request que intente mandarlas en su
propio `config` se rechaza. Es la diferencia que importa: un `user_id` suelto
en `config.configurable` -- que es lo que esta rama leia antes de esta
correccion -- es un dato que el llamador afirma y nadie valida, y cualquiera
que alcanzara el puerto podia escribir como quien quisiera.
`user_id_from_config` ya **no** lo acepta.

La identidad se reinyecta en cada corrida y no se persiste en el checkpoint.
Eso incluye la corrida que reanuda una pausa de `interrupt()`: una aprobacion
la firma quien la aprueba, con el token de ESE request, no quien empezo la
conversacion dias antes.

### Que pasa si el token falta o no sirve

Un token ausente, vacio, vencido o invalido es un **401** del servidor, antes
de que la conversacion llegue a existir. El handler convierte a 401 tambien
el caso de que Firebase no se pueda inicializar en el proceso del agente
(credenciales ausentes en el servicio): el request no esta autenticado, y un
500 seria una mentira sobre la causa.

### Dos limites conocidos

- **No hay autorizacion, solo autenticacion.** El hook registra
  `@auth.authenticate` y ningun handler `@auth.on`. Una vez autenticado,
  cualquier usuario puede listar, leer y reanudar cualquier hilo del
  servidor -- incluidas las aprobaciones pendientes de otro. Para un negocio
  de un solo dueño con un puñado de usuarios de confianza es aceptable;
  aislar hilos por usuario es una pieza aparte, con su propio criterio de
  producto sobre quien puede ver que conversacion.
- **LangGraph Studio no pasa por este hook.** Con la autenticacion de Studio
  activa (el default), un request del Studio se autentica contra LangSmith y
  la identidad que llega es `"langgraph-studio-user"`, no un UUID de la tabla
  `users`. Las herramientas de LECTURA funcionan; las tres de escritura
  fallan con `AgentAuthError` porque esa identidad no es un UUID valido. Para
  probar escrituras desde el Studio hay que agregar
  `"disable_studio_auth": true` al bloque `auth` de `langgraph.json` y mandar
  un token de Firebase real, o probarlas desde el panel.

**Nada de esta seccion se corrio contra un servidor LangGraph de verdad** --
vale la misma advertencia que la seccion "Levantarlo". Lo que si esta
verificado, leyendo el fuente de `langgraph-sdk` 0.2.9 (instalado en este
venv), `langgraph-cli` 0.4.32 y `langgraph-api` 0.15.1: la forma de la
entrada `auth` en `langgraph.json`, que un handler sincrono esta soportado
(el servidor lo envuelve en `run_in_threadpool`), que `authorization` es un
parametro que el servidor sabe inyectar, que el resultado aterriza en
`configurable` como `langgraph_auth_user_id`, y que esa clave es reservada.
`app/agent/auth_hook.py` cita el archivo y la linea de cada uno de esos
puntos.

## El contrato del panel: la huella

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
  borrador (con la marca) y `confirm()` comitea el stock y la salida de caja
  aparte. Un proceso que muera **entre las dos** deja un borrador sin
  confirmar que la reanudacion ya no vuelve a tocar, porque la marca ya
  esta. No hay compra duplicada ni caja duplicada -- el resultado es una
  compra a medias que alguien tiene que confirmar o borrar a mano desde el
  panel. Cerrarlo de verdad pide que `create()` y `confirm()` compartan
  transaccion, que es un cambio en `PurchaseService`, no en el agente.
- **La tabla se crea sola, la primera vez que una herramienta escribe.** No
  hay migracion de Alembic (a proposito: el schema `agent` esta fuera de su
  radar), asi que el usuario de la base necesita permiso de `CREATE` --
  el mismo que ya necesita para que `_postgres_checkpointer` cree el schema
  `agent` y las tablas del checkpointer al construir el grafo.
- **Una invocacion directa de la herramienta, fuera de un grafo, no se
  desduplica.** No hay id de tarea, no hay reanudacion posible, y dos
  llamadas son dos hechos distintos. La guardia se desactiva sola en vez de
  inventarse una equivalencia.
- **`entity_id` (que fila quedo escrita) se anota despues del commit, en su
  propia transaccion, y es best-effort.** Si se pierde, la repeticion se
  sigue evitando; lo unico que se pierde es poder decir *cual* fila fue.

## Checklist de despliegue: el enganche de caja

Toda venta pagada -- registrada por el agente o por el panel, es el mismo
codigo -- escribe tambien un movimiento de caja (`entrada`,
`CashMovementType.ENTRADA`) ademas de la venta misma. Esto vive en
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
4. Recien entonces desplegar el servicio del agente (o el cambio del
   orquestador de ventas, si no se habia desplegado antes). A partir de
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
