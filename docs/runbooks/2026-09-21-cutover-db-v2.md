# Cutover a db_v2

Runbook manual. Cada paso dice qué correr, qué significa que salió bien, y qué
hacer si no. Ejecutalo en orden, uno por uno, verificando entre cada paso
antes de seguir al siguiente. No lo automatices.

**Precondición: nadie usando la app mientras corren los pasos 3 a 8.** No
solo mientras corre la copia (pasos 3-6): la app en producción sigue
sirviendo desde `db_dev` hasta que el paso 8 corta el tráfico a `db_v2`, y
el paso 7 es de duración abierta. Una venta que entra a `db_dev` en
cualquier momento entre el paso 3 y el corte del paso 8 queda fuera de
`db_v2` sin ningún aviso — nada en este runbook detecta esa pérdida por sí
solo, por eso el paso 8 empieza con un re-censo obligatorio.

**Evidencia previa.** El 2026-09-21 se corrió una auditoría de solo lectura
contra `db_dev`: 85 columnas comparadas contra los modelos, cero columnas
`double precision`, cero columnas de más, cero columnas faltantes, cero
desajustes de precisión o nulabilidad. Es decir, no hay motivo para esperar
sorpresas en el paso 2. Aun así, el paso 2 se repite igual: la base pudo
cambiar entre esa auditoría y este cutover, y este runbook no confía en una
foto vieja.

**Sobre el guard de `db_dev`.** `alembic/env.py` se niega a migrar el
schema `db_dev` a menos que la variable de entorno `REVENEW_ALLOW_DB_DEV=1`
esté seteada. `scripts/copy_schema_data.py` tiene el mismo guard, pero solo
sobre su **destino** (`--target`): pasarle `db_dev` como `--source` — que
es justo lo que hacen los pasos 3 y 4 — no lo activa, y no debería
activarlo. Nada de lo que sigue en este runbook setea esa variable. Si
algún paso falla citándola, **no la setees**: es la señal de que algo
(típicamente `POSTGRES_SCHEMA`, o un `--target` mal escrito) apunta a
`db_dev` cuando debería apuntar a `db_v2`. Revisá eso primero.

## 0. Censo previo (antes de tocar nada)

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings
e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
with e.connect() as c:
    for t in ["users","customers","products","purchases","purchase_items",
              "sales","sale_items","sale_item_lot_allocations","customer_product_cycles"]:
        print(f"{t:<32}{c.execute(text(f'SELECT count(*) FROM db_dev.{t}')).scalar():>6}")
    print("SUM(sales.total) =", c.execute(text("SELECT sum(total) FROM db_dev.sales")).scalar())
PY
```

**Éxito:** `users 2, customers 21, products 2, purchases 38,
purchase_items 38, sales 156, sale_items 156, sale_item_lot_allocations 155,
customer_product_cycles 26`, `SUM(sales.total) = 9954.49`.

**Si no coinciden:** parate ahí. Alguien escribió en `db_dev` desde el
2026-09-21. No sigas con un número que no reconocés — actualizá el número
esperado solo si podés explicar la diferencia (por ejemplo, ventas nuevas
legítimas), y si no podés, andá a buscar a quién antes de tocar nada más.

**Si el script no corre** (error de conexión, credenciales, import — no un
número raro sino una excepción antes de imprimir nada): es un problema de
entorno, no de datos. Revisá `.env` y la conectividad a la base antes de
asumir nada sobre `db_dev`. No sigas hasta que el censo corra limpio de
punta a punta.

Resultado real obtenido: ____________________________________________

## 1. Crear db_v2 y migrarlo a la revisión inicial

```bash
POSTGRES_SCHEMA=db_v2 venv/bin/alembic upgrade 7c8a87a5384a
```

Esto deja `db_v2` en el mismo esquema físico que `db_dev` tiene hoy (nueve
tablas, sin los campos de pago ni `cash_movements` todavía — esos llegan en
el paso 6, después de copiar los datos).

**Éxito:** el comando termina sin error. Verificá con:

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings
e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
with e.connect() as c:
    tables = [r[0] for r in c.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='db_v2' ORDER BY table_name"
    ))]
    print(tables)
    for t in tables:
        if t == "alembic_version":
            continue
        n = c.execute(text(f'SELECT count(*) FROM db_v2."{t}"')).scalar()
        print(f"  {t:<32}{n:>6}")
PY
```

