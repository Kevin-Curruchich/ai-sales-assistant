# Multi-tenant con Row-Level Security

Fecha: 2026-10-26

## Objetivo

Que Revenew sirva a varios comercios reales en la misma base y el mismo
despliegue, con aislamiento fuerte: un comercio nunca puede leer ni escribir
datos de otro, aunque el codigo de la app olvide un filtro. Los datos son
permanentes (no es un modo demo).

Exito:

- Un dueno se registra, se crea su comercio vacio y queda como `admin`.
- El `admin` agrega vendedores (`sales_rep`, `viewer`) a su comercio.
- Los datos actuales quedan en un primer comercio sin perder nada.
- Un test con dos comercios demuestra que A no ve, edita ni borra nada de B en
  ningun recurso, incluido el agente.

## Decisiones

| Tema | Decision |
|------|----------|
| Modelo de tenancy | Tablas compartidas + `business_id` + RLS de Postgres (no schema por comercio, no BD por comercio) |
| Usuarios ↔ comercio | Un comercio tiene N usuarios; un usuario pertenece a un solo comercio con un rol |
| Fuente de verdad del comercio | `users.business_id` en la BD. Firebase solo da identidad; sin claim de comercio |
| Alta de vendedores | El `admin` crea la cuenta sin contrasena; el vendedor la define con un enlace de Firebase. Sin tabla de invitaciones ni servicio de correo propio |
| Zona horaria | Por comercio, leida de `businesses.timezone` en cada llamada |

Fuera de alcance: facturacion, limites por plan, borrar un comercio,
invitaciones por link, un usuario en varios comercios.

## 1. Modelo de datos

### Tabla nueva `businesses` (sin RLS)

| Columna | Tipo | Notas |
|---------|------|-------|
| `id` | uuid PK | |
| `name` | varchar(255) NOT NULL | |
| `timezone` | varchar(64) NOT NULL | default `America/Guatemala`; validado como zona IANA |
| `google_calendar_id` | varchar(255) NULL | NULL = no se sincroniza |
| `created_at` | timestamptz | |

### `users` (sin RLS)

- Nueva columna `business_id` uuid NOT NULL, FK a `businesses.id`.
- `email` sigue unico global (Firebase lo exige; un usuario = un comercio).
- No lleva RLS porque se consulta antes de conocer el comercio. Todo listado o
  busqueda de usuarios por id filtra por `business_id` en `UserService`.

### Tablas con `business_id` + RLS (11)

`customers`, `products`, `purchases`, `purchase_items`, `sales`, `sale_items`,
`sale_item_lot_allocations`, `cash_movements`, `follow_ups`,
`customer_product_cycles`, `agent_threads`.

Las tablas hijas tambien llevan la columna (no se hereda por JOIN), para que la
politica sea identica en todas.

En cada una:

```sql
business_id uuid NOT NULL
  DEFAULT current_setting('app.business_id')::uuid
  REFERENCES businesses(id);

ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON <t>
  USING      (business_id = current_setting('app.business_id', true)::uuid)
  WITH CHECK (business_id = current_setting('app.business_id', true)::uuid);
```

Sin variable fijada, `current_setting(..., true)` es NULL: las lecturas
devuelven 0 filas y los inserts fallan (falla cerrado). El DEFAULT hace que
los repositorios no tengan que pasar `business_id` al crear.

En los modelos SQLAlchemy la columna se declara con
`server_default=text("current_setting('app.business_id')::uuid")` para que el
autogenerate de Alembic no detecte deriva.

### Uniques que pasan a ser por comercio

- `products.sku` → `UNIQUE (business_id, sku)`
- `purchases.reference_number` → `UNIQUE (business_id, reference_number)`
- `customer_product_cycles` `uq_customer_product` y `follow_ups`
  `uq_follow_ups_open_pair` ya incluyen ids que son de un solo comercio; no
  cambian.

### Roles de Postgres

- `postgres` (dueno/superusuario): solo Alembic y scripts de operacion. Se
  salta RLS siempre.
