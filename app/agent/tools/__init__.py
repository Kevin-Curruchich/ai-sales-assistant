"""Herramientas del agente conversacional.

`app/agent/tools/read.py` trae las seis de solo lectura.
`app/agent/tools/write.py` (Task 8) trae las cuatro que escriben, cada una
detras de un `interrupt()` de confirmacion humana.
"""

from app.agent.tools.read import (
    buscar_cliente,
    buscar_producto,
    consultar_caja,
    consultar_seguimiento,
    consultar_ventas,
    previsualizar_venta,
)
from app.agent.tools.write import (
    registrar_cobro,
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
    consultar_ventas,
]
WRITE_TOOLS = [registrar_venta, registrar_compra, registrar_movimiento_caja, registrar_cobro]
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS

__all__ = [
    "buscar_cliente",
    "buscar_producto",
    "consultar_caja",
    "consultar_seguimiento",
    "consultar_ventas",
    "previsualizar_venta",
    "registrar_cobro",
    "registrar_compra",
    "registrar_movimiento_caja",
    "registrar_venta",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "ALL_TOOLS",
]
