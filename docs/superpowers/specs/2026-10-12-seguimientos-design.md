# Seguimientos con cierre y espejo en Google Calendar

**Fecha:** 2026-10-12
**Estado:** diseño aprobado, pendiente de plan de implementación

## Problema

"Clientes Prioritarios" no tiene estado: `SaleService.get_follow_ups(filter_type="7_days")`
(`app/services/sales/orchestrator.py`) lo recalcula en cada carga a partir de
`customer_product_cycles.estimated_next_purchase`. Un cliente sale de la lista
sólo cuando **todos** sus productos quedan a más de 7 días.

Consecuencias que se ven en el dashboard:

- Registrar una venta de gas a Aurita no la saca de la lista si los huevos
  vencen en dos días.
- Aun vendiéndole huevos, un intervalo corto deja la próxima fecha otra vez
  dentro de 7 días y la vuelve a mostrar.
- No hay forma de decir "ya la llamé, esta semana no": un seguimiento no tiene
  cierre.

`GET /calendar/events` tiene el mismo origen y el mismo problema.
`customer_product_cycles.calendar_event_id` existe pero nunca se escribió.

## Objetivo

Que cada seguimiento sea un hecho con ciclo de vida —se abre, se cierra— para
que la lista muestre sólo lo pendiente, y que cada seguimiento abierto aparezca
en un calendario de Google del negocio.

Decisiones tomadas con el negocio:

| Pregunta | Decisión |
|---|---|
| Qué cierra un seguimiento | Una venta de ese producto a ese cliente, o un cierre manual |
| Tras un cierre manual | Elegir: **posponer** a una fecha (se abre otro ese día) o **descartar** (no hay otro hasta la próxima venta) |
| Dónde viven | Base de datos como fuente de verdad; Google Calendar como espejo |
| Qué calendario | Uno solo del negocio, con cuenta de servicio (sin OAuth por usuario) |
| Fecha del evento | El día estimado de compra (`estimated_next_purchase`) |
| Granularidad | Un seguimiento por cliente + producto |

## Fuera de alcance

- Herramientas del agente para posponer o descartar.
- Sincronizar de Google hacia la app (mover o borrar el evento en Google no
  cambia nada en la app).
- Reescribir los movimientos o ciclos existentes.
- Agregar pytest con Postgres a CI (recomendado, PR aparte).

## 1. Modelo de datos

Tabla nueva `follow_ups`:

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK; su `.hex` es también el id del evento en Google |
| `customer_id` | uuid FK `customers.id` | |
| `product_id` | uuid FK `products.id` | |
| `due_date` | date | Proyección al abrirse, o la fecha elegida al posponer |
| `status` | varchar(10) + CHECK | `open` / `closed` |
| `close_reason` | varchar(10) + CHECK, null | `sale` / `postponed` / `dismissed`; null mientras está abierto |
| `sale_id` | uuid FK `sales.id`, null, `ON DELETE SET NULL` | Venta que lo cerró |
| `closed_at` | timestamptz, null | |
| `closed_by` | uuid FK `users.id`, null | Null si lo cerró una venta |
| `sync_status` | varchar(10) + CHECK | `pending` / `synced` / `failed` / `skipped` |
| `sync_error` | text, null | Último error de Google |
| `created_at`, `updated_at` | timestamptz | |

- **Invariante:** a lo sumo un seguimiento abierto por par. Índice único parcial
  `(customer_id, product_id) WHERE status = 'open'`, en la base y no sólo en
  Python.
- VARCHAR + CHECK y no enum nativo, mismo patrón y mismas razones que
  `cash_movements.type` (`app/models/cash_movement.py`).
- CHECK de coherencia: `status = 'open'` ⇔ `close_reason IS NULL AND closed_at IS NULL`.
- `customer_product_cycles` sigue siendo la proyección y no cambia, salvo que
  se elimina `calendar_event_id` (nunca usada).

## 2. Flujos

Toda la lógica vive en un `FollowUpService` nuevo (`app/services/follow_up_service.py`).
Cada escritura deja las filas tocadas con `sync_status = 'pending'` y, después
del commit, llama a `request_calendar_sync()` (sección 3).

### Registrar una venta

En `SaleService.create`, por cada producto vendido, en la misma transacción que
la venta (mismo patrón que el movimiento de caja, `commit=False`):

1. Antes de `_update_cycle`, leer `last_purchase_date` del ciclo del par.
2. Si no había ciclo, o `sale.date > last_purchase_date` (la venta es
   información nueva):
   - Cerrar el seguimiento abierto del par, si lo hay: `close_reason='sale'`,
     `sale_id`, `closed_at = now`.
   - Tras `_update_cycle`, si el ciclo tiene `estimated_next_purchase`, abrir
     uno nuevo con esa fecha.
