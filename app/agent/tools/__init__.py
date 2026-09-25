"""Herramientas del agente conversacional.

`app/agent/tools/read.py` (Task 7) trae las cuatro de solo lectura.  Las tres
que escriben llegan en la Task 8.
"""

from app.agent.tools.read import (
    buscar_cliente,
    consultar_caja,
    consultar_seguimiento,
    previsualizar_venta,
)

READ_TOOLS = [buscar_cliente, previsualizar_venta, consultar_seguimiento, consultar_caja]

__all__ = [
    "buscar_cliente",
    "consultar_caja",
    "consultar_seguimiento",
    "previsualizar_venta",
    "READ_TOOLS",
]