Deben aparecer las nueve tablas de negocio más `alembic_version`, y cada
tabla de negocio en `0` filas. El nombre de la tabla solo en la lista no
alcanza como prueba de "vacía" — por eso el conteo va abajo, no solo el
listado.

**Si falla citando `db_dev` y el guard `REVENEW_ALLOW_DB_DEV`:** no setees
esa variable. Revisá `POSTGRES_SCHEMA` en el entorno desde el que corriste
el comando — probablemente no se exportó y Alembic cayó al default
(`db_dev`). Corregilo y repetí el paso.

**Si falla por cualquier otro motivo:** no sigas. `db_v2` puede haber
quedado a medio crear; investigá el error antes de reintentar.

Resultado real obtenido: ____________________________________________

## 2. Diff de esquemas antes de copiar

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings
from scripts.copy_schema_data import TABLE_ORDER
e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
q = ("SELECT column_name, data_type, udt_name, numeric_precision, "
     "numeric_scale, character_maximum_length FROM information_schema.columns "
     "WHERE table_schema=:s AND table_name=:t ORDER BY column_name")
with e.connect() as c:
    for t in TABLE_ORDER:
        a = {r[0]: r[1:] for r in c.execute(text(q), {"s":"db_dev","t":t})}
        b = {r[0]: r[1:] for r in c.execute(text(q), {"s":"db_v2","t":t})}
        if a != b:
            print(f"--- {t}")
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    print(f"    {k}: db_dev={a.get(k)}  db_v2={b.get(k)}")
PY
```

Nota: esta consulta trae `udt_name`, `numeric_precision`, `numeric_scale` y
`character_maximum_length` además de `data_type`. Las tres últimas están
porque `numeric(10,2)` y `numeric(10,4)` imprimen igual (`numeric`) si solo
mirás el tipo — justo la clase de diferencia que puede redondear plata en
silencio. `udt_name` está porque este proyecto tiene un enum nativo
(`earning_mode_enum`): dos enums nativos distintos reportan
`data_type='USER-DEFINED'` con `numeric_precision`, `numeric_scale` y
`character_maximum_length` en `NULL` los dos, así que sin `udt_name` esta
consulta podía dar "sin salida" y aun así dejar pasar un tipo de enum
distinto entre `db_dev` y `db_v2` — que el script de copia sí detecta y
aborta.

**Éxito:** sin salida. Eso confirma, contra el estado real de hoy, lo mismo
que ya mostró la auditoría del 2026-09-21.

**Si hay salida:** no sigas al paso 3. Cada línea impresa es una columna
que difiere entre `db_dev` y `db_v2` (falta de un lado, tipo distinto, o
precisión/escala distinta). Resolvé el drift — típicamente ajustando la
revisión inicial o investigando qué cambió en `db_dev` desde el 2026-09-21
— antes de copiar nada.

Resultado real obtenido: ____________________________________________

## 3. Copia en seco

```bash
venv/bin/python -m scripts.copy_schema_data --source db_dev --target db_v2 --dry-run
```

Nota: `copy_schema_data.py` tiene tests de sus funciones internas, pero
nadie corrió nunca su `main()` — el camino de línea de comandos en sí no
tiene cobertura. Este dry-run es, en la práctica, su primera prueba de humo
como CLI. Si falla acá con algo que huele a problema de argparse, de import,
o de conexión (y no a un problema de datos), es plausible que sea un
tropiezo de arranque del script y no un problema con `db_dev` — revisalo con
ese criterio antes de asumir que los datos están mal.

**Éxito:** imprime una tabla con el conteo de filas por tabla, un `TOTAL`, y
termina con `(dry-run: no se escribio nada)`. Los conteos deben coincidir
con el censo del paso 0 (`cash_movements` no aparece en esta lista: nace
vacía y el script no la mueve — ver más abajo).

**Si aborta con `SchemaMismatch` sobre "tablas que TABLE_ORDER no conoce":**
el script encontró en `db_dev` una tabla de la que no sabe nada. Si es
`cash_movements` y todavía tiene cero filas, esto no debería pasar (está
exenta mientras esté vacía); si aparece igual, o es otra tabla, algo nuevo
se creó en `db_dev` después de que se escribió este runbook. No sigas:
actualizá `TABLE_ORDER` o confirmá explícitamente que la tabla nueva nace
vacía antes de reintentar.

**Si aborta con `SchemaMismatch` sobre columnas con "tipos distintos":** el
script está comparando tipo, `udt_name`, precisión y escala entre `db_dev`
y `db_v2` para esa columna, no solo el nombre — el paso 2 (ya con
`udt_name`, precisión y escala incluidos) debería haber mostrado esto
mismo. Si llegaste hasta acá de todos modos, es que algo cambió en
`db_dev` entre el paso 2 y este, o que saltaste el paso 2. No copies con
ese mensaje en pantalla: los datos podrían truncarse o redondearse en
silencio.

**Si aborta citando `db_dev` y `REVENEW_ALLOW_DB_DEV`:** eso solo puede
pasar si `--target` terminó apuntando a `db_dev` — revisá que no invertiste
`--source` y `--target` al escribirlos. `--source db_dev` en sí es
esperado y no dispara este guard. No setees la variable.

Resultado real obtenido: ____________________________________________

## 4. Copia real

```bash
venv/bin/python -m scripts.copy_schema_data --source db_dev --target db_v2
```

**Éxito:** misma salida que el paso 3, sin la línea de `(dry-run...)`. Si el
paso 3 salió limpio, este paso no debería sorprender — corre exactamente la
misma lógica, ahora con commit real.

Confirmá además, ahora mismo, que el origen no se tocó — repetí el censo
del paso 0 contra `db_dev` (no `db_v2`):

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings
e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
with e.connect() as c:
    for t in ["users","customers","products","purchases","purchase_items",
              "sales","sale_items","sale_item_lot_allocations","customer_product_cycles"]:
        print(f"{t:<32}{c.execute(text(f'SELECT count(*) FROM db_dev.{t}')).scalar():>6}")
    print("SUM(sales.total) =", c.execute(text("SELECT sum(total) FROM db_dev.sales")).scalar())
PY
```

