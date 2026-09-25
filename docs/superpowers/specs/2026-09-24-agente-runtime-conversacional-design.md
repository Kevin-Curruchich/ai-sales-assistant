# Runtime conversacional del agente Revenew

**Fecha:** 2026-09-24
**Estado:** diseño aprobado, pendiente de plan de implementación
**Pieza 2A de 4** — 2B (proactividad), 3 (Google Calendar) y el panel tienen su propio ciclo.
**Depende de:** `docs/superpowers/specs/2026-09-22-superficie-servicios-agente-design.md` (pieza 1, ya en `main`)
**Huecos que hereda:** `docs/superpowers/handoffs/2026-09-23-piezas-2-3-agente.md`

## Problema

El agente Revenew vive en Fleet con Google Sheets como base de datos. La pieza 1
construyó la superficie de services que sus herramientas van a importar en
proceso. Falta el agente: el grafo, las herramientas, la confirmación humana y
la identidad con la que escribe.

El export de Fleet tiene 18 herramientas —7 de Slack, 6 de Google Sheets, 5 de
Google Calendar— y **ninguna sobrevive tal cual**. Las de Sheets las reemplazan
los services; las de Calendar son la pieza 3; las de Slack eran el canal de
entrega, que ahora es un panel propio.

## Estado verificado

Contra el repo en `16c6c38` y la base `db_local` (espejo de producción:
21 clientes, 156 ventas, 38 compras, `SUM(sales.total) = 9954.49`).

| Lo que hace falta | Estado hoy |
|---|---|
| Grafo, checkpointer, entrypoint | No existe. El spike de WhatsApp se borró en `16c6c38`. |
| `SaleService.preview_sale(data)` | Existe. Devuelve lotes, costo, precio sugerido, `is_habitual_price`, márgenes, totales y advertencias. |
| `SaleService.create(data, user_id)` | Existe. Escribe `customer_id`, `user_id`, `date`, `total`, `is_payment_pending`, `items` — **y nada más**. |
| `payment_method` / `payment_date` / `occurred_at` en ventas | Columnas creadas y con CHECK. **Ninguna ruta de código las escribe.** |
| `CashService.record(...)` | Existe y commitea internamente. Nada fuera de `cash_service.py` lo invoca. |
| `cash_movements` en producción | **Vacía. Cero filas.** Las 156 ventas históricas no generaron movimientos. |
| Autenticación | Firebase, en `app/api/dependencies.py`. El proceso del agente no la tiene cableada. |

## Decisiones

| Decisión | Elegido | Descartado y por qué |
|---|---|---|
| Proactividad | El agente corre solo y avisa | Reactivo puro era más chico; se quiso el comportamiento completo. Se separó a 2B porque el planificador no tiene qué planificar hasta que las herramientas existan. |
| Canal de aviso (2B) | Bandeja persistida en el panel | Correo y push web suman proveedor, claves y fallos silenciosos fuera de control. |
| Topología | Dos procesos, un repo | Un solo proceso FastAPI rompe `useStream`; LangGraph Platform no puede importar los services. |
| Confirmación | Toda escritura se confirma | Deshacer una venta con FIFO exigiría devolver cantidades a los lotes, y eso no existe. |
| Identidad | El humano que aprobó | Un usuario «agente» dedicado pierde quién aprobó cada operación. |
| Herramientas | Sobre las costuras de los services | Una por skill obliga al LLM a encadenar cuatro pasos que `preview_sale` hace en uno. |
| Venta → caja | Enganche completo + saldo inicial | Sin saldo inicial el acumulado parece un saldo y no lo es. |

## Arquitectura

### Dos procesos, un repo

El grafo vive en `app/agent/` e importa los services como cualquier módulo:
`from app.services.sales import SaleService`. Arranca por su cuenta.

| | Local | Railway |
|---|---|---|
| API | `hypercorn app.main:app` | servicio actual |
| Agente | `langgraph dev` (2024) | segundo servicio, mismo repo |

Comparten código, modelos y base; no comparten proceso. Un error del agente no
puede tirar la API que sirve al panel.

