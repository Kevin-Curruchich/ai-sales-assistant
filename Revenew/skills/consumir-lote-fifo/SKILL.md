---
name: consumir-lote-fifo
description: Lógica pura de costeo FIFO: dado un producto y una cantidad a vender, determina qué lote(s) se consumen y el costo unitario aplicado. Es una skill de cálculo, separada de registrar_venta para poder probarla y depurarla de forma aislada.
---

**input:** `producto_id: string`, `cantidad: number`

**lógica:**

1. Ordena los lotes del producto por `fecha_compra` ascendente, filtrando `cantidad_restante > 0`.
2. Consume del lote más antiguo hasta agotar `cantidad`. Si un lote no alcanza, continúa con el siguiente.
3. Si la venta cruza más de un lote, calcula `costo_unitario_aplicado` como el costo ponderado por cantidad tomada de cada lote.
4. Devuelve la lista de `(lote_id, cantidad_tomada, costo_unitario)` usados, para que `registrar_venta` pueda descontarlos y dejar trazabilidad.

**output:** `lote_id` (o lista si cruza lotes), `costo_unitario_aplicado`, detalle por lote si aplica.