Tiene que dar exactamente lo mismo que el paso 0. El script nunca escribe
en el origen, pero esto lo prueba en vez de asumirlo.

**Si falla la copia:** el script corre todo en una sola transacción (o se
copia entero, o no se copia nada), así que una falla acá no deja `db_v2` a
medias. `db_dev` tampoco se toca en ningún caso: el script nunca escribe en
el origen. Resolvé la causa del error (mismos motivos que en el paso 3) y
repetí desde el paso 3 en seco antes de reintentar la copia real.

**Si el re-censo de `db_dev` no coincide con el paso 0:** parate y
avisá — algo escribió en el origen mientras corría esto, lo cual no
debería ser posible dado que la copia solo lee de `db_dev`. No confíes en
el resto del runbook hasta entender qué pasó.

Resultado real obtenido (db_dev, re-censo): ________________________________
Resultado real obtenido (copia): ___________________________________________

## 5. Verificar conteos en db_v2

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings

TABLES = ["users", "customers", "products", "purchases", "purchase_items",
          "sales", "sale_items", "sale_item_lot_allocations",
          "customer_product_cycles"]

e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
with e.connect() as c:
    for schema in ("db_dev", "db_v2"):
        print(f"--- {schema} ---")
        for t in TABLES:
            n = c.execute(text(f'SELECT count(*) FROM "{schema}".{t}')).scalar()
            print(f"{t:<32}{n:>6}")
        total = c.execute(text(f'SELECT sum(total) FROM "{schema}".sales')).scalar()
        print("SUM(sales.total) =", total)