### El checkpointer va en su propio schema

LangGraph persiste hilos y estado en tablas propias (`checkpoints`,
`checkpoint_writes`, `checkpoint_blobs`). Si viven en `db_local`/`db_v2`, el
`autogenerate` de Alembic las lee como deriva y emite `drop_table` en cada
migración — el mismo problema que costó una task entera en la pieza de Alembic.

Van al schema `agent`, que Alembic no mira. Ese schema NO se crea solo:
`PostgresSaver.setup()` versiona y crea las tablas del checkpointer, pero corre
DDL sin calificar (`CREATE TABLE checkpoints`, no `CREATE TABLE agent.checkpoints`)
que aterriza donde apunte el `search_path` de la conexión — no hay ningún
`CREATE SCHEMA` en el paquete. Contra una base fresca (un `db_local` nuevo,
producción, CI) sin el schema `agent` todavía creado, `setup()` revienta con
`InvalidSchemaName: no schema has been selected to create in`. `app/agent/graph.py`
(`_postgres_checkpointer`) crea el schema explícito con `CREATE SCHEMA IF NOT
EXISTS` — la misma técnica que `alembic/env.py` ya usa para el schema del
negocio — antes de fijar `search_path` y de llamar `setup()`.

### Modelo

`claude-sonnet-5` para el bucle del agente: es tool-calling sostenido con
conversación, no razonamiento profundo, y la latencia importa cuando estás
registrando una venta parado en el negocio. `claude-opus-5-5` queda disponible
si alguna tarea lo justifica.

Dependencias: **agregar** `langchain-anthropic` y `langgraph-checkpoint-postgres`;
**quitar** `openai` y `langchain-openai`, que quedaron del spike borrado.

### Autenticación e identidad

El panel manda su token de Firebase al servidor LangGraph, que lo valida
reutilizando `app/api/dependencies.py`. De ahí sale el `user_id` que firma las
filas.

La operación la propone una conversación y la aprueba una persona: el registro
de auditoría nombra a esa persona.

### Tracing

LangSmith por variables de entorno (`LANGSMITH_TRACING`, `LANGSMITH_API_KEY`,
`LANGSMITH_PROJECT`). Sin código: cada conversación, cada llamada a herramienta
y cada interrupción quedan registradas.

### Desarrollo

Contra `db_local` (`docker-compose.dev.yml`, puerto 55433). El agente puede
registrar ventas equivocadas todo el día sin tocar el negocio.

## Las herramientas

Siete. Las firmas de los services están verificadas contra el código.

### De solo lectura

**`buscar_cliente(nombre)`** → `CustomerService.get_all(search=..., limit=10)`
Devuelve candidatos con id, nombre y empresa. Con más de un resultado el agente
**pregunta**; nunca elige. Cubre `buscar-cliente`.

**`previsualizar_venta(cliente_id, items, fecha)`** → `SaleService.preview_sale(SaleCreate(...))`
La herramienta central. Una llamada devuelve, por ítem: de qué lotes sale y
cuánto de cada uno, `cost_basis_unit`, `suggested_unit_price`,
`is_habitual_price`, márgenes, totales, y advertencias si los lotes no alcanzan.

Cubre **cuatro** skills — `consumir-lote-fifo`, `verificar-lote-disponible`,
`detectar-precio-habitual` y `calcular-margen-extra` — y cierra el hueco que la
revisión final marcó: el agente no reconstruye la ventana de márgenes
habituales, la lee del preview.

**`consultar_seguimiento(filtro)`** → `SaleService.get_follow_ups(filter_type, limit, offset)`
Quién está vencido, con intervalo y fecha estimada. Cubre `calcular-proyeccion`
en su uso real.

**`consultar_caja(desde, hasta)`** → `CashService.running_balance()` / `owner_balance()` / `ledger()`

### De escritura — las tres se confirman

**`registrar_venta(...)`** → `SaleService.create(data, user_id)`
**`registrar_compra(...)`** → `PurchaseService.create(data, user_id)`
**`registrar_movimiento_caja(...)`** → `CashService.record(occurred_at, type, amount, payment_method, note)`

