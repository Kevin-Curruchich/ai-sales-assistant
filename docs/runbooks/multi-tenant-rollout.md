# Rollout multi-comercio (RLS) en Railway

Runbook manual. Cada paso dice qué correr, qué significa que salió bien, y qué
hacer si no. En orden, verificando entre pasos. No lo automatices.

En los ejemplos el usuario de la API se llama `revenew_api` y el schema del
negocio es `db_v2`; reemplazalos si usás otros nombres.

## Qué cambia y por qué

Desde este cambio la API **no arranca** si su rol de Postgres es superusuario
o tiene `BYPASSRLS` (`app/main.py::assert_rls_enforced`): con ese rol RLS no
aplica y un comercio vería los datos de otro, sin ningún error. El arranque
falla con `RuntimeError: La API no puede conectarse como superusuario ni con
BYPASSRLS`. Por eso hacen falta dos usuarios:

| Variable (servicio de la API en Railway) | Usuario | La usa |
|---|---|---|
| `MIGRATIONS_DATABASE_URL` | `postgres` (dueño de las tablas) | Solo `alembic upgrade head` en `entrypoint.sh` (lo lee `alembic/env.py`). |
| `DATABASE_URL` (o `PGHOST`/`PGUSER`/... o `POSTGRES_*`, en ese orden de prioridad) | `revenew_api`, miembro de `app_tenant` | La API, el worker del calendario y el checkpointer del agente. |

Si `MIGRATIONS_DATABASE_URL` no está definida, Alembic cae a `DATABASE_URL`,
y con `revenew_api` la migración falla (no puede crear roles ni tablas). Las
dos tienen que estar definidas.

Requisitos que no son obvios:

- **El rol de las migraciones necesita `CREATEROLE`**: la migración
  `b4e1d7a2c9f0` crea el rol `app_tenant`. `postgres` en Railway es
  superusuario, así que lo tiene.
- **Todas las migraciones futuras tienen que correr como ese mismo rol
  (`postgres`).** La migración hace `ALTER DEFAULT PRIVILEGES ... TO
  app_tenant`, y eso solo cubre las tablas que cree *el rol que corrió la
  migración*. Una tabla creada por otro rol nace sin GRANTs para `app_tenant`
  y la API recibe `permission denied` sobre ella.
- **`revenew_api` tiene que ser dueño del schema `agent` y de todas sus
  tablas.** `CREATE` sobre el schema no alcanza: el checkpointer de LangGraph
  (`AsyncPostgresSaver.setup()`) y `agent.tool_writes`
  (`app/agent/idempotency.py`) crean y *alteran* tablas ahí en tiempo de
  ejecución, y alterar una tabla exige ser su dueño. Sin esto el agente
  arranca degradado (`agent_graph_init_failed` en `/health`). No hace falta
  `CREATE` sobre la base: la app solo crea el schema si no existe.

**Precondición: nadie usando la app durante el paso 3.** El código viejo no
fija `app.business_id`; contra el schema ya migrado sus inserts fallan por
`business_id` NOT NULL.

## 1. Crear el usuario de la API

Conectado como `postgres` (`railway connect Postgres`, o `psql` con la URL
pública):

```sql
-- La migración también lo crea con IF NOT EXISTS y los mismos atributos;
-- crearlo antes permite que revenew_api exista antes de migrar.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_tenant') THEN
    CREATE ROLE app_tenant NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

CREATE ROLE revenew_api LOGIN PASSWORD '<generada, larga>'
  NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB IN ROLE app_tenant;
```

**Salió bien si:**

```sql
SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'revenew_api';
-- f | f
SELECT pg_has_role('revenew_api', 'app_tenant', 'MEMBER');
-- t
```

**Si falla:** `role "revenew_api" already exists` → alguien ya lo creó; revisá
que la consulta de arriba dé `f | f` y `t` y seguí.

## 2. Darle el schema `agent`

Si el schema todavía no existe:

```sql
CREATE SCHEMA IF NOT EXISTS agent AUTHORIZATION revenew_api;
```

Si ya existe (lo creó la API corriendo como `postgres`), pasale el schema y
sus tablas:

```sql
ALTER SCHEMA agent OWNER TO revenew_api;
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.relname, c.relkind FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'agent' AND c.relkind IN ('r', 'p', 'S', 'v')
  LOOP
    EXECUTE format(
      'ALTER %s agent.%I OWNER TO revenew_api',
      CASE r.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' ELSE 'TABLE' END,
      r.relname);
  END LOOP;
END $$;
```