PY
```

(`cash_movements` no se verifica acá: en `db_v2` esa tabla todavía no
existe a esta altura — la crea la migración `8df5b1f79497`, que recién se
aplica en el paso 6. Verificarla ahora daría `UndefinedTable`, siempre, sin
que eso signifique nada sobre la copia. Se verifica en el paso 6.)

**Éxito:** el bloque `db_dev` da exactamente los mismos números que el
paso 0 (prueba, de nuevo, que el origen sigue intacto). El bloque `db_v2`
da esos mismos números — incluido `SUM(sales.total) = 9954.49` — ahí
también.

**Si `db_dev` no coincide con el paso 0:** parate. No sigas al paso 6 sin
entender qué escribió en el origen.

**Si `db_v2` no coincide:** no sigas al paso 6. La copia quedó incompleta o
duplicada. No la reintentes sobre `db_v2` tal cual está — un `TRUNCATE`
simple de `sales`, `purchases` o `products` va a fallar por las foreign
keys que le apuntan desde otras tablas. La forma limpia de reintentar es
borrar el schema entero y repetir desde el paso 1:

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings
e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
with e.connect() as c:
    c.execute(text('DROP SCHEMA "db_v2" CASCADE'))
    c.commit()
PY
```

(Si preferís no borrar el schema, la alternativa es un único `TRUNCATE`
con las nueve tablas y `CASCADE`:
`TRUNCATE "db_v2".customer_product_cycles, "db_v2".sale_item_lot_allocations,
"db_v2".sale_items, "db_v2".sales, "db_v2".purchase_items, "db_v2".purchases,
"db_v2".products, "db_v2".customers, "db_v2".users CASCADE;` — pero borrar
el schema y repetir desde el paso 1 es más simple y menos propenso a que se
te escape una tabla.)

Resultado real obtenido: ____________________________________________

## 6. Aplicar el resto de las migraciones

```bash
POSTGRES_SCHEMA=db_v2 venv/bin/alembic upgrade head
```

Esto aplica `8df5b1f79497` (campos de pago + `cash_movements`) y
`2208c8e60855` (consistencia numérica de cantidades) sobre los datos que
acabás de copiar.

**Éxito:** el comando termina sin error. Repetí el bloque del paso 5 (el que
consulta `db_dev` y `db_v2` en la misma pasada — el censo del paso 0 solo
sabe leer `db_dev`, así que no sirve acá sin editarlo a mano) — los
conteos y el `SUM(sales.total)` no deben haber cambiado respecto del
paso 5. Además, ahora sí, verificá `cash_movements`:

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings
e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
with e.connect() as c:
    print(c.execute(text('SELECT count(*) FROM db_v2.cash_movements')).scalar())
PY
```

Tiene que dar `0`. Eso es correcto, no una falla: `cash_movements` nace
vacía con esta migración y la copia del paso 4 no la toca a propósito (no
está en `TABLE_ORDER`; ver la nota del paso 3).

**Si algún conteo de las nueve tablas de negocio cambió:** para. Ninguna de
estas dos migraciones debería tocar filas existentes; si lo hizo, no sigas
al paso 7 sin entender por qué.

**Si `cash_movements` no da `0`:** eso sí sería una sorpresa real — pasaría
a significar que algo escribió ahí entre que la migración la creó y que la
consultaste. Investigá antes de seguir.

**Si `alembic upgrade head` falla a mitad de camino** (por ejemplo, aplica
`8df5b1f79497` pero `2208c8e60855` tira error): `db_v2` puede haber quedado
estampada en una revisión intermedia con los datos ya copiados encima. No
reintentes `upgrade head` a ciegas. Primero mirá en qué quedó:

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings
e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
with e.connect() as c:
    print(c.execute(text('SELECT version_num FROM db_v2.alembic_version')).scalar())
PY
```

Corregí la causa del fallo (probablemente algo en el entorno, no en los
datos — las migraciones ya se probaron contra este mismo esquema en otras
tasks) y recién entonces repetí `alembic upgrade head`: Alembic solo aplica
lo que falta desde la revisión en la que quedó, no repite lo ya aplicado.

