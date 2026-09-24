# Desarrollo local

## Por que hay una base local

Hasta el 2026-09-24, el `.env` del proyecto apuntaba a Railway: desarrollar
localmente escribia en la base que tiene los unicos registros del negocio.
Funcionaba mientras el unico que escribia eras vos, a mano.

La pieza 2 del agente cambia eso. El que va a decidir que escribir es un LLM,
con prompts a medio ajustar y herramientas que todavia no hacen del todo lo
que dicen. Eso no puede correr contra produccion.

## Las tres bases

| | Puerto | Persistencia | Para que |
|---|---|---|---|
| `docker-compose.dev.yml` | 55433 | volumen nombrado | desarrollo |
| `docker-compose.test.yml` | 55432 | tmpfs (se borra) | tests |
| Railway | — | — | produccion |

Las dos locales corren **Postgres 17**, igual que produccion. Estaban en 16
hasta que se monto esto; validar contra una version mayor distinta a la que
sirve es la clase de deriva que aparece tarde y cara.

## El schema local se llama `db_local`

No `db_v2`. Es deliberado y es el unico guard que no hay que mantener: si una
conexion mal dirigida llega a Railway con la configuracion local, `db_local`
no existe alla y la conexion falla, en vez de escribir sobre datos reales.

## Arranque

```bash
cp .env.example .env          # si todavia no tenes uno
docker compose -f docker-compose.dev.yml up -d
venv/bin/alembic upgrade head # crea db_local desde cero
```

`venv/bin/pip` esta roto en este venv (resuelve a un Python 3.14 vacio).
Usa siempre `venv/bin/python -m pip`.

## Tests

```bash
docker compose -f docker-compose.test.yml up -d
venv/bin/python -m pytest -q
```

Los tests no leen `.env`: usan `TEST_DATABASE_URL`, con default a la base
efimera del puerto 55432. `tests/conftest.py` aborta si esa URL nombra un
host de Railway, porque los tests crean y destruyen schemas.

## Comandos contra produccion

Las credenciales de Railway viven en `.env.production`, gitignoreado y
**no cargado por defecto**. Se cargan por comando, a proposito:

```bash
set -a && . ./.env.production && set +a
venv/bin/python -m scripts.backfill_projections --dry-run
```

Que haya que escribir esa linea es la proteccion. Antes era lo que pasaba
solo.

`db_dev` sigue siendo el respaldo congelado de antes del cutover.
`alembic/env.py`, `scripts/copy_schema_data.py` y
`scripts/backfill_projections.py` se niegan a tocarlo sin
`REVENEW_ALLOW_DB_DEV=1`. No hay motivo para setear esa variable.

## Sembrar la base local con datos

El schema arranca vacio. Sin historial, las skills del agente que dependen de
el —proyeccion, precio habitual, FIFO— no se pueden probar de verdad.

Para copiar el estado de produccion (son tus datos, en tu maquina):

```bash
set -a && . ./.env.production && set +a
docker run --rm -e PGPASSWORD="$POSTGRES_PASSWORD" postgres:17-alpine \
  pg_dump -h "$POSTGRES_SERVER" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" -n db_v2 --no-owner --no-privileges > /tmp/db_v2.sql

docker exec -i revenew-dev-db psql -U revenew -d revenew_dev -c \
  "DROP SCHEMA IF EXISTS db_local CASCADE;"
docker exec -i revenew-dev-db psql -U revenew -d revenew_dev < /tmp/db_v2.sql
docker exec revenew-dev-db psql -U revenew -d revenew_dev -c \
  "ALTER SCHEMA db_v2 RENAME TO db_local;"
rm /tmp/db_v2.sql
```

El volcado lleva nombres y correos de clientes reales a un archivo en tu
disco. Borralo cuando termines, como hace la ultima linea.