Los movimientos de caja que acompañan a una venta o a una compra **no los crea
la herramienta**: los crea el service (ver «Enganche venta → caja»). La
herramienta de caja es para lo que no nace de una operación — aportes, retiros,
gastos, depósitos.

### Las nueve skills cambian de rol

Dejan de ser procedimientos que el agente ejecuta paso a paso y pasan a ser
documentación de dominio en el prompt: el FIFO, los márgenes, qué significa
`aporte_socio`. El agente entiende el negocio por ellas y actúa por las
herramientas.

## La confirmación

### Mecánica

La herramienta de escritura llama a `interrupt()` con la operación ya resuelta.
LangGraph congela el grafo, guarda el estado en el checkpointer, y devuelve el
payload al panel. Al responder, el cliente reanuda con `Command(resume=...)` y
la ejecución sigue **desde adentro de la herramienta**.

Como el estado vive en Postgres, cerrar el panel a mitad de una confirmación no
pierde nada ni escribe solo: el hilo sigue esperando.

### Qué se muestra

La operación armada, no el pedido crudo. Payload JSON estructurado —no texto—
para que el panel pinte una tarjeta:

```
Venta a Aurita — 2026-09-24
  1 cartón de huevos (30 U)
    precio      Q36.00   ← su precio habitual, no el estándar de Q37
    sale de     lote del 2026-09-18 a Q33.33/u
    costo       Q33.33
    margen      Q2.67
  Total: Q36.00 · Ganancia: Q2.67
```

### Las tres respuestas

Aprobar, corregir o cancelar. **Corregir no vuelve a la conversación**: reanuda
con los valores editados, recalcula el preview y vuelve a pedir confirmación.
Cambiar la cantidad cambia de qué lotes sale y cuánto cuesta.

### Recalcular al escribir

`registrar_venta` recalcula la asignación FIFO **dentro de la misma transacción
que escribe**. Si el resultado difiere de lo mostrado, no escribe: muestra la
diferencia y vuelve a pedir confirmación.

Esto no es precaución abstracta. En la pieza 1, `preview_sale` y `create`
diferían un centavo en ventas fraccionarias, y si el panel reenviaba el precio
previsualizado, `create` lo rechazaba con 422 — una venta válida imposible de
registrar. El preview y la escritura son dos momentos distintos y entre ellos
puede entrar otra venta que consuma los mismos lotes.

De paso resuelve las confirmaciones viejas: una aprobación de hace tres días no
escribe sobre un inventario que cambió.

### No todas son igual de reversibles

Un movimiento de caja mal registrado se corrige con otro movimiento. Una venta
mal registrada consume lotes y hoy no hay forma de devolverlos:
`recalculate_sale_snapshots_for_products` es un no-op por diseño. Por eso la
confirmación de venta muestra el lote y el costo, no solo el total: es el único
momento en que se puede detectar.

## Huecos de escritura a cerrar

**1. Schemas de entrada.** `SaleCreate` gana `medioPago`, `fechaPago` y
`occurredAt`; `PurchaseCreate` gana `medioPago`. `create()` debe escribirlos.

Default: **venta pagada salvo que se diga lo contrario**, `fechaPago` igual a la
fecha de venta. La compra puede quedar a medio pagar.

**2. `CashService.record(..., commit=True)`.** Sin el parámetro,
`registrar_compra` hace tres commits separados y una caída entre medio sube el
inventario sin movimiento de caja.

**3. `occurred_at` en la venta.** Hoy se deriva `date` solo. Con el agente
importa más: «vendí esto hace un rato» y «vendí esto ayer» deben distinguirse.

**4. Enganche venta → caja, con saldo inicial.**

Una venta pagada genera entrada de caja, **desde el agente y desde el panel**
— va en `SaleService.create`, no en la herramienta, para que las mismas ventas
dejen el mismo rastro por donde se registren. Es un cambio de comportamiento en
un endpoint vivo y va en el checklist de despliegue.

