---
name: detectar-precio-habitual
description: Revisa el historial de ventas de un cliente/producto para detectar
  si tiene un precio o descuento consistente distinto al estándar, y lo sugiere
  sin que Kevin tenga que repetirlo. Úsala dentro de registrar_venta antes de
  proponer precio, cuando Kevin no dio uno explícito.
---

**input:** `cliente_id: string`, `producto_id: string`

**lógica:**

1. Toma las últimas 3 ventas de ese par en `Ventas`.
2. Si `margen_real` es consistente entre ellas (diferencia ≤ Q2) pero distinto de `margen_base`, ese es el "margen habitual" del cliente.
3. Precio sugerido = `costo_unitario_aplicado del lote actual + margen habitual del cliente`.
4. Si menos de 3 ventas históricas del par, no hay patrón suficiente — devuelve `precio_sugerido` estándar del lote.
5. Un cambio puntual de precio no reescribe el patrón — se necesita que se repita en 2+ ventas nuevas consecutivas para actualizarlo.

**output:** `precio_sugerido: number`, `es_patron_detectado: boolean`, para que el agente lo mencione explícitamente al pedir confirmación en Slack.