- `app_user` (nuevo, `NOSUPERUSER NOBYPASSRLS`, no dueno de tablas): la API y
  el worker. `GRANT SELECT, INSERT, UPDATE, DELETE` sobre las tablas del schema
  de negocio y `USAGE` sobre sus secuencias; los mismos permisos sobre el schema
  `agent` del checkpointer.
- Config: nuevas variables `POSTGRES_APP_USER` / `POSTGRES_APP_PASSWORD` para
  la API; Alembic sigue usando `POSTGRES_USER`.

### Migracion (una sola revision, sin perdida de datos)

1. Crear `businesses` e insertar el comercio actual (nombre y zona desde
   `BUSINESS_TIMEZONE` y `GOOGLE_CALENDAR_ID` actuales).
2. Agregar `users.business_id` y las 11 columnas como NULL, rellenar con el id
   del comercio actual, pasar a NOT NULL y poner el DEFAULT.
3. Reemplazar los uniques de `products.sku` y `purchases.reference_number`.
4. Crear el rol `app_user` (si no existe) y los GRANTs; activar RLS + FORCE y
   crear las politicas.

`downgrade` revierte en orden inverso.

## 2. Del request al comercio

1. `get_current_user` verifica el token y carga `User` por uid (sin RLS).
2. Si no existe: **403** "Tu cuenta no pertenece a ningun comercio". Se elimina
   la creacion automatica de `get_or_create_from_firebase`.
3. Guarda `db.info["business_id"]` y `db.info["business_tz"]` (cargado de
   `businesses.timezone`), y ejecuta
   `select set_config('app.business_id', :id, true)` en la transaccion abierta.
4. Un listener `after_begin` sobre `SessionLocal` (en `app/core/database.py`)
   vuelve a fijar la variable desde `session.info` al iniciar cada
   transaccion, porque `set_config(..., true)` se pierde en cada `commit()`.
5. Repositorios y servicios de negocio no cambian.

### Endpoints de usuarios

- `POST /auth/signup` (publico): `business_name`, `email`, `password`,
  `display_name`. Ya no acepta `role`. Crea comercio + usuario `admin` en una
  transaccion; si falla la BD despues de crear el usuario en Firebase, borra
  el usuario de Firebase.
- `POST /auth/users` (solo `admin`): `email`, `display_name`, `role` en
  {`sales_rep`, `viewer`}; sin `password`. Crea el usuario en Firebase sin
  contrasena (`create_firebase_user` con `password` opcional) y su fila en el
  comercio del admin, con el mismo rollback de Firebase que el signup.
  Responde el usuario mas `password_setup_link`, generado con
  `auth.generate_password_reset_link(email)` del Admin SDK.
- `POST /auth/users/{id}/password-link` (solo `admin`, mismo comercio): genera
  un enlace nuevo, para reenviar cuando el anterior caduco (1 h por defecto).
- El correo lo envia el frontend con `sendPasswordResetEmail(auth, email)` del
  SDK cliente, usando la plantilla de Firebase (traducida en la consola). El
  admin tambien ve el enlace para copiarlo y mandarlo por otro canal. El
  backend no envia correos.
- `GET /auth/users`, `GET/PUT /auth/users/{id}`, `PATCH /auth/users/{id}/role`:
  filtran por el `business_id` del admin; un usuario de otro comercio responde
  **404**.
- Arreglo de paso: `update_role` valida contra {`admin`, `sales_rep`,
  `viewer`} (hoy dice `manager` en vez de `sales_rep`).

## 3. Procesos fuera del request

### Agente

- El endpoint agrega `business_id` (de `current_user`) a `configurable`, junto
  al `user_id`.
- `agent_session()` lo copia a `db.info`; si falta, lanza error (misma regla
  que `user_id_from_config`).
- Los checkpoints de LangGraph (schema `agent`) no llevan RLS. Quedan
  protegidos porque todo endpoint de threads carga primero la fila de
  `agent_threads` (con RLS) y responde 404 si es ajena. La implementacion
  verifica que `/threads/*` y `/stream` lo hagan antes de tocar el grafo.