La compra **no** es simétrica: tiene ciclo de vida. `create()` deja
`status="draft"` y `confirm()` es la que la vuelve real y libera los lotes. El
movimiento de caja va en `PurchaseService.confirm()`, no en `create()` — un
borrador todavía no gastó nada. Genera una `salida` por lo efectivamente pagado.

Por eso la herramienta `registrar_compra` crea **y confirma** en una sola
operación confirmada por vos: un borrador que el agente deja colgado es peor que
no haberlo creado. El `aporte_socio` de los pasos 5–6 de la skill
`registrar-compra` **no es automático** — sólo existe cuando el dinero lo pone
un socio, y eso el agente lo pregunta antes de confirmar. Una compra pagada con
caja del negocio genera `salida` y nada más.

Ambas composiciones —venta + entrada, compra + salida— son la razón de ser del
`commit=False` del punto 2: service y movimiento entran en la misma
transacción.

Como `cash_movements` está vacía, se carga un movimiento de **saldo inicial**
con el efectivo real al momento del corte. Sin él, el acumulado sería «lo que
entró desde que encendimos esto»: un número que parece un saldo y no lo es.

**Quinto tipo `saldo_inicial`** en `CashMovementType`, con su migración y CHECK
(el patrón de `PaymentMethod`). `aporte_socio` inflaría `owner_balance()`, que
significa «lo que el negocio le debe al socio», y el saldo inicial no es una
deuda salvo que el dinero venga del bolsillo del socio. `entrada` significa
cobro de venta.

> Advertencia que el propio código deja escrita en `app/models/cash_movement.py`:
> un quinto tipo agregado a un lado y no al otro hace que el último saldo del
> ledger difiera del `running_balance` de SQL. `CASH_OUTFLOW_TYPES`, el `case()`
> del repositorio y el vocabulario de los tests se tocan **en el mismo commit**.
> `saldo_inicial` es entrada: no va en `CASH_OUTFLOW_TYPES`. Ya existe el test
> que atrapa la desincronización.

## Pruebas

**Sin LLM.** Las herramientas son funciones normales: sesión de base y datos de
prueba, sin llamar al modelo. Cubre lo que puede corromper datos — que
`registrar_venta` escriba lo que el preview mostró, que la composición de
`registrar_compra` sea atómica, que `buscar_cliente` no elija solo, que el
recálculo al escribir detecte lotes consumidos entre medio.

**Con el grafo.** Que la interrupción suspenda de verdad, que el estado
sobreviva al reinicio, y que reanudar con una corrección recalcule en vez de
escribir lo viejo. Checkpointer en memoria y modelo falso: sin clave de API ni
red.

**A mano.** Que el agente entienda «vendí dos cartones a Aurita». Eso es calidad
de prompt, no corrección de código; se mira en LangSmith, contra `db_local`.

## Fuera de alcance

- **Planificador y notificaciones** — pieza 2B, construida sobre estas
  herramientas.
- **Google Calendar** — pieza 3.
- **Integración del panel con `useStream`** — repo del frontend. Acá queda el
  servidor con su API estable.
- **Deshacer una venta** — exigiría devolver cantidades a los lotes. La
  confirmación previa es la mitigación elegida.
- **Los menores diferidos** del handoff que no bloquean (ventana de `ledger()`,
  tile `needs_estimate`, reexports de `__init__`).

## Riesgos

| Riesgo | Mitigación |
|---|---|
| El agente escribe contra producción durante el desarrollo | `.env` apunta a `db_local`; producción exige cargar `.env.production` a mano. |
| Las tablas de LangGraph confunden a Alembic | Schema `agent`, separado, fuera de `include_schemas`. |
| El enganche a caja cambia el comportamiento del panel | Va en el checklist de despliegue, con el saldo inicial cargado antes. |
| El quinto tipo de movimiento desincroniza ledger y SQL | Un solo commit toca enum, `CASH_OUTFLOW_TYPES`, repositorio y tests; el test existente lo atrapa. |
| Dos procesos que comparten base pueden pelearse por transacciones | Cada proceso abre su propia sesión; las escrituras del agente son cortas y confirmadas. |
