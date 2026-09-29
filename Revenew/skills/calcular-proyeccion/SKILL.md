---
name: calcular-proyeccion
description: Calcula o recalcula la fecha estimada de próxima compra de un
  cliente para un producto, usando promedio móvil ponderado (EWMA) o método de
  Croston. Úsala después de cada venta registrada, o cuando Kevin pregunte
  "¿cuándo compra de nuevo [cliente]?".
---

**input:** `cliente_id: string`, `producto_id: string`

**lógica:**

1. Si hay menos de 2 compras históricas del par, no proyectes — avisa que falta historial.
2. Si hay 4-5+ compras con intervalos irregulares, usa Croston (suaviza tamaño e intervalo de demanda por separado, α 0.1–0.2). Si no, usa EWMA por defecto (`α = 0.3`).
3. `fecha_proyectada = fecha_última_compra + intervalo_estimado`.
4. Si el cliente compró antes/después de lo proyectado la última vez, ajusta el intervalo estimado antes de proyectar el siguiente.
5. Escribe/actualiza la fila correspondiente en `Proyecciones` (incluye `metodo` usado).

**output:** `fecha_proyectada`, `metodo`, `confianza` (baja si hay poco historial).