3. Si `sale.date <= last_purchase_date` **no se toca ningún seguimiento**,
   con una excepción: si el par no tenía proyección antes de la venta
   (`estimated_next_purchase` nulo o sin ciclo), la tiene ahora y no hay
   seguimiento abierto, se abre uno en esa proyección.

Por qué la desigualdad estricta, con el caso que lo motivó:

> Ventas de huevos a Aurita el 1 y el 8 de oct; seguimiento abierto el 15. El
> 12 se registra una venta olvidada **con fecha 5 de oct**. Aurita no ha vuelto
> a comprar desde el 8: cerrar el seguimiento la sacaría de Prioritarios justo
> cuando toca llamarla y contaría una conversión falsa. Moverle la fecha
> pisaría un posponer manual. La regla: no tocarlo. La proyección del ciclo sí
> se recalcula.
>
> Excepción: ventas el 8 de oct y una olvidada con fecha 1 de oct registrada
> después. La del 8 no dejó proyección (era la primera compra); la atrasada
> sí la crea. Sin seguimiento el cliente no aparecería en ninguna lista, así
> que se abre uno en la proyección nueva. Esto no puede deshacer un descartar:
> descartar exige una proyección previa, y entonces la excepción no aplica.
>
> Una segunda venta el mismo 8 de oct tampoco toca nada: el seguimiento ya lo
> resolvió la primera de ese día.

Costo aceptado: tras una venta atrasada, `due_date` puede diferir unos días de
la proyección nueva hasta la siguiente venta real.

El agente registra ventas por `SaleService`, así que hereda esto sin cambios.

### Posponer

`FollowUpService.postpone(follow_up_id, new_date, user_id)`:

- 409 si el seguimiento no está abierto; 422 si `new_date <= business_today()`.
- Cierra el actual (`close_reason='postponed'`, `closed_by`) y abre uno nuevo
  con `due_date = new_date`, en una transacción.
- Una venta posterior lo cierra como a cualquier otro.

### Descartar

`FollowUpService.dismiss(follow_up_id, user_id)`:

- 409 si no está abierto.
- Lo cierra (`close_reason='dismissed'`, `closed_by`) y no abre otro. El par
  vuelve a tener seguimiento con su próxima venta.

### Lecturas

`get_follow_ups`, `get_follow_up_metrics` y `get_calendar_events` dejan de leer
la proyección y leen **seguimientos abiertos**:

- `days_until` y el estado (`overdue` / `urgent` / `upcoming` / `normal`) se
  calculan sobre `due_date`, con los mismos umbrales que hoy.
- Clientes Prioritarios (`7_days`): abiertos con `due_date <= hoy + 7`,
  vencidos incluidos, agrupados por cliente.
- `needs_estimate` (clientes con ciclos sin proyección) se sigue calculando
  desde `customer_product_cycles`, como hoy, sólo para el filtro `all`.

## 3. Espejo en Google Calendar

### Configuración

- Credenciales: la cuenta de servicio que ya usa Firebase
  (`FIREBASE_CREDENTIALS_PATH`), con scope
  `https://www.googleapis.com/auth/calendar.events`.
- Variable nueva `GOOGLE_CALENDAR_ID`. Sin ella el sincronizador no arranca y
  las filas se quedan `pending`: así corren desarrollo y tests, y al
  configurarla más tarde todo lo pendiente se sube en la pasada de arranque.
- Sin dependencias nuevas: `google.auth.transport.requests.AuthorizedSession`
  (`google-auth` y `requests` ya vienen con `firebase-admin`; se declaran
  explícitos en `requirements.txt`). API REST de Calendar v3, timeout de 5 s
  por llamada.

Configuración manual, una vez:

1. Habilitar la Google Calendar API en el proyecto de GCP de esa cuenta.
2. Crear el calendario (p. ej. "Seguimientos Revenew") y compartirlo con el
   email de la cuenta de servicio con permiso "Hacer cambios en eventos".
3. Poner su id en `GOOGLE_CALENDAR_ID` en Railway.

### El evento

- Día completo en `due_date` (`start.date` / `end.date = due_date + 1`).
- Título: `Seguimiento: {cliente} · {producto}`.
- Descripción: última compra (fecha y cantidad), intervalo promedio, email del
  cliente.
- `id = follow_up.id.hex` (32 caracteres `0-9a-f`, válidos en el alfabeto
  base32hex que Google acepta). El id determinista hace idempotentes todas las
  operaciones.

### Cliente

`GoogleCalendarClient` (`app/services/google_calendar.py`) con tres métodos:

| Método | Llamada | Se toma como éxito |
|---|---|---|
| `insert(event)` | `POST /calendars/{id}/events` | 2xx, 409 (ya existe) |
| `patch(event_id, fields)` | `PATCH /calendars/{id}/events/{event_id}` | 2xx |
| `delete(event_id)` | `DELETE /calendars/{id}/events/{event_id}` | 2xx, 404, 410 |

