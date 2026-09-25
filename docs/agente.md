# El agente conversacional

`app/agent/` es un grafo de LangGraph (`create_react_agent`, `version="v2"`)
con siete herramientas -- tres de lectura, tres de escritura que pausan a
pedir confirmacion humana, y `buscar_cliente`. Corre en un **proceso
separado** del backend FastAPI de este mismo repositorio, aunque comparte su
base de datos y sus modelos (`app/models`, `app/services`).

Este documento cubre como correrlo local, como se despliega, por que el
checkpointer vive en su propio schema de Postgres, el contrato que el panel
tiene que cumplir al aprobar una escritura, y el checklist antes de
desplegar el enganche de caja. Si estas por tocar las herramientas de
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

Segundo servicio de Railway, mismo repositorio, mismo `Dockerfile`, mismo
`.env` de produccion (misma base de datos) -- distinto comando de arranque.
El servicio de la API sigue usando `entrypoint.sh` (`alembic upgrade head`
seguido de `hypercorn app.main:app`). El servicio del agente arranca el
servidor de LangGraph en su lugar, contra el mismo `langgraph.json`.

Puntos a confirmar al armar ese segundo servicio (no verificados en esta
rama, ver la advertencia arriba sobre `langgraph dev`):

- Las cuatro variables de entorno de la seccion anterior, cargadas en
  Railway igual que las demas (`ANTHROPIC_API_KEY` es la unica realmente
  obligatoria para que el grafo arranque; las tres de LangSmith son
  opcionales).
- El resto de las variables de `.env.production` (`POSTGRES_*`/`DATABASE_URL`,
  credenciales de Firebase) las necesita tambien el agente: autentica con el
  mismo Firebase (`app/agent/auth.py::resolve_user`) y lee/escribe con los
  mismos modelos y el mismo `POSTGRES_SCHEMA` que la API.
- **Solo un servicio debe correr `alembic upgrade head`.** El proceso del
  agente no deberia repetir esa migracion al arrancar -- ya la corre
  `entrypoint.sh` del lado de la API. Si el arranque del agente en
  produccion tambien la dispara (por ejemplo si reusa `entrypoint.sh` tal
  cual), confirmar que correrla dos veces en paralelo, en dos deploys que
  arrancan casi al mismo tiempo, no genere una condicion de carrera contra
  `alembic_version`.
- El schema `agent` se crea solo, en el primer arranque del grafo (ver
  abajo) -- no hace falta nada manual para eso.

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

## Checklist de despliegue: el enganche de caja

Desde Task 9 (commit `491a816` en adelante), toda venta pagada registrada a
traves del agente escribe tambien un movimiento de caja (`entrada`,
`CashMovementType.ENTRADA`) ademas de la venta misma -- ver
`app/services/sales/orchestrator.py`. Esto cambia el comportamiento de un
endpoint que ya esta vivo: **desde el momento en que esto se despliega,
cada venta pagada registrada por el panel suma a la caja.**

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

## Pregunta abierta: que hacer con `Revenew/`

`Revenew/` (`AGENTS.md` y nueve skills) es el prompt del runtime anterior de
este proyecto -- uno que corria en Slack, escribia en Google Sheets y creaba
eventos en Google Calendar. Task 9 decidio, a proposito, no cargarlo en
tiempo de ejecucion: un contenedor desplegado no lo va a tener, y de todas
formas describe herramientas (Sheets, Calendar, Slack) que este agente no
tiene. En su lugar, `app/agent/prompt.py::SYSTEM_PROMPT` es una traduccion
escrita a mano de esas mismas reglas de negocio (FIFO, margenes, vocabulario
de caja, deteccion de precio habitual) a las siete herramientas reales de
este grafo -- ver el docstring de modulo de `prompt.py`.

