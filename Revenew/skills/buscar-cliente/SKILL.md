---
name: buscar-cliente
description: Resuelve una referencia en lenguaje natural a un cliente ("la señora del cartón de huevos", "Don Tilo") al cliente_id exacto en la hoja Clientes. Úsala antes de cualquier registro cuando el cliente no se identificó con un ID exacto.
---

**input:** `texto_libre: string`

**lógica:** busca coincidencia por nombre en `Clientes`. Si hay ambigüedad (más de una coincidencia razonable) o no hay ninguna, no adivines — pide confirmación explícita a Kevin con las opciones encontradas.

**output:** `cliente_id` único, o lista de candidatos + pregunta de confirmación.