### Sincronización: despertar sólo cuando hay trabajo

`sync_pending_follow_ups(db, client)` toma las filas `pending` y `failed`, y
por cada una:

- **Abierto** → `insert`; si responde 409, `patch` con fecha, título y
  descripción.
- **Cerrado** → `delete`.
- Éxito → `synced`, `sync_error = NULL`. Error → `failed` con el mensaje.

Quién la ejecuta (`app/services/calendar_sync.py`):

- Un hilo daemon iniciado en el `lifespan` de FastAPI que espera sobre un
  `threading.Event`, con su propia sesión de base de datos.
- `request_calendar_sync()` sólo hace `event.set()`: no bloquea la request. La
  llaman `FollowUpService` después de cada commit que deja filas `pending` y
  `SaleService.create` después del suyo.
- Al despertar, sincroniza todo lo pendiente y vuelve a dormir **sin
  temporizador**. Sólo si quedaron filas `failed` duerme con
  `wait(timeout=900)` para reintentar cada 15 minutos hasta que no queden.
- Una pasada al arrancar cubre la carga inicial y lo que quedó de un reinicio.
- En reposo no hace ninguna consulta, así que Railway puede dormir el servicio.
- Hypercorn corre un solo worker; con más, los ids deterministas harían
  inofensivo el trabajo duplicado.
- En tests el hilo no se inicia: se llama `sync_pending_follow_ups` con un
  cliente falso.

Por qué no en la misma request: la llamada a Google no puede ser parte de la
transacción. Antes del commit deja eventos huérfanos si el commit falla;
después, un fallo de Google necesita reintento igual. Además sumaría hasta
~2 s a una venta de varios productos.

Límites aceptados: un cambio hecho a mano en Google se sobrescribe con el
siguiente cambio del seguimiento; un error permanente (calendario sin
compartir) se reintenta cada 15 minutos y queda visible en `sync_error`.

## 4. Migración

Una revisión de Alembic:

- Crea `follow_ups` con sus CHECK e índice único parcial.
- Elimina `customer_product_cycles.calendar_event_id`.
- Carga inicial: por cada ciclo con `estimated_next_purchase`, un seguimiento
  `open` con esa fecha. Si la fecha es anterior a `business_today()` al migrar,
  `sync_status = 'skipped'` (la app lo muestra vencido; Google no se llena de
  eventos en el pasado). Si no, `pending`.
- Downgrade: borra la tabla y restaura la columna (no tenía datos).

## 5. API

Los esquemas existentes crecen; ninguno cambia de forma.

- `FollowUpItemResponse` agrega `follow_up_id` y `due_date`. El front debe
  mostrar `due_date` como "Próxima compra" en lugar de
  `estimated_next_purchase`.
- `CalendarEvent` agrega `followUpId` (camelCase, como el resto del esquema).
- Pasan a leer seguimientos abiertos: `GET /follow-ups`,
  `GET /follow-ups/metrics`, `priorityCustomers` de `GET /dashboard/summary`,
  `GET /calendar/events`.
- Nuevos, en `app/api/v1/endpoints/follow_ups.py`:
  - `POST /follow-ups/{id}/postpone`, body `{ "date": "YYYY-MM-DD" }` → 200
    con el seguimiento nuevo; 409 si ya estaba cerrado; 422 si la fecha no es
    posterior a hoy; 404 si no existe.
  - `POST /follow-ups/{id}/dismiss` → 204; 409 si ya estaba cerrado; 404 si no
    existe.
- Se actualizan `API_CONTRACT.md` y `types.ts`.

## 6. Pruebas

Contra Postgres:

- Venta nueva cierra con `sale` y abre el siguiente con la proyección nueva.
- Venta con fecha `<=` a la última compra no toca seguimientos (los dos casos
  de Aurita: atrasada y mismo día).
- Producto sin proyección: la venta no abre nada.
- Primera venta de un par: no hay seguimiento que cerrar ni proyección; no abre nada.
- Posponer abre uno en la fecha elegida; rechaza fecha no futura y seguimiento cerrado.
- Descartar no abre otro; la próxima venta sí.
- El índice único impide dos abiertos del mismo par.
- Prioritarios y calendario excluyen cerrados y respetan la fecha pospuesta.
- La carga inicial crea uno por ciclo con fecha; los vencidos quedan `skipped`.
- Cada escritura deja `sync_status = 'pending'` y llama a `request_calendar_sync()`.

Sincronización, con un `GoogleCalendarClient` falso:

- `pending` → `synced`; error → `failed` con `sync_error`; reintento → `synced`.
- Cerrado → `delete`; 409 en insert pasa a `patch`; 404/410 en delete es éxito.
- `skipped` nunca se envía.
- El hilo duerme sin temporizador si no hay `failed` y con 900 s si los hay.
