---
name: verificar-lote-disponible
description: Confirma que exista al menos un lote con cantidad_restante > 0 para el producto antes de procesar una venta. Úsala como paso previo obligatorio dentro de registrar_venta, nunca de forma aislada por Kevin.
---

**input:** `producto_id: string`, `cantidad_requerida: number`

**lógica:**

1. Suma `cantidad_restante` de todos los lotes de ese producto.
2. Si el total es menor a `cantidad_requerida`, o si `stock_actual` del producto es mayor a 0 pero no hay ningún lote con existencia (caso de stock inicial sin migrar), **no continúes con la venta** — reporta la inconsistencia a Kevin y pide que cree o corrija el lote correspondiente.

**output:** `disponible: boolean`, detalle del problema si `false`