# Adopción de Alembic y cambios de esquema

**Fecha:** 2026-09-21
**Estado:** diseño aprobado, pendiente de plan de implementación

## Problema

El proyecto no tiene migraciones. El esquema se crea con `Base.metadata.create_all()`
en el arranque, y los cambios posteriores se aplican con `ensure_schema_compatibility()`
en `app/core/database.py:47` — un runner de migraciones hecho a mano cuyo propio
docstring dice *"Apply additive schema updates for running environments without Alembic"*.

Ese mecanismo no versiona, no es reversible, y no deja registro de qué se aplicó.
Con cambios de esquema en camino, deja de ser viable.

## Estado actual verificado

Una sola instancia Postgres en Railway (`revenew`) con dos schemas:

- `db_dev` — 9 tablas, los datos reales: 156 ventas (2026-04-07 → 2026-08-27),
  21 clientes, 2 productos, 38 compras, Q9,954.49 facturados.
- `public` — 4 tablas vacías, restos de antes de que existiera `POSTGRES_SCHEMA`.

El `.env` local apunta a esa misma base. No existe un entorno de desarrollo separado.

### Divergencia con el Google Sheet

El Sheet de Revenew cubre 2026-08-05 → 2026-09-19 y contiene datos que la base no
tiene: todo septiembre y las 26 filas de la hoja Caja. El solape de agosto es casi
idéntico (base: 27 ventas / Q1,614.00; Sheet: 26 ventas / ≈Q1,619), lo que confirma
que son el mismo negocio y no dos conjuntos distintos.

**Importar el Sheet queda explícitamente fuera de alcance.** `cash_movements` se crea
vacía. Reconciliar ambos mundos es un proyecto aparte.

## Decisiones

| Decisión | Elegido | Motivo |
|---|---|---|
| Schema en las migraciones | Agnóstico vía `search_path` | Una misma migración sirve para `db_dev`, `db_v2` y `public` |
| Destino | Schema nuevo `db_v2`, misma instancia | Copia con SQL, sin dump/restore; rollback = cambiar una variable |
| Aplicación en deploy | Automática en `entrypoint.sh` | Un deploy = un esquema al día |
| Aporte de socio | Solo `cash_movements` | Una sola fuente de verdad; en el Sheet la columna duplicada ya divergió |
| Alcance | Alembic + esquema, sin importar el Sheet | Aísla el riesgo: si algo falla, fue la migración |

## Arquitectura

### Configuración

```
alembic.ini              # sin URL — las credenciales viven solo en settings
alembic/
  env.py
  script.py.mako
  versions/
```

`env.py` concentra toda la lógica del schema dinámico:

- URL y schema desde `app.core.config.settings`, nunca desde `alembic.ini`.
- `CREATE SCHEMA IF NOT EXISTS <schema>` antes de migrar.
- `SET search_path TO <schema>` en la conexión — el mismo mecanismo del engine
  (`app/core/database.py:17`).
- `version_table_schema=<schema>`: `alembic_version` vive dentro de cada schema,
  de modo que `db_dev` y `db_v2` llevan versión independiente.
- `include_schemas=False` y metadata sin schema → ninguna operación emite `schema=`.
- `compare_type=True` — necesario porque los cambios recientes son de tipo.
- `pg_advisory_lock` alrededor del upgrade, para el día que haya varias réplicas.

### Cambios en el código existente

En `app/core/database.py`: `MetaData(schema=SCHEMA)` pasa a
`MetaData(naming_convention=...)`. En `app/models/product.py:29` se quita el
`schema=SCHEMA` del `SQLEnum`. El `search_path` resuelve ambos casos.

Convención de nombres, gratis ahora porque el schema se crea desde cero:

```python
{"ix": "ix_%(column_0_label)s",
 "uq": "uq_%(table_name)s_%(column_0_name)s",
 "ck": "ck_%(table_name)s_%(constraint_name)s",
 "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
 "pk": "pk_%(table_name)s"}
```

