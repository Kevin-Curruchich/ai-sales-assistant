"""Las menciones y el comando del composer, como texto que el modelo lee.

El panel manda los ids de lo que la persona menciono con `@` y, si uso un
comando, la intencion (`/venta`, `/cobro`...). El endpoint los guarda en
`additional_kwargs` del `HumanMessage` (ver `AgentStreamRequest.mensaje_humano`)
y el texto queda tal cual lo escribio la persona.

El modelo no ve `additional_kwargs`: `langchain_anthropic` solo manda el
`content`. Por eso `prompt_con_fecha` pasa cada mensaje por `con_referencias`,
que devuelve una COPIA con un bloque de referencias al final del texto. Se
arma en cada turno y no se guarda: el checkpoint y el historial que ve el
panel siguen teniendo solo el texto de la persona.

Cada referencia se nombra con el parametro que la herramienta espera
(`cliente_id`, `producto_id`, `venta_id`) para que el modelo no tenga que
deducir a donde va cada id.
"""

from langchain_core.messages import BaseMessage, HumanMessage

_PARAMETRO = {
    "cliente": "cliente_id",
    "producto": "producto_id",
    "venta": "venta_id",
}

_INTENCION = {
    "venta": "registrar una venta",
    "compra": "registrar una compra de inventario",
    "cobro": "registrar el cobro de una venta a credito (registrar_cobro)",
    "caja": "registrar un movimiento de caja",
}


def _bloque(texto: str, comando: str | None, menciones: list[dict]) -> str:
    lineas = []
    if menciones:
        lineas.append(
            "Referencias del panel -- ids exactos de lo que la persona menciono "
            "con @. Usalos tal cual, sin buscarlos de nuevo:"
        )
        for mencion in menciones:
            etiqueta = texto[mencion["inicio"] : mencion["fin"]]
            parametro = _PARAMETRO[mencion["tipo"]]
            linea = f'- "{etiqueta}" = {mencion["tipo"]}, {parametro} {mencion["id"]}'
            if mencion["tipo"] == "venta":
                linea += " (el venta_id de registrar_cobro)"
            lineas.append(linea)
    if comando:
        lineas.append(
            f"Comando /{comando}: la persona quiere {_INTENCION[comando]}. Es una "
            "pista de intencion; si el mensaje pide otra cosa, segui el mensaje."
        )
    return "\n".join(lineas)


def con_referencias(mensaje: BaseMessage) -> BaseMessage:
    """`mensaje` con su bloque de referencias, o el mismo objeto si no tiene."""
    if not isinstance(mensaje, HumanMessage):
        return mensaje
    comando = mensaje.additional_kwargs.get("comando")
    menciones = mensaje.additional_kwargs.get("menciones") or []
    if not comando and not menciones:
        return mensaje

    texto = mensaje.text
    return mensaje.model_copy(
        update={
            "content": f"{texto}\n\n---\n{_bloque(texto, comando, menciones)}",
            "additional_kwargs": {},
        }
    )
