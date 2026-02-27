# Revenew

### Objetivo

Construir un panel administrativo en **React + TypeScript** para registrar ventas por cliente (con **múltiples ítems por venta**) y calcular una **estimación de próxima compra** basada en el historial de compras, de forma que el negocio pueda priorizar seguimiento (lista + calendario).

### Problema / contexto

Muchos negocios venden de forma recurrente y necesitan responder rápido:

- ¿Cuándo fue la última compra de este cliente?
- ¿Cuándo es probable que vuelva a comprar?
- ¿A quién debo dar seguimiento esta semana?

### Usuario objetivo

Personas que administran ventas recurrentes (B2C/B2B) y requieren control de historial + seguimiento sin depender de hojas de cálculo.

### Alcance MVP (5 capacidades)

**Incluye:**

- **Clientes (CRUD)**: datos básicos + contacto.
- **Ventas (CRUD)** con estructura:
  - venta: fecha, cliente, total.
  - ítems: SKU o texto libre, cantidad, precio unitario (precio histórico), subtotal.
- **Estimación por venta**: cada venta guarda su `estimatedNextPurchaseDate` (calculada desde backend).
- **Vista Seguimiento (lista)**: clientes ordenados por próxima compra más cercana, con filtros (7/14/30 días, atrasados).
- **Vista Calendario**: calendario con eventos de próximas compras estimadas.
