"""Servicios del dominio de ventas.

Un modulo por concern, con los mismos limites que marcan las nueve skills del
agente Revenew: FIFO, precios, proyeccion y reportes.  `SaleService` los compone.
"""

from app.services.sales.orchestrator import SaleService

__all__ = ["SaleService"]
