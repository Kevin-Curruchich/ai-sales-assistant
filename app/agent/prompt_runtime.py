"""Lo que el prompt del sistema necesita saber y cambia con cada turno.

`app/agent/prompt.py` es texto fijo: las reglas de negocio no cambian entre un
mensaje y el siguiente. Este modulo arma lo que SI cambia -- hoy, solo la fecha.

## Por que hace falta

`previsualizar_venta`, `registrar_venta`, `registrar_compra` y
`registrar_movimiento_caja` reciben `fecha` como parametro OBLIGATORIO, y hasta
ahora nada se la decia al modelo. El modelo la inventaba desde su entrenamiento:
una fecha pasada.

Eso no era cosmetico. `PurchaseRepository.get_fifo_available_lots` filtra los
lotes con `purchase.date <= fecha_de_la_venta`, asi que una fecha inventada
anterior a la compra deja el lote afuera. Medido contra produccion el
2026-10-01, con todo el inventario abierto el 2026-09-30 por el corte de mes:
con la fecha real el FIFO ve 1 lote con 6 unidades; con cualquier fecha anterior
al 30/09 ve 0 y 0. El agente respondia "el producto tiene stock pero esta sin
lotes" y no podia registrar una sola venta, mientras el panel -- que manda la
fecha real del navegador -- funcionaba sin problema.

## Por que un callable y no un string

`create_react_agent` acepta en `prompt` un `Callable[[state], mensajes]` que
invoca EN CADA TURNO. Eso es justo lo que hace falta: el grafo se construye una
sola vez, en el `lifespan` de `app/main.py`, y vive mientras viva el proceso. Una
fecha interpolada al construirlo se congelaria el dia del despliegue, y el bug
volveria a los pocos dias -- peor, porque "funcionaba cuando lo probamos".

## Por que la zona del negocio y no `date.today()`

Railway corre en UTC y Guatemala esta seis horas atras: entre las 18:00 y la
medianoche local, el servidor ya esta en el dia siguiente. `date.today()`
fecharia manana una venta de las siete de la tarde.
"""

from __future__ import annotations

from datetime import date, datetime

from langchain_core.messages import SystemMessage

from app.agent.prompt import SYSTEM_PROMPT
from app.core.datetime_utils import business_tz

_PLANTILLA_FECHA = """

## La fecha de hoy

Hoy es {hoy} (zona horaria del negocio).

Las herramientas que registran algo -- `previsualizar_venta`, `registrar_venta`,
`registrar_compra`, `registrar_movimiento_caja` -- piden `fecha` en formato
YYYY-MM-DD, y `registrar_cobro` pide `fecha_pago` en el mismo formato. Usa {hoy} salvo que la persona diga otra cosa ("la vendi ayer", "fue
el lunes"), y en ese caso calcula la fecha a partir de hoy.

No inventes la fecha ni la deduzcas de la conversacion: una fecha anterior a la
compra de un lote hace que el calculo FIFO no lo vea, y la venta se bloquea con
un mensaje que habla de falta de stock cuando en realidad hay."""


def _hoy() -> date:
    """El dia de hoy en la zona del negocio.

    Funcion propia y no una expresion suelta para que los tests puedan fijar el
    reloj sin parchear `datetime` en todo el modulo.
    """
    return datetime.now(business_tz()).date()


def prompt_con_fecha(state) -> list:
    """Lo que el modelo recibe en este turno: el prompt del sistema con la fecha
    de hoy, y despues la conversacion tal cual.

    `create_react_agent` pasa el estado completo del grafo y usa lo que esta
    funcion devuelva COMO LA LISTA ENTERA de mensajes, asi que la conversacion
    tiene que reenviarse: omitirla la borraria en cada turno.
    """
    mensajes = (state or {}).get("messages") or []
    sistema = SystemMessage(SYSTEM_PROMPT + _PLANTILLA_FECHA.format(hoy=_hoy().isoformat()))
    return [sistema, *mensajes]
