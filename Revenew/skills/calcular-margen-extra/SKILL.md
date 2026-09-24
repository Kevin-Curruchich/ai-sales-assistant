---
name: calcular-margen-extra
description: Dado el precio cobrado y el costo del lote aplicado, calcula el margen real y lo compara contra el margen objetivo del producto. Úsala dentro de registrar_venta, o cuando Kevin pregunte directamente "¿cuánto gané de más esta semana con el gas?"
---

**input:** `producto_id: string`, `precio_unitario: number`, `costo_unitario_aplicado: number`

**lógica:**

```json
margen_base  = margen_objetivo (de Productos)
margen_real  = precio_unitario − costo_unitario_aplicado
margen_extra = margen_real − margen_base
total        = precio_unitario × cantidad
```

**output:** `margen_base`, `margen_real`, `margen_extra`, `total`.