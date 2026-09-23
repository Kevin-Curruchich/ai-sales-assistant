# Superficie de servicios para el agente Revenew

**Fecha:** 2026-09-22
**Estado:** diseño aprobado, pendiente de plan de implementación
**Pieza 1 de 3** — las otras dos (runtime del agente, Google Calendar) tienen su propio ciclo.

## Problema

El agente Revenew vive hoy en Fleet, con Google Sheets como base de datos. Se va a traer
al repo como servidor LangGraph, y sus herramientas van a **importar los services en
proceso** contra Postgres. Esta pieza construye esa superficie.

No es un refactor de higiene. Tres de las nueve skills del agente no tienen dónde apoyarse,
y una calcularía algo distinto de lo que declara.

## Estado verificado

Contra `db_v2` (26 ciclos, 156 ventas, 21 clientes) y el export en `Revenew/`.

| Skill del agente | Hoy en el repo |
|---|---|
| `consumir-lote-fifo` | `sale_service._allocate_fifo_lots:64` — existe, privado |
| `verificar-lote-disponible` | `product_service.get_lots_availability` |
| `calcular-margen-extra` | dentro de `_resolve_sale_item_pricing` — entrelazado |
| `registrar-venta` | `sale_service.create:484` |
| `registrar-compra` | `purchase_service` |
| `buscar-cliente` | `customer_service.get_all(search=)` — parcial |
| `calcular-proyeccion` | existe, **con el método equivocado** |
| `detectar-precio-habitual` | **no existe** |
| `registrar-movimiento-caja` | **no existe nada** |

### Los tres huecos

**1. `cash_movements` existe y nada la usa.** La tabla y el modelo se crearon en la
migración `002`; no hay repositorio, ni servicio, ni endpoints. Las secciones 4, 7 y 8 del
`AGENTS.md` (flujo de caja, deuda con el socio, resumen semanal) están construidas sobre ella.

**2. La proyección contradice al agente.** `sale_service._update_cycle:631` usa el promedio
aritmético de todos los intervalos. La skill `calcular-proyeccion` pide EWMA con α=0.3.
Además, con menos de 2 compras el código **inventa 30 días**, mientras el `AGENTS.md` dice
*"no proyectes con confianza — pide un estimado inicial en vez de inventarlo"*. Nunca escribe
`projection_method` ni `projection_confidence`, que existen desde `002` y están en NULL.

**3. `detectar-precio-habitual` no existe.** `_suggested_unit_price:105` usa siempre el
margen estándar del producto.

## Decisiones

| Decisión | Elegido | Motivo |
|---|---|---|
| Superficie | Solo services y repositorios | El agente importa en proceso; los endpoints son otro consumidor y otro ciclo |
| Método de proyección | **EWMA únicamente** | Croston no se justifica con estos datos (ver abajo) |
| `avg_interval_days` | `Integer` → `Numeric(10,4)` | Redondear en cada paso degrada el suavizado en los intervalos cortos |
| Ciclos existentes | Recalcular desde el historial | Deja los 26 con valor, método y confianza fundamentados |
| Precio habitual | También en `preview_sale` | Panel y agente deben sugerir lo mismo |
| Estructura | Paquete `app/services/sales/` | Las skills marcan las costuras; solo 5 imports cambian |

### Por qué se descarta Croston

Croston separa el suavizado del **tamaño** de demanda del de su **intervalo**, para demanda
intermitente. Los datos no lo sostienen: de 100 ventas de huevos, 88 son exactamente 1
unidad; de 56 de gas, 54 son exactamente 1. Sin variación de tamaño, Croston degenera en
EWMA sobre intervalos dividido por una constante, con el doble de parámetros.

El `AGENTS.md` lo prescribe, pero se escribió antes de mirar los datos. `projection_method`
se guarda igual, de modo que añadirlo después no cuesta nada.

### Por qué EWMA sí cambia las decisiones

Medido sobre los datos reales:

| Par | Últimos 3 intervalos | Media simple | EWMA α=0.3 | Efecto |
|---|---|---|---|---|
| Cecy / gas | 17, 21, 19 | 23.4 d | **19.3 d** | la contactarías 4 días tarde |
| Tía Lily / huevos | 6, 9, 34 | 11.8 d | **17.4 d** | la perseguirías 6 días antes de tiempo |
| Aurita / huevos | 2, 4, 4 | 3.7 d | 3.4 d | casi igual |

Cecy tiene un hueco de 56 días al principio que la media arrastra para siempre. Tía Lily
está desacelerando y la media sigue recordando cuando compraba cada 5 días. Aurita apenas
difiere porque su comportamiento es estable — EWMA solo importa cuando el cliente cambia,
que es justo cuando interesa enterarse.

En EWMA la compra más reciente pesa 30%, la anterior 21%, la siguiente 14.7%; las últimas
ocho concentran el 94% del peso.

## Arquitectura

```
app/services/sales/
  __init__.py       re-exporta SaleService — los 5 imports actuales siguen funcionando
  fifo.py           consumir-lote-fifo + verificar-lote-disponible
  pricing.py        detectar-precio-habitual + calcular-margen-extra
  projection.py     calcular-proyeccion (EWMA)
  reporting.py      reporte de ganancias e insumos del resumen semanal
  orchestrator.py   SaleService: registrar-venta, preview, update
app/services/cash_service.py
app/repositories/cash_movement_repository.py
```

Los importadores actuales de `sale_service` son cinco: `endpoints/sales.py`,
`endpoints/follow_ups.py`, `endpoints/dashboard.py`, `endpoints/calendar.py` y
`purchase_service.py`. El `__init__.py` los mantiene funcionando sin editarlos.

### `fifo.py`

`_allocate_fifo_lots` sale de `sale_service` como **función pura sobre lotes**, sin sesión de
base de datos — que es lo que pide la skill: *"separada de registrar_venta para poder
probarla de forma aislada"*. Devuelve la lista de `(lote, cantidad_tomada, costo_unitario)` y
el costo ponderado cuando la venta cruza lotes.

`verificar-lote-disponible` añade la comprobación que la skill exige y que hoy no existe: si
`stock_actual > 0` pero no hay ningún lote con existencia, **es una inconsistencia que se
reporta**, no un costo que se inventa.

### `pricing.py`

`detectar-precio-habitual`: toma las últimas 3 ventas del par cliente/producto; si el
`margen_real` es consistente entre ellas (diferencia ≤ Q2) pero distinto del margen estándar,
ese es el margen habitual del cliente. Con menos de 3 ventas no hay patrón.

Se integra en `_resolve_sale_item_pricing`, de modo que `preview_sale` sugiere el precio
habitual. **Esto cambia lo que el panel muestra hoy** — es deliberado: panel y agente deben
coincidir, y un precio que cambia en silencio sería peor que el cambio en sí.

Para que no sea silencioso, `SaleItemPreview` gana un único campo booleano,
`is_habitual_price: bool = False`, siguiendo exactamente la convención del
`is_price_overridden` que ya tiene al lado.

`calcular-margen-extra` se separa como cálculo puro:
`margen_real = precio − costo_aplicado`, `margen_extra = margen_real − margen_base`.

### `projection.py`

EWMA con α=0.3: `nuevo = 0.3 × observado + 0.7 × anterior`. `projection_method` guarda
`"ewma"`.

`projection_confidence`, en inglés como el resto de los enums del repo (`active`, `percent`,
`draft`):

| Valor | Condición | Efecto |
|---|---|---|
| `insufficient` | < 2 compras | **No se proyecta**: `estimated_next_purchase = NULL` |
| `low` | 2–3 compras | |
| `medium` | 4–7 compras | |
| `high` | 8+ compras | |

**Los 7 ciclos con una sola compra no se pierden.** Hoy los 26 tienen fecha, pero 7 de ellas
están inventadas a 30 días. Al dejar de inventarlas, `get_follow_ups` los expone en un grupo
propio — *falta estimado inicial* — para que el panel y el agente los pidan, en vez de que
desaparezcan del calendario. Es lo que manda el `AGENTS.md`.