### Worker de Google Calendar

- Se arranca si hay credenciales de cuenta de servicio (ya no depende de
  `GOOGLE_CALENDAR_ID` global).
- Cada pasada lista `businesses` con `google_calendar_id` no nulo; por cada uno
  abre sesion con `db.info["business_id"]`, crea el cliente para ese
  calendario y llama a `sync_pending_follow_ups` sin cambios.
- Un comercio que falla cuenta como falla (reintento a los 15 min) pero no
  detiene a los demas.
- `GOOGLE_CALENDAR_ID` deja de leerse en runtime; solo lo usa la migracion.

### Zona horaria por comercio

- `business_tz`, `business_today` y `business_midnight`
  (`app/core/datetime_utils.py`) reciben la sesion: `business_tz(db)` devuelve
  `ZoneInfo(db.info["business_tz"])`; si falta, lanza error (no cae a la zona
  global).
- Se actualizan las llamadas en: `app/repositories/sale_repository.py`,
  `app/agent/prompt_runtime.py`, `app/agent/tools/read.py`,
  `app/agent/tools/write.py`, `app/api/v1/endpoints/cash.py`,
  `app/services/product_service.py`, `app/services/follow_up_service.py`,
  `app/services/sales/orchestrator.py`.
- `agent_session()` y el worker tambien llenan `db.info["business_tz"]`.
- `BUSINESS_TIMEZONE` deja de leerse en runtime; solo lo usa la migracion.

### Scripts

`backfill_projections`, `fix_historical_cost_basis`: reciben `--business-id`
o, sin el, recorren todos los comercios fijando `app.business_id` y
`business_tz` en cada uno (los inserts dependen del DEFAULT).
`copy_schema_data`: copia `businesses` antes que el resto.

## 4. Pruebas

- `conftest` conecta la app como un rol sin superusuario; con superusuario los
  tests de aislamiento pasarian siempre.
- Fixtures con dos comercios, A y B, cada uno con datos en las 11 tablas.
- Test de aislamiento: autenticado como A, cada endpoint de lectura no
  devuelve nada de B, y cada `GET/PUT/PATCH/DELETE` por id de B responde 404.
- La variable sobrevive a un `commit()` dentro del mismo request.
- Sesion sin comercio: 0 filas en lectura y error en insert.
- Agente: thread de B desde A responde 404; herramientas de escritura crean
  filas en A.
- Usuarios: signup crea comercio + admin; admin no ve ni edita usuarios de
  otro comercio; usuario de Firebase sin fila responde 403.
- Alta de vendedor: crea usuario de Firebase sin contrasena, devuelve
  `password_setup_link` (Firebase mockeado), y si falla la BD se borra el
  usuario de Firebase; `password-link` sobre un usuario de otro comercio
  responde 404.
- Zona horaria: dos comercios con zonas distintas obtienen `business_today`
  distinto a la misma hora UTC.
- Worker: recorre solo comercios con calendario y una falla no frena al resto.
- Migracion: los datos existentes quedan en el primer comercio y siguen
  visibles.

## Riesgos

- **Conexion como superusuario en produccion** anula todo el aislamiento sin
  dar error. La API valida al arrancar que su rol no sea superusuario ni tenga
  `BYPASSRLS`, y si lo es no arranca.
- **Consultas sin sesion SQLAlchemy** (conexiones crudas) no pasan por
  `after_begin`; fallan cerrado (0 filas), no filtran de mas.
- **Frontend** (`revenew`): necesita pantalla de signup con nombre de comercio
  y gestion de usuarios para el admin (alta, enlace copiable, boton
  "reenviar", llamada a `sendPasswordResetEmail`). Va en un cambio aparte.
- **El enlace de contrasena es una credencial**: quien lo tenga puede tomar la
  cuenta mientras no caduque. Solo se devuelve al `admin` del mismo comercio y
  nunca se escribe en logs.