**Si falla citando `db_dev` y el guard:** mismo diagnóstico que en el paso
1 — revisá `POSTGRES_SCHEMA`, no setees `REVENEW_ALLOW_DB_DEV`.

Resultado real obtenido: ____________________________________________

## 7. Probar la app contra db_v2

`uvicorn` queda corriendo en primer plano, así que el `curl` no puede ir
en la misma terminal después de ese comando — no va a llegar a ejecutarse
mientras el server siga arriba. Usá dos terminales:

```bash
# Terminal A — queda corriendo:
POSTGRES_SCHEMA=db_v2 venv/bin/uvicorn app.main:app --port 8002
```

```bash
# Terminal B:
curl -s http://127.0.0.1:8002/health
```

Si solo tenés una terminal disponible, corré `uvicorn` en segundo plano.
El servidor tarda un instante en levantar, así que el primer intento de
`curl` inmediatamente después puede fallar con "connection refused" sin
que eso signifique nada malo — por eso `--retry-connrefused` en vez de
disparar el `curl` una sola vez:

```bash
POSTGRES_SCHEMA=db_v2 venv/bin/uvicorn app.main:app --port 8002 &
curl -s --retry 5 --retry-delay 1 --retry-connrefused http://127.0.0.1:8002/health
# cuando termines de probar:
kill %1
```

Para pegarle a los endpoints reales con un token válido
(`/api/v1/sales`, `/api/v1/products`, `/api/v1/dashboard/summary`):
el token es un **Firebase ID token**, no algo que emita este backend.
Conseguilo iniciando sesión normalmente (el frontend, o la REST API de
Identity Toolkit de Firebase con el email/contraseña de un usuario real) y
copiándolo del inspector de red o del almacenamiento local del navegador.
Se manda como `Authorization: Bearer <token>`:

```bash
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8002/api/v1/sales
```

**Éxito:** `/health` responde OK y los tres endpoints devuelven datos
consistentes con los números verificados en el paso 6 (por ejemplo,
`/api/v1/sales` no debería devolver 0 ventas ni un total distinto de
9954.49 en el agregado).

**Si algún endpoint falla o devuelve datos inconsistentes:** no avances al
paso 8. `db_dev` sigue intacto y la app en producción sigue sirviendo desde
ahí — no hay apuro. Investigá acá, contra `db_v2` en local, con el tiempo
que haga falta. Cuando termines, matá el `uvicorn` de prueba (`kill %1`, o
Ctrl-C en la Terminal A).

Resultado real obtenido: ____________________________________________

## 8. Cambiar el entorno y desplegar

**Antes que nada, re-censo de `db_dev`.** Repetí exactamente la consulta
del paso 0. Tiene que seguir dando `156` ventas y
`SUM(sales.total) = 9954.49` — los mismos números del paso 0 y del re-censo
del paso 4. Si cambiaron, alguien escribió en `db_dev` durante la ventana
que debía estar congelada (pasos 3-8): **parate**. Esas ventas nuevas no
están en `db_v2`. Copialas a mano, o repetí la copia entera (pasos 3-6),
antes de cortar el tráfico hacia `db_v2`.

Con eso confirmado, dos operaciones **en este orden exacto**:

1. **Primero, la variable.** En Railway, `POSTGRES_SCHEMA=db_v2`. Guardarla
   dispara un redeploy automático de la imagen que está corriendo ahora
   mismo — la que todavía **no** tiene `alembic upgrade head` en el
   entrypoint. Esa imagen no es inofensiva porque "no toque la base": su
   `lifespan` corre `prepare_schema_bootstrap()`,
   `Base.metadata.create_all()` y `ensure_schema_compatibility()` — DDL de
   verdad, heredada de antes de este plan, contra lo que sea que
   `POSTGRES_SCHEMA` nombre en ese momento. El redeploy es seguro porque,
   contra `db_v2` ya en `head`, esa DDL no tiene nada para hacer: el
   `DROP TYPE` del enum está condicionado a labels fuera de
   `percent`/`fee` (no hay ninguno), cada `ADD COLUMN` es `IF NOT EXISTS`
   (las columnas ya las puso Alembic), y las promociones a `NUMERIC` solo
   actúan sobre columnas que todavía sean `integer` (ya son numéricas).
   Y para las ventas nuevas que esta imagen vieja escriba contra `db_v2`
   sin conocer `payment_date` ni `payment_method`: tampoco hay problema —
   la migración `8df5b1f79497` agregó esas columnas como `nullable=True`,
   así que un `INSERT` que las omita funciona igual.

   **Este es el momento en que el tráfico real pasa a `db_v2`** — no el
   deploy del paso 3 de abajo. A partir de acá, cualquier venta que entra
   por la app queda solo en `db_v2`. Ver Rollback: de acá en adelante ya
   no es gratis.