### `cash_service.py` y su repositorio

Nuevos desde cero. Tipos `entrada`, `salida`, `aporte_socio`, `retiro_socio`.

`saldo_acumulado` se **calcula al leer** con `SUM(...) OVER (ORDER BY movement_date)`, nunca
se almacena — decisión ya tomada en el spec de migraciones y que la hoja de cálculo original
demostró correcta al desincronizarse.

`owner_balance` (lo que el negocio le debe a Kevin) = suma de `aporte_socio` − suma de
`retiro_socio`.

Los ganchos que describe la sección 4 del `AGENTS.md` (venta pagada → `entrada`; compra →
`salida`; aporte → `aporte_socio`) quedan **disponibles pero sin cablear** a los flujos
existentes. Cablearlos cambiaría el comportamiento de `/api/v1/sales` y eso excede "solo
services".

### `orchestrator.py`

`SaleService` conserva `create`, `update`, `preview_sale`, `update_payment_status_enriched` y
la serialización. Pasa a componer los servicios anteriores en vez de implementarlos.

## Migración `004` y backfill

**Migración:** `customer_product_cycles.avg_interval_days` de `Integer` a `Numeric(10,4)`,
con `postgresql_using` para conservar los valores. El `downgrade` pierde decimales y debe
decirlo en la propia revisión.

**Backfill:** script que reproduce las 156 ventas por par cliente/producto a través de EWMA y
escribe en los 26 ciclos `avg_interval_days`, `estimated_next_purchase`, `projection_method`
y `projection_confidence`. Para los 7 ciclos de una sola compra, `estimated_next_purchase`
pasa a NULL y la confianza a `insufficient`.

Es idempotente: recalcula desde el historial, no acumula.

## Verificación

TDD contra la Postgres desechable de `docker-compose.test.yml`. Lo que debe estar cubierto:

- **EWMA** reproduciendo a mano los números de la tabla de arriba: Cecy 19.3, Tía Lily 17.4.
- **`insufficient` no inventa fecha** — el test que habría atrapado el comportamiento actual.
- **FIFO cruzando lotes**, con costo ponderado correcto.
- **FIFO con stock pero sin lotes** — debe reportar inconsistencia, no inventar costo.
- **Precio habitual** detectándose con 3 ventas consistentes y **no** con 2.
- **Saldo de caja** contra una secuencia conocida de entradas, salidas y aportes.
- **Los 5 importadores actuales siguen funcionando** sin editarlos.

## Fuera de alcance

Endpoints nuevos. Croston. Google Calendar. El runtime del agente. Cablear los movimientos de
caja a las ventas y compras existentes.

**Una excepción explícita sobre los schemas Pydantic:** el alcance es "solo services", pero
`preview_sale` devuelve `SaleItemPreview` y el precio habitual tiene que ser visible ahí. Se
permite ese único campo, `is_habitual_price`. Ningún schema nuevo, ninguna otra adición.

## Incoherencias en las skills del agente

El `AGENTS.md` es la autoridad. Estas dos se corrigen al portar las skills en la pieza 2, no
aquí:

1. `registrar-venta/SKILL.md` dice `pagada = FALSE` por defecto y habla de "si el workbook no
   tiene todavía ese campo". El `AGENTS.md` sección 4 dice `pagada = true` por defecto. La
   skill quedó vieja.
2. `registrar-compra/SKILL.md` omite `medio_pago` y `monto_aporte_propio`, que el `AGENTS.md`
   sección 4 exige.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| El preview del panel cambia de precio sin aviso | `is_habitual_price` lo hace explícito en la respuesta |
| Los 7 ciclos sin fecha desaparecen del calendario | Grupo propio en `get_follow_ups`, no silencio |
| El backfill se corre dos veces | Es idempotente: recalcula desde el historial |
| El re-export de `SaleService` oculta el acoplamiento | Los tests importan desde los módulos nuevos, no desde el paquete |
