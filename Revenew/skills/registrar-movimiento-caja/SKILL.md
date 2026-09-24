---
name: registrar-movimiento-caja
description: Crea una fila en Caja con el movimiento (entrada/salida) y
  recalcula saldo_acumulado. La invocan internamente registrar_venta,
  marcar_como_pagado y registrar_compra
---

`saldo_acumulado_nuevo = saldo_acumulado_anterior ± monto` (suma si `entrada`, resta si `salida`)