2. **Confirmá que sirve desde `db_v2`** antes de seguir: `/health`, y un
   par de pedidos reales a los endpoints del paso 7, deben responder con
   los números verificados en el paso 6 (o mayores, si ya entró tráfico
   nuevo legítimo después del corte).

3. **Recién ahora, deployá la imagen de esta branch** (la que agrega
   `alembic upgrade head` a `entrypoint.sh`, commit `7306fb8` en adelante).
   Como `POSTGRES_SCHEMA` ya es `db_v2` y `db_v2` ya está en `head`, ese
   `alembic upgrade head` no tiene nada que hacer — corre y sale sin
   cambiar nada. El guard de `db_dev` no se activa porque el schema
   resuelto no es `db_dev`.

**No invertís el orden.** Si el paso 3 (la imagen nueva) llega a producción
mientras `POSTGRES_SCHEMA` todavía dice `db_dev`, el guard de
`alembic/env.py` la rechaza, `set -e` aborta el arranque, y el contenedor
no levanta — por eso la variable va primero, con la imagen vieja, que ni
siquiera pasa por ese código.

**Éxito:** el deploy del paso 3 completa, el healthcheck de Railway pasa, y
`/health` responde desde producción con la imagen nueva.

**Si el contenedor de la imagen nueva no arranca y el log muestra el guard
de `db_dev`:** `POSTGRES_SCHEMA` no llegó a aplicarse antes de este
deploy. Confirmá la variable en Railway (paso 1 de arriba) antes de
reintentar el deploy. No hay necesidad de tocar `REVENEW_ALLOW_DB_DEV`.

**Si el paso 2 no confirma bien** (el healthcheck falla, o los endpoints no
devuelven los números esperados) con la imagen vieja ya sirviendo desde
`db_v2`: no sigas al paso 3. Andá a Rollback — el redeploy automático del
paso 1 ya movió tráfico real a `db_v2`, así que un rollback desde acá ya no
es gratis (ver más abajo), pero desplegar la imagen nueva sobre datos que
no verificaste es peor.

Resultado real obtenido: ____________________________________________

## Rollback

**Es gratis solo hasta el momento en que, en el paso 8, seteás
`POSTGRES_SCHEMA=db_v2` en Railway.** El redeploy automático que dispara
ese cambio de variable empieza a servir tráfico real desde `db_v2` de
inmediato — incluso antes de desplegar la imagen nueva del paso 3. Cualquier
venta que entre desde ese momento en adelante existe solo en `db_v2`; un
rollback después de eso la descarta en silencio, sin ningún error que lo
avise en ningún lado.

**Si todavía no seteaste esa variable:** no hay nada que rollbackear —
simplemente no avances al paso 8. `db_dev` sigue siendo el único lugar
donde vive el tráfico.

**Si ya seteaste la variable, con o sin haber desplegado la imagen nueva:**

Dos operaciones, **en este orden** — al revés del orden del paso 8, por un
motivo distinto (ver más abajo):

