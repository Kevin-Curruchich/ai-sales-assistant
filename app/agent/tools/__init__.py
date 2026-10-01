"""Herramientas del agente conversacional.

`app/agent/tools/read.py` trae las cinco de solo lectura.
`app/agent/tools/write.py` (Task 8) trae las tres que escriben, cada una
detras de un `interrupt()` de confirmacion humana.
"""

from app.agent.tools.read import (
    buscar_cliente,
    buscar_producto,
    consultar_caja,
    consultar_seguimiento,
    previsualizar_venta,
)
from app.agent.tools.write import (
    registrar_compra,
    registrar_movimiento_caja,
    registrar_venta,
)

READ_TOOLS = [
    buscar_cliente,
    buscar_producto,
    previsualizar_venta,
    consultar_seguimiento,
    consultar_caja,
]
WRITE_TOOLS = [registrar_venta, registrar_compra, registrar_movimiento_caja]
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS

__all__ = [
    "buscar_cliente",
    "consultar_caja",
    "consultar_seguimiento",
    "previsualizar_venta",
    "registrar_compra",
    "registrar_movimiento_caja",
    "registrar_venta",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "ALL_TOOLS",
]
