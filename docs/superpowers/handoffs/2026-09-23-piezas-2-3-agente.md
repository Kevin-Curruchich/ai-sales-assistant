# Handoff a las piezas 2 y 3 del agente Revenew

**Fecha:** 2026-09-23
**Origen:** cierre de la pieza 1 (`feat/agent-service-surface`), revisión final de rama.
**Spec de la pieza 1:** `docs/superpowers/specs/2026-09-22-superficie-servicios-agente-design.md`

La pieza 1 construyó la superficie de services que las herramientas del agente van a
importar en proceso. Esto es lo que quedó **deliberadamente sin cablear** y lo que la
revisión final encontró. No son defectos de la pieza 1: el spec prohibió explícitamente
cerrarlos ("Ningún schema nuevo, ninguna otra adición"). Son el punto de partida de la
pieza 2.

## Huecos de escritura: las columnas existen, nada puede escribirlas

La pieza 1 añadió y restringió columnas que ninguna ruta de código puede llenar todavía.

- `sales.payment_method` tiene su CHECK y `sales.occurred_at` existe, pero `SaleCreate`
  (`app/schemas/sale.py`) no lleva `medio_pago`, `fecha_pago` ni `occurred_at`. La skill
  `registrar-venta` declara los tres.
- `PurchaseCreate` (`app/schemas/purchase.py`) no lleva `medio_pago`. La skill
  `registrar-compra` lo declara con default `efectivo`.
- `SaleService.create(data, user_id)` exige un FK a `users`. El agente necesita su propia
  fila de usuario antes de poder registrar nada.

## Atomicidad: `record()` commitea por dentro

`CashService.record()` commitea (`app/services/cash_service.py`). Se conservó así a
propósito — es la convención del repo y cambiarlo sin arreglar el modelo transaccional de
fondo dejaría a `CashService` como la única excepción.

Pero los pasos 5–6 de `registrar-compra` son exactamente la composición que lo necesita:
compra + `aporte_socio` + `salida`, dos services que commitean por separado. Un fallo entre
ellos deja el inventario subido sin movimiento de caja. Añadir `commit: bool = True` a
`record()` es barato y retrocompatible; hacerlo cuando la pieza 2 componga esa secuencia.

## Skills sin costura limpia

Siete de nueve se pueden llamar hoy tal cual. Dos no:

- **`detectar-precio-habitual`** — `pricing.detect_habitual_margin()` es pura y correcta,
  pero recibe una lista de márgenes. La lógica que *construye* esa lista (últimos 3 items,
  descartar los de `cost_basis_unit` nulo, restar) vive inline en `orchestrator.py` y no es
  invocable. El agente o la reimplementa —y puede equivocarse— o llama a `preview_sale` y
  lee `is_habitual_price`. Un `habitual_margin_for(customer_id, product_id, cost_basis)`
  en el orquestador lo cierra.
- **`calcular-proyeccion`** — `projection.project(dates)` es pura y correcta, pero no hay
  forma invocable de **recalcular y persistir** el ciclo de un par cliente/producto.
  `_update_cycle` es privado y exige un `sale_date` y `quantity` que también escribe. La
  skill dice "úsala cuando Kevin pregunte «¿cuándo compra de nuevo X?»" y esa ruta no
  existe. Extraer el recálculo por ciclo del backfill a un método de service y hacer que
  `_update_cycle` y el script lo llamen.

También: `fifo.check_availability()` no tiene ningún llamador en la app. `create()` sigue
reportando el 409 genérico, así que el reporte de inconsistencia "stock sin lotes" sólo le
llega a quien llame la función directamente — o sea, al agente.

## Seguridad de schema: los guards protegen scripts, no al service layer

Esto es lo más importante de este documento.

`PROTECTED_SCHEMA` / `REVENEW_ALLOW_DB_DEV` protegen tres scripts (`alembic/env.py`,
`scripts/copy_schema_data.py`, `scripts/backfill_projections.py`). No protegen nada más.

La pieza 1 añadió el **primer service con capacidad de escritura pensado para importarse en
proceso** (`CashService.record`, que commitea) y ese service no tiene guard. `.env` resuelve
`POSTGRES_SCHEMA` a `db_dev` y `app/core/config.py` lo tiene como default, así que quien
abra un REPL en el repo y llame `CashService(SessionLocal()).record(...)` escribe en el
respaldo vivo, sin aviso.

Nada de la pieza 1 hace eso. La pieza 2 es un programa cuyo trabajo entero es llamar ese
método. Decidir antes de empezarla:

