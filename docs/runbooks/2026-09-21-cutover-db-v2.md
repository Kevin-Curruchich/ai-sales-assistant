# Cutover a db_v2

Runbook manual. Cada paso dice qué correr, qué significa que salió bien, y qué
hacer si no. Ejecutalo en orden, uno por uno, verificando entre cada paso
antes de seguir al siguiente. No lo automatices.

**Precondición: nadie usando la app mientras corre la copia (pasos 3-6).**
Una venta que entra a `db_dev` mientras estás copiando queda fuera de
`db_v2` sin ningún aviso.

**Evidencia previa.** El 2026-09-21 se corrió una auditoría de solo lectura
contra `db_dev`: 85 columnas comparadas contra los modelos, cero columnas
`double precision`, cero columnas de más, cero columnas faltantes, cero
desajustes de precisión o nulabilidad. Es decir, no hay motivo para esperar
sorpresas en el paso 2. Aun así, el paso 2 se repite igual: la base pudo
cambiar entre esa auditoría y este cutover, y este runbook no confía en una
foto vieja.

**Sobre el guard de `db_dev`.** Tanto `alembic/env.py` como
`scripts/copy_schema_data.py` se niegan a tocar el schema `db_dev` a menos
que la variable de entorno `REVENEW_ALLOW_DB_DEV=1` esté seteada. Ese guard
existe justamente para este runbook: nada de lo que sigue setea esa
variable, y si algún paso falla citándola, **no la setees**. Es la señal de
que algo (típicamente `POSTGRES_SCHEMA`, o un `--source`/`--target`
invertido) apunta a `db_dev` cuando debería apuntar a `db_v2`. Revisá eso
primero.

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
    rows = c.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='db_v2' ORDER BY table_name"
    )).fetchall()
    print([r[0] for r in rows])
PY
```

Deben aparecer las nueve tablas de negocio más `alembic_version`, todas
vacías.

**Si falla citando `db_dev` y el guard `REVENEW_ALLOW_DB_DEV`:** no setees
esa variable. Revisá `POSTGRES_SCHEMA` en el entorno desde el que corriste
el comando — probablemente no se exportó y Alembic cayó al default
(`db_dev`). Corregilo y repetí el paso.

**Si falla por cualquier otro motivo:** no sigas. `db_v2` puede haber
quedado a medio crear; investigá el error antes de reintentar.

## 2. Diff de esquemas antes de copiar

```bash
venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text
from app.core.config import settings
from scripts.copy_schema_data import TABLE_ORDER
e = create_engine(settings.SQLALCHEMY_DATABASE_URI)
q = ("SELECT column_name, data_type FROM information_schema.columns "
     "WHERE table_schema=:s AND table_name=:t ORDER BY column_name")
with e.connect() as c:
    for t in TABLE_ORDER:
        a = {r[0]: r[1] for r in c.execute(text(q), {"s":"db_dev","t":t})}
        b = {r[0]: r[1] for r in c.execute(text(q), {"s":"db_v2","t":t})}
        if a != b:
            print(f"--- {t}")
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    print(f"    {k}: db_dev={a.get(k)}  db_v2={b.get(k)}")
PY
```

**Éxito:** sin salida. Eso confirma, contra el estado real de hoy, lo mismo
que ya mostró la auditoría del 2026-09-21.

**Si hay salida:** no sigas al paso 3. Cada línea impresa es una columna
que difiere entre `db_dev` y `db_v2` (falta de un lado, o mismo nombre con
tipo distinto). Resolvé el drift — típicamente ajustando la revisión inicial
o investigando qué cambió en `db_dev` desde el 2026-09-21 — antes de copiar
nada.

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
script está comparando tipo, precisión y escala entre `db_dev` y `db_v2`
para esa columna, no solo el nombre. Es la misma clase de drift que el paso
2 debería haber cazado — si llegaste hasta acá es que algo cambió entre el
paso 2 y este, o que el paso 2 no cubría esa tabla. No copies con ese
mensaje en pantalla: los datos podrían truncarse o redondearse en silencio.

**Si aborta citando `db_dev` y `REVENEW_ALLOW_DB_DEV`:** revisá que no
invertiste `--source` y `--target`. No setees la variable.

## 4. Copia real

```bash
venv/bin/python -m scripts.copy_schema_data --source db_dev --target db_v2
```

**Éxito:** misma salida que el paso 3, sin la línea de `(dry-run...)`. Si el
paso 3 salió limpio, este paso no debería sorprender — corre exactamente la
misma lógica, ahora con commit real.

**Si falla:** el script corre todo en una sola transacción (o se copia
entero, o no se copia nada), así que una falla acá no deja `db_v2` a medias.
`db_dev` tampoco se toca en ningún caso: el script nunca escribe en el
origen. Resolvé la causa del error (mismos motivos que en el paso 3) y
repetí desde el paso 3 en seco antes de reintentar la copia real.

## 5. Verificar conteos y checksums

Repetí el censo del paso 0, apuntando a `db_v2` en vez de `db_dev` (cambiá
`db_dev.{t}` por `db_v2.{t}` en la query, o pasale `db_v2` como schema).

**Éxito:** los mismos números del paso 0, exactos, incluido
`SUM(sales.total) = 9954.49`. Además, `SELECT count(*) FROM
db_v2.cash_movements` debe dar `0` — eso es correcto, no una falla:
`cash_movements` nace vacía y esta copia no la toca a propósito.

**Si algún conteo no coincide:** no sigas al paso 6. La copia quedó
incompleta o duplicada. No reintentes la copia sobre `db_v2` tal cual está
— truncá las tablas de `db_v2` que ya tengan filas y repetí desde el paso 3,
o investigá antes de tocar nada más.

## 6. Aplicar el resto de las migraciones

```bash
POSTGRES_SCHEMA=db_v2 venv/bin/alembic upgrade head
```

Esto aplica `8df5b1f79497` (campos de pago + `cash_movements`) y
`2208c8e60855` (consistencia numérica de cantidades) sobre los datos que
acabás de copiar.

**Éxito:** el comando termina sin error. Repetí el censo del paso 0 contra
`db_v2` una vez más — los conteos y el `SUM(sales.total)` no deben haber
cambiado respecto del paso 5.

**Si algún conteo cambió:** para. Ninguna de estas dos migraciones debería
tocar filas existentes; si lo hizo, no sigas al paso 7 sin entender por qué.

**Si falla citando `db_dev` y el guard:** mismo diagnóstico que en el paso
1 — revisá `POSTGRES_SCHEMA`, no setees `REVENEW_ALLOW_DB_DEV`.

## 7. Probar la app contra db_v2

```bash
POSTGRES_SCHEMA=db_v2 venv/bin/uvicorn app.main:app --port 8002
curl -s http://127.0.0.1:8002/health
```

Y pegale a los endpoints reales con un token válido: `/api/v1/sales`,
`/api/v1/products`, `/api/v1/dashboard/summary`.

**Éxito:** `/health` responde OK y los tres endpoints devuelven datos
consistentes con los números verificados en el paso 6 (por ejemplo,
`/api/v1/sales` no debería devolver 0 ventas ni un total distinto de
9954.49 en el agregado).

**Si algún endpoint falla o devuelve datos inconsistentes:** no avances al
paso 8. `db_dev` sigue intacto y la app en producción sigue sirviendo desde
ahí — no hay apuro. Investigá acá, contra `db_v2` en local, con el tiempo
que haga falta.

## 8. Cambiar el entorno y desplegar — un solo movimiento

`entrypoint.sh` corre `alembic upgrade head` antes de levantar el servidor.
Si el código nuevo llega a producción mientras `POSTGRES_SCHEMA` todavía
dice `db_dev`, el guard de `alembic/env.py` va a rechazar la migración, el
`set -e` del entrypoint va a abortar, y el contenedor **no va a arrancar**.
Eso es lo esperado y es protector, no un bug — pero significa que estos dos
cambios no son dos pasos, son uno solo:

1. En Railway, setear `POSTGRES_SCHEMA=db_v2`.
2. Redeploy del mismo servicio, en la misma operación.

No los hagas por separado (por ejemplo, redeployar primero "para probar" y
cambiar la variable después, o viceversa): cualquier orden intermedio deja
una ventana donde el contenedor no levanta.

**Éxito:** el deploy completa, el healthcheck de Railway pasa, y `/health`
responde desde producción.

**Si el contenedor no arranca y el log muestra el guard de `db_dev`:**
`POSTGRES_SCHEMA` no llegó a aplicarse antes del redeploy (variable mal
guardada, deploy disparado antes de que se guardara, etc.). Corregí la
variable en Railway y volvé a desplegar. No hay necesidad de tocar
`REVENEW_ALLOW_DB_DEV`.

**Si el contenedor arranca pero la app se comporta mal:** andá al Rollback,
abajo. No hay que diagnosticar en caliente contra producción con el negocio
parado.

## Rollback

```
POSTGRES_SCHEMA=db_dev
```

y redeploy. `db_dev` queda intacto con sus 156 ventas — ningún paso de este
runbook lo modifica, así que sigue siendo un respaldo en vivo válido en
cualquier momento hasta acá.

## Limpieza (semanas después, no ahora)

Cuando `db_v2` lleve tiempo estable: borrar las tablas vacías que hayan
quedado huérfanas en `public` y, solo entonces, considerar qué hacer con
`db_dev`.
