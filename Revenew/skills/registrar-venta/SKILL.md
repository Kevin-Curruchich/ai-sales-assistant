---
name: registrar-venta
description: Use when the user asks to register a sale in Revenew, especially when FIFO cost, stock updates, margins, or paid vs unpaid status must be captured.
---

# Registrar venta

Use this skill for the Revenew sales workflow.

## Core behavior

- Identify the client, product, quantity, date, and price from the request.
- Use FIFO: consume the oldest lot with remaining quantity.
- If a sale spans more than one lot, compute the weighted applied cost and say so.
- Update `Ventas`, `Lotes`, and `Productos` consistently.
- Keep the confirmation short and operational: client, product, amount charged, FIFO cost, real margin, and any extra margin if relevant.

## Payment status

- Sales are **paid by default**: `pagada = TRUE` and `fecha_pago` = the sale date.
- Set `pagada = FALSE` only when Kevin says the sale is pending or on credit. Leave `fecha_pago` blank in that case.
- If Kevin names a different payment date ("me paga la otra semana"), use the date he gives.
- `medio_pago` defaults to `efectivo`. The accepted values are `efectivo` and `transferencia`.
- Register the cash entry **only** when `pagada = TRUE`. An unpaid sale moves no cash until it is collected — see `registrar-movimiento-caja`.

## Gotchas

- Never guess a FIFO cost when a product has stock but no usable lot; ask Kevin to create the missing lot first.
- If the request is missing price, quantity, or client, ask only for the missing sales detail.
- For `Cilindro de gas propano`, stock counts *filled* cylinders only; when a customer returns an empty cylinder, do *not* increase `stock_actual`.
