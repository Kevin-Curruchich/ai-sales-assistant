---
name: registrar-compra
description: Registra la compra de inventario a proveedor (reposición de stock) para un producto. Úsala cuando Kevin indique que compró unidades de gas o huevos para reponer stock, no cuando se trate de una venta a cliente.
---

**input:**

```json
producto_id: string
cantidad: number
costo_unitario: number
fecha: date (default: hoy)
medio_pago: string (default: efectivo — valores aceptados: efectivo, transferencia)
nota: string (opcional)
```

**lógica:**

1. Escribe la fila en `Compras`.
2. Busca `margen_objetivo` del producto en `Productos`.
3. Crea un nuevo lote en `Lotes`: `cantidad_restante = cantidad`, `costo_unitario`, `precio_sugerido = costo_unitario + margen_objetivo`.
4. Suma `cantidad` a `stock_actual` en `Productos`.
5. Si parte de la compra salió del bolsillo personal de Kevin, registra primero un
   movimiento `aporte_socio` en `Caja` por ese monto, con `registrar-movimiento-caja`.
   **No hay columna de aporte en `Compras`**: el aporte vive en `Caja` como movimiento,
   que es la única fuente de verdad. El `costo_unitario` del lote es siempre el completo —
   de dónde salió el dinero no cambia lo que costó el producto.
6. Registra una `salida` en `Caja` por el `total_costo` completo de la compra.

**output:** confirmación con `lote_id` creado, `stock_actual` actualizado y el efecto neto
en el saldo de caja.