Sin ella, los índices y FKs sin nombre explícito reciben los nombres por defecto de
Postgres y el autogenerate no puede alterarlos de forma fiable más adelante.

### Código que se elimina

Del `lifespan` en `app/main.py` y de `app/core/database.py` (~80 líneas):

- `Base.metadata.create_all()` — convivir con Alembic es el clásico pie de plomo:
  crea tablas que Alembic cree que nunca existieron.
- `ensure_schema_compatibility()` — sus tres parches quedan incorporados en `001`.
- `prepare_schema_bootstrap()` — el hack del enum con labels en mayúscula no tiene
  sentido en un schema recién creado.

## Migraciones

### `001` — esquema actual, exacto

Reproduce lo que hoy existe en `db_dev`, incluyendo los parches que aplica
`ensure_schema_compatibility()`: `purchase_items.remaining_quantity`,
`sale_items.discount_percent`, `sale_items.discount_amount`,
`sales.is_payment_pending`.

Sale de `--autogenerate` contra una base vacía pero **se revisa a mano**: el
autogenerate no es confiable con enums nativos (`earning_mode_enum`) ni con
`server_default=func.gen_random_uuid()`.

El objetivo es que la copia de datos desde `db_dev` sea columna por columna, sin
traducciones. Meter aquí los campos nuevos haría que `001` no corresponda a ningún
esquema que existió nunca.

### `002` — campos nuevos y libro de caja

Derivados del diff contra el Sheet.

**`purchases`**
- `payment_method` `Optional[str]` — el `medio_pago` de la hoja Compras.

**`sales`**
- `payment_date` `Optional[date]` — el `fecha_pago`. **Nullable y sin CHECK**: en el
  Sheet, VTA-046 está pagada con `fecha_pago` vacía y VTA-001 tiene fecha de pago
  anterior a la venta. Exigir coherencia rompería con datos reales.
- `payment_method` `Optional[str]`.

**`sale_items`**
- `expected_unit_margin` `Numeric(14,2)` nullable — el `margen_base`, congelado al
  momento de la venta igual que `cost_basis_unit`. Sin él, cambiar el
  `earning_percent` de un producto destruye la capacidad de calcular
  `margen_extra = margen_real − margen_base` retroactivamente.
  **Explícitamente por unidad**, porque en el Sheet la columna es ambigua: VTA-001
  usa 1.75 para media unidad y VTA-042 usa 3,5 para media unidad.

**`customer_product_cycles`**
- `projection_method` `Optional[str]` — `"ewma"` | `"croston"`.
- `projection_confidence` `Optional[str]`.
- `calendar_event_id` `Optional[str]` — el `evento_calendario_id`.

> `projection_confidence` no existe en la hoja Proyecciones; es un requisito nuevo,
> no un gap contra el Sheet.

**Tabla nueva `cash_movements`**

| Columna | Tipo | Nota |
|---|---|---|
| `id` | UUID PK | |
| `movement_date` | Date | |
| `type` | enum | `entrada` \| `salida` \| `aporte_socio` \| `retiro_socio` |
| `amount` | Numeric(14,2) | |
| `payment_method` | Optional[str] | |
| `sale_id` | FK nullable → `sales.id` | |
| `purchase_id` | FK nullable → `purchases.id` | |
| `note` | Optional[Text] | |
| `created_at` | timestamptz | |

`retiro_socio` no aparece en los datos del Sheet; se incluye por simetría con
`aporte_socio`.

**Limitación conocida y aceptada:** un movimiento solo puede apuntar a una venta.
En el Sheet, MOV-019 tiene `referencia = "VTA-039+VTA-040+VTA-044"` — un cobro de Q90
que salda tres ventas de una vez. Ese caso no entra en este diseño.

Se acepta a propósito: cubre 25 de los 26 movimientos del Sheet, y `cash_movements`
nace vacía, así que el caso no existe todavía en la base. Cuando aparezca, la salida es
una tabla `cash_movement_allocations` (`cash_movement_id`, `sale_id`, `amount`,
siguiendo el patrón que el repo ya usa en `sale_item_lot_allocations`) introducida en
su propia migración — que es justamente para lo que sirve tener Alembic. Mientras
tanto, un cobro que salde varias ventas se registra como varios movimientos.

**`saldo_acumulado` no se guarda.** Se calcula al leer con
`SUM(...) OVER (ORDER BY movement_date)`. La evidencia respalda la decisión: en el
Sheet, MOV-011 cierra en 375, MOV-013 suma un aporte de 125.02 y muestra 295 en vez
de 500.02. La columna está desincronizada.

### `003` — consistencia de tipos (opcional, separable)

- `products.min_stock`: `Integer` → `Numeric(10,4)`, para acompañar a `stock`.
- `purchase_items.quantity`: `Integer` → `Numeric(10,4)`. Hoy `remaining_quantity` ya
  es `Numeric(10,4)` mientras `quantity` sigue entero (`app/models/purchase.py:55`):
  vender medio cartón deja un restante de 5.5 sobre un inicial de 6.

## Copia de datos `db_dev` → `db_v2`

1. **Diff de esquemas** antes de copiar: comparar columna por columna `db_dev` contra
   el `db_v2` recién migrado con `001`. Cualquier drift que hayan dejado los parches
   aparece aquí y no a mitad del copiado.
2. `INSERT INTO db_v2.x SELECT <columnas explícitas> FROM db_dev.x`, nunca `SELECT *`,
   **todo en una sola transacción**, en orden seguro de FK:

   `users → customers → products → purchases → purchase_items → sales → sale_items
   → sale_item_lot_allocations → customer_product_cycles`

3. Recién entonces aplicar `002` y `003` sobre `db_v2`.

## Verificación

- **Conteos exactos** contra el estado de hoy: `users 2, customers 21, products 2,
  purchases 38, purchase_items 38, sales 156, sale_items 156,
  sale_item_lot_allocations 155, customer_product_cycles 26`.
- **Checksums monetarios**: `SUM(sales.total)` debe dar **Q9,954.49**. Una copia puede
  traer todas las filas y aun así truncar decimales.
- **Reversibilidad**: `alembic downgrade base` y `upgrade head` en un schema desechable.
- **La app de verdad**: levantarla contra `db_v2` y pegarle a los endpoints reales
  antes de dar el trabajo por hecho.

## Deploy

`entrypoint.sh` gana una línea antes de hypercorn:

```sh
alembic upgrade head
exec hypercorn app.main:app --bind "0.0.0.0:${PORT:-8000}"
```

Con el `set -e` que ya tiene, si la migración falla el contenedor no arranca — que es
el comportamiento correcto. `alembic` se suma a `requirements.txt`.

## Rollback

`POSTGRES_SCHEMA=db_dev`. El schema viejo queda intacto como respaldo vivo, con sus
156 ventas. No se borra nada en ningún paso de este plan.

**Superado por `docs/runbooks/2026-09-21-cutover-db-v2.md`.** Esta línea
asume que solo hay que cambiar la variable, pero una vez que `entrypoint.sh`
corre `alembic upgrade head` al arrancar (ver más arriba), redesplegar la
imagen de este plan con `POSTGRES_SCHEMA=db_dev` dispara el guard de
`db_dev` en `alembic/env.py` y el contenedor no levanta — el rollback real
también necesita volver a la imagen anterior a ese cambio. El runbook tiene
el procedimiento completo y las condiciones bajo las que deja de ser
gratis; seguí ese documento, no esta línea, al ejecutar un rollback.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| Drift entre `db_dev` y los modelos | El diff de esquemas antes de copiar |
| Autogenerate emite mal el enum nativo | Revisión manual obligatoria de `001` |
| Escrituras durante la copia | La copia es de una sola pasada; no usar la app mientras corre |
| `public` con 4 tablas vacías confunde | Limpieza aparte, fuera de este trabajo |
| La base queda más desactualizada aún | Consecuencia aceptada: importar el Sheet es proyecto aparte |