1. **Primero, la imagen.** Redesplegá en Railway **el deployment que
   estaba en producción antes de que este plan de migración empezara** —
   no cualquier imagen sin `alembic upgrade head`, sino específicamente
   una que además tenga los modelos viejos. Son dos condiciones y las dos
   importan:
   - Sin `alembic upgrade head` en el arranque, para no pisar el guard de
     `db_dev` (ver paso 8).
   - Con modelos que coinciden columna por columna con lo que `db_dev`
     realmente tiene, para no devolver 500 en cada consulta que toque
     `payment_date`, `payment_method`, `expected_unit_margin` o
     `cash_movements` — columnas que esas tablas no tienen en `db_dev`.

     Una imagen construida entre el commit `51cfa6a` (que agregó esos
     campos a los modelos) y el commit `7306fb8` (que agregó
     `alembic upgrade head` al entrypoint) cumple la primera condición y
     falla la segunda: arranca limpio y después rompe en cada venta. Si
     vas a identificar el deployment por commit en vez de por el
     historial de Railway, el límite es **anterior a `51cfa6a`**, no
     anterior a `7306fb8`. En la práctica es más simple y más seguro
     elegirlo directamente del historial de deployments de Railway: "el
     que estaba corriendo en producción antes de que este cutover
     empezara" ya cumple las dos condiciones sin que tengas que mapear
     commits bajo presión.

   Hacelo con `POSTGRES_SCHEMA` todavía en lo que sea que esté seteada en
   ese momento (probablemente `db_v2`) — esta imagen no tiene
   `alembic upgrade head` en el arranque (como mucho, la misma DDL
   heredada del paso 8, inofensiva contra un schema ya en `head` por las
   mismas razones explicadas ahí), así que no importa contra qué schema
   apunte en este paso intermedio.

2. **Confirmá que arrancó bien** antes de seguir: `/health` responde, y el
   log de arranque no menciona el guard de `db_dev` ni ningún error de
   `alembic`.

3. **Recién ahora, la variable:** `POSTGRES_SCHEMA=db_dev`.

**Por qué este orden y no el del paso 8:** ahí la variable iba primero
porque la imagen vieja es segura contra cualquier schema. Acá es al
revés: guardar la variable dispara un redeploy en Railway, y si ese
redeploy reconstruye desde el HEAD de la branch en vez de reusar la
imagen que acabás de fijar en el paso 1, lo que sale a producción es de
nuevo la imagen que sí corre `alembic upgrade head` — ahora apuntando a
`db_dev`. Es el mismo loop guard-abort que este rollback existe para
evitar. Fijando primero la imagen vieja confirmada, el peor caso de un
redeploy disparado por la variable es que vuelva a traer esa misma
imagen vieja, no la nueva.

**Éxito:** después del paso 3, `/health` responde desde producción y el
log de arranque no menciona el guard de `db_dev`.

**Si el log del paso 3 menciona el guard de `db_dev`:** Railway
reconstruyó desde el HEAD de la branch en vez de mantener la imagen que
fijaste en el paso 1 — la imagen nueva volvió a llegar, ahora con
`POSTGRES_SCHEMA=db_dev`, y el guard la rechazó. Volvé a fijar/redesplegar
explícitamente la imagen del paso 1 (si Railway te deja pinear ese
deployment, hacelo, para que un futuro cambio de variable no lo vuelva a
reemplazar). **Bajo ninguna circunstancia** setees
`REVENEW_ALLOW_DB_DEV` para sortear esto.

**Ninguna venta que haya entrado a `db_v2` después del corte del paso 8
vuelve a aparecer al hacer este rollback.** Quedó en `db_v2`, no en
`db_dev`, y este procedimiento no las mueve. Si ya hubo tráfico de
producción real contra `db_v2` antes de decidir el rollback, hay que
migrarlo a mano de vuelta a `db_dev` antes de considerar el rollback
"completo" — de lo contrario esas ventas simplemente no existen para nadie.

`db_dev` en sí sigue intacto durante todo este runbook — ningún paso
escribe en él — así que como respaldo de los datos que tenía **antes** del
cutover, sigue siendo válido en cualquier momento, incluido después de un
rollback.

## Limpieza (semanas después, no ahora)

Cuando `db_v2` lleve tiempo estable: borrar las 4 tablas vacías de `public`
(restos de antes de que existiera `POSTGRES_SCHEMA`) y, solo entonces,
considerar qué hacer con `db_dev`.