- Cambiar el default de `POSTGRES_SCHEMA` para que el schema protegido no sea el que se
  hereda sin configurar. Cuesta una línea. Contraargumento: el default alternativo es
  `db_v2`, que ahora es el dato vivo, así que un REPL descuidado golpea producción en vez
  del respaldo. Ningún default es seguro; es una elección entre dos riesgos.
- Subir el guard a `app/core/database.py` para que proteja a todo importador. **No se puede
  hacer todavía**: la app arranca contra `db_dev` en producción hasta el deploy del cutover,
  y el guard rompería el arranque.

Queda abierto a propósito, escalado al usuario.

## Nota operativa

`scripts/copy_schema_data.py --source db_dev --target db_v2` ahora lanza `SchemaMismatch`
en `avg_interval_days` (`integer` en el origen contra `numeric(10,4)` en el destino). Es el
guard funcionando: se niega en vez de redondear en silencio, y la copia es transaccional.
Pero la ruta de re-siembra db_dev → db_v2 está bloqueada hasta que alguien añada el cast.
Escribirlo antes de necesitarlo con prisa.

## Menores diferidos por la revisión final

Ninguno bloquea. En orden aproximado de valor:

- Un test de deriva entre el enum `PaymentMethod` y el CHECK de la migración. El camino
  honesto no es `create_constraint=True` (Postgres refleja `IN (...)` como
  `= ANY (ARRAY[...])`, así que autogenerate sigue sin detectarlo); es un test propio.
- `ledger(start=...)` acumula desde cero, así que para cualquier `start` la columna de saldo
  es un subtotal de ventana, no el saldo del libro — y contradice en silencio a
  `running_balance(as_of=end)`. O arrastrar el saldo de apertura, o renombrar para que la
  semántica de ventana sea explícita. Ningún test lo cubre.
- Un quinto tile `needs_estimate` en `FollowUpMetrics`. Los cuatro actuales se derivan de
  `days_until`, que un cliente sin estimación no tiene, así que el filtro actual es
  correcto; lo que falta es el tile, y añadirlo toca el schema que el spec congeló.
- `predict_next_purchase` (`customer_service.py`) descarta los ciclos `insufficient` sin
  marcador. La mitigación se implementó sólo en `get_follow_ups`.
- Una venta con precio habitual detectado no deja rastro en la fila: `create` descarta
  `is_habitual` y guarda `is_price_overridden=False`. `sale_items.expected_unit_margin`
  existe y está sin usar.
- `unit_cost_snapshot=float(lot.unit_cost)` — el único `float` del camino de dinero, en la
  tabla cuyo trabajo es ser el snapshot inmutable de auditoría. Preexistente.
- `SalePreviewTotals`: `total_revenue − total_cost ≠ total_gross_profit` por un centavo en
  cantidades fraccionarias (redondeo unitario y luego multiplicación). Consistente entre
  preview, create y el reporte de ganancias, así que nada se contradice entre módulos —
  pero es el único payload donde ambas cantidades aparecen juntas y no cuadran.
- La fórmula de precio sugerido sigue en dos sitios (`pricing.suggested_unit_price` y
  `ProductService._compute_suggested_price`). Verificadas matemáticamente idénticas.
- `app/services/sales/__init__.py` importa el orquestador con avidez, así que importar una
  función "pura" arrastra FastAPI, todos los repositorios y el `create_engine` de módulo.
- `app/models/__init__.py` no reexporta `CASH_OUTFLOW_TYPES` ni `PaymentMethod`.
- El límite de 10 000 filas en `ledger()` no tiene paginación detrás.
- `build_profit_rows` con lista vacía y `group_by` inválido devuelve `[]` en vez de lanzar,
  así que `?group_by=bogus` sobre un rango vacío responde 200.

## Contratos que cambian para el panel

Van en el checklist del deploy, junto con el cambio de `POSTGRES_SCHEMA`:

- `created_at` / `updated_at` pasan de string truncado a instante ISO completo en ventas,
  compras, productos y los dicts de cliente. Elegido en el spec.
- `format_sale_dates` devuelve `created_at` como `"%Y-%m-%d"` mientras `format_customer_dates`
  devuelve un `datetime` — y ambos aparecen en el mismo payload de `get_customer_details`.
  Los dos son correctos en cuanto al día; la forma es incoherente y es preexistente.
- `PurchaseResponse.created_at` y `ProductResponse.created_at` son no-opcionales mientras
  `_format_datetime` puede devolver `None`. Inalcanzable hoy (ambas columnas tienen
  `server_default`), o sea un 500 latente, no vivo.