(Una secuencia ligada a una columna cambia de dueño con su tabla; si el
`ALTER SEQUENCE` falla por eso, es inofensivo: ya quedó.)

**Salió bien si:**

```sql
SELECT nspowner::regrole FROM pg_namespace WHERE nspname = 'agent';  -- revenew_api
SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'agent';  -- todas revenew_api
```

## 3. Migrar como `postgres` y pasar la API a `revenew_api` (un solo deploy)

En el servicio de la API en Railway, en un mismo cambio de variables:

1. `MIGRATIONS_DATABASE_URL` = la URL actual con `postgres`
   (el valor que hoy tiene `DATABASE_URL`, p. ej. `${{Postgres.DATABASE_URL}}`).
2. `DATABASE_URL` = la misma URL con `revenew_api` y su contraseña:
   `postgresql://revenew_api:<password>@<host>:<port>/<db>`.
   `DATABASE_URL` gana sobre `PG*` y `POSTGRES_*`, así que no hace falta
   tocarlas.
3. `POSTGRES_SCHEMA` sin cambios (`db_v2`).

Desplegá el commit de este cambio con esas variables. `entrypoint.sh` corre
`alembic upgrade head` con `MIGRATIONS_DATABASE_URL` (crea `businesses`,
agrega `business_id`, activa RLS y da los GRANTs a `app_tenant`) y después
arranca la API con `DATABASE_URL`.

**Salió bien si:** el deploy queda verde y en el log aparece la migración
`b4e1d7a2c9f0` sin errores.

**Si falla:**

- `RuntimeError: La API no puede conectarse como superusuario ni con
  BYPASSRLS` → `DATABASE_URL` sigue apuntando a `postgres`. Corregila.
- `permission denied for schema ...` / `must be owner` durante la migración →
  `MIGRATIONS_DATABASE_URL` falta o no es `postgres`.
- `permission denied for table ...` en las requests → la tabla no la creó
  `postgres` (ver "Requisitos"); corré como `postgres`
  `GRANT SELECT, INSERT, UPDATE, DELETE ON <tabla> TO app_tenant`.

**El deploy anterior NO es un respaldo seguro.** Si la migración corrió pero
el deploy nuevo no queda sano (guard de RLS, permisos, `/health`), Railway
sigue sirviendo el build anterior al multi-comercio, y ese build no fija
`app.business_id`: **toda escritura falla** (`business_id` NOT NULL) hasta
que un deploy nuevo quede sano. Las lecturas siguen andando. Es una caída de
escrituras: corregí la variable o el permiso que indica el log y redeployá
de inmediato. Si la migración misma falla, `set -e` corta el arranque y la
transacción se revierte; ahí el build anterior sí sigue funcionando.

## 4. Verificar

1. `GET /health` → `{"status": "ok"}`. `degraded` con
   `agent_graph_init_failed` → revisá el paso 2.
2. Entrá con un usuario del comercio principal: se ven sus clientes, ventas
   y productos de siempre, y el agente responde.
3. Como `revenew_api`, sin fijar comercio, la base no devuelve nada (falla
   cerrado):

   ```sql
   SELECT count(*) FROM db_v2.customers;  -- 0
   SELECT set_config('app.business_id', (SELECT id::text FROM db_v2.businesses LIMIT 1), false);
   SELECT count(*) FROM db_v2.customers;  -- el total de siempre
   ```

## 5. Rollback

RLS no aplica a `postgres` (superusuario), y la app ya fija el comercio en
cada request, así que con un solo comercio funciona igual como `postgres`.
**Pero el guard de este cambio no la deja arrancar así**: volver solo las
variables deja la API caída.

1. Volvé `DATABASE_URL` a la URL de `postgres`.
2. Redeployá un revert de este commit ("Refuse to start without RLS..."), o
   el commit anterior a él, que ya trae el código multi-comercio pero no el
   guard.

No redeployes un deploy anterior al multi-comercio: su código no fija
`app.business_id` y no puede escribir en el schema migrado. Deshacer la
migración (`alembic downgrade`) solo si se abandona el multi-comercio
entero, y siempre con `MIGRATIONS_DATABASE_URL` como `postgres`.