**Nota sobre el estado real de `Revenew/` al escribir esto (2026-09-25):**
el brief de esta task, y la spec, daban por sentado que `Revenew/` no esta
versionado. Eso fue cierto hasta el commit `5d2be30` de esta misma rama
(`docs: Fix four defects the pre-flight scan found in the plan`,
2026-09-24), que lo agrego a git junto con un cambio de la spec -- casi
seguro sin intencion, de paso en un `git add` mas amplio, no como una
decision deliberada de versionarlo. `git ls-files Revenew/` y
`git log -- Revenew/` lo confirman: los once archivos estan comiteados, sin
ninguna nota que diga que describen un runtime retirado. Esta task no lo
tocó -- no se agrego, modifico ni removio ningun archivo de `Revenew/` aca
(instruccion explicita: no commitearlo desde esta task, y no se hizo) --
pero vale dejar constancia de que la premisa "no esta en git" con la que
arranca esta seccion ya no describe el estado del repositorio, y que quien
decida que hacer con esto deberia primero confirmar con el resto del equipo
si ese commit fue intencional.

Independientemente de como llego a estar versionado, el problema de fondo
que esta pregunta busca resolver sigue igual: las reglas de negocio de
Revenew viven en DOS lugares -- `Revenew/AGENTS.md` (ahora si en git, pero
sin ninguna nota de que describe un runtime que ya no existe) y
`SYSTEM_PROMPT` (lo que el agente real usa). Nada mantiene esos dos textos
sincronizados -- si maniana cambia el margen objetivo de un producto, o la
regla de cuando avisar de un lote cruzado, no hay ningun mecanismo que
fuerce actualizar los dos. Van a divergir con el tiempo, calladamente, y
quien lea `Revenew/AGENTS.md` primero (es mas largo y mas legible como
documento de negocio que el prompt) se va a llevar una regla vieja sin
saber que lo es.

Recomendacion: **dejarlo versionado (ya que asi quedo) pero marcarlo
explicitamente como referencia historica, no como fuente de verdad
activa.** Concretamente:

- Agregar una nota al principio de `Revenew/AGENTS.md` (o un
  `Revenew/README.md` nuevo, si se prefiere no tocar los archivos de skill)
  que diga, en una linea, que este documento describe el runtime
  Slack/Sheets/Calendar anterior, ya retirado, y que la fuente de verdad
  operativa es `app/agent/prompt.py::SYSTEM_PROMPT`.
- No borrarlo ni moverlo: perder el documento de origen hace mas dificil
  auditar si la traduccion a `SYSTEM_PROMPT` capturo todo lo que importaba,
  y sirve como referencia si alguna vez hay que reconstruir por que una
  regla de negocio es como es. Revertir el commit que lo agrego tampoco
  resuelve nada -- solo vuelve a la situacion anterior, donde el documento
  de origen podia perderse sin dejar rastro.

Alternativas consideradas y por que no:

- **Retirarlo del todo** (borrarlo de git y del disco): pierde el historial
  de decisiones de negocio sin ganar nada -- el riesgo de que alguien lo lea
  como vigente se resuelve marcandolo como historico, no borrandolo.
- **Dejarlo tal cual esta ahora** (versionado, sin ninguna nota): es peor
  que marcarlo -- cualquiera que lo encuentre, sobre todo alguien nuevo en
  el proyecto que no sepa de esta migracion, puede leerlo como si fuera la
  especificacion vigente, y es un documento mas legible y mas largo que
  `SYSTEM_PROMPT`, asi que es el que probablemente lea primero.
- **Fusionarlo de verdad con `SYSTEM_PROMPT`** (generar el prompt a partir
  de estos archivos en vez de una traduccion a mano): resuelve la deriva de
  raiz, pero es un cambio de arquitectura mayor -- el prompt dejaria de ser
  un texto curado y pasaria a ser generado, con todo el trabajo de
  filtrar las partes que hablan de Sheets/Calendar/Slack en tiempo de
  build. Fuera de alcance de esta task; vale la pena evaluarlo aparte si la
  deriva entre los dos documentos se vuelve un problema real.

Esta decision no se aplico en este task -- no se agrego ninguna nota a
`Revenew/AGENTS.md` ni se creo un `Revenew/README.md`; los archivos quedan
exactamente como estaban, tal como indica la instruccion de no moverlos ni
borrarlos. Queda como recomendacion para quien decida sobre el
repositorio -- junto con la pregunta previa de si el commit `5d2be30` que
lo versiono fue intencional.
