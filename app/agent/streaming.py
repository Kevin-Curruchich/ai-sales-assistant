"""Traduce lo que `graph.astream(...)` emite a los eventos que el panel
entiende.

El panel no habla LangGraph: habla cinco eventos, `token` / `herramienta` /
`confirmacion` / `fin` / `error`, cada uno `{"event": str, "data": dict}`.
Esta es la unica capa que conoce las dos formas.

`stream_mode=["messages", "updates"]` -- con `stream_mode` como LISTA,
`astream` rinde tuplas `(modo, chunk)`, no chunks pelados: iterar cada item
como si fuera un chunk directo lee basura. Los dos modos dan cosas
distintas y no se pisan: los chunks de `"messages"` (tuplas
`(mensaje, metadata)`) dan el texto que el modelo va generando -- de ahi
salen los `token`. Los chunks de `"updates"` (un dict `nodo -> salida`) dan,
por un lado, las tool_calls que el nodo del modelo acaba de pedir -- de ahi
salen los `herramienta` -- y por otro, bajo la clave especial
`"__interrupt__"`, la pausa de un `interrupt()` -- de ahi sale la
`confirmacion`.

Esta capa NO FIRMA nada. La `huella` que viaja en el payload de
`interrupt()` (ver `app/agent/tools/write.py`) ya llega firmada por la
herramienta de escritura que la construyo, con `firmar()` de
`app/agent/signing.py`. Esta capa la pasa tal cual dentro de la
`confirmacion`: si firmara ella misma, estaria firmando datos que nunca
valido -- exactamente lo que la huella existe para impedir. Por eso este
modulo no importa `app.agent.signing` en absoluto.

Contrato del generador: siempre termina, y termina en un evento terminal.
Tres finales posibles -- `fin` con `estado: "completo"` (la corrida llego al
final sin pausarse), `fin` con `estado: "pausado"` (hubo un `interrupt()` y
no hay mas nada que traducir despues), o `error` como ultimo evento, sin
`fin` despues. Un stream que no cierra deja al panel esperando para
siempre; uno que sigue vivo despues de un terminal manda eventos que ya
nadie deberia leer.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)


async def eventos_sse(graph, entrada, config) -> AsyncIterator[dict]:
    interrumpido = False

    try:
        async for modo, chunk in graph.astream(
            entrada, config, stream_mode=["messages", "updates"]
        ):
            if modo == "messages":
                mensaje, _metadata = chunk
                # `.text`, no `.content`: el grafo real bindea las siete
                # herramientas (`app/agent/graph.py`), y con herramientas
                # bindeadas `langchain_anthropic` NUNCA coerciona `content` a
                # `str` (`coerce_content_to_string` en
                # `chat_models.py` es `not _tools_in_params(...) and ...`, y
                # `_tools_in_params` es siempre verdadero aca) -- `content` es
                # una lista de bloques (`text`, `tool_use`,
                # `input_json_delta`, `thinking`). `.text` extrae solo el
                # texto de esos bloques y da `""` para los que no son texto
                # (`tool_use`/`input_json_delta`), asi que el filtro de abajo
                # los descarta solos: sin esto, el JSON parcial de los
                # argumentos de una tool_call se le escribia al usuario como
                # si fuera texto del asistente.
                texto = mensaje.text if isinstance(mensaje, AIMessage) else ""
                if texto:
                    yield {"event": "token", "data": {"texto": texto}}
                continue

            if modo != "updates" or not isinstance(chunk, dict):
                continue

            interrupciones = chunk.get("__interrupt__")
            if interrupciones:
                interrumpido = True
                for interrupcion in interrupciones:
                    yield {"event": "confirmacion", "data": interrupcion.value}
                continue

            for salida in chunk.values():
                for mensaje in (salida or {}).get("messages", []):
                    for llamada in getattr(mensaje, "tool_calls", None) or []:
                        # `{"nombre", "estado"}`, no `{"nombre", "argumentos"}`:
                        # es el contrato que
                        # `docs/superpowers/specs/2026-09-29-agente-en-proceso-design.md`
                        # le promete al panel. Los argumentos de una escritura
                        # ya viajan en la `confirmacion`; mandarlos tambien
                        # aca es un dato de mas (y para `registrar_venta`, una
                        # lista de items entera) que nadie pidio.
                        yield {
                            "event": "herramienta",
                            "data": {"nombre": llamada.get("name"), "estado": "llamando"},
                        }
    except Exception as exc:
        # `Exception`, nunca `BaseException`. `asyncio.CancelledError` hereda
        # de `BaseException`, no de `Exception` (verificado en este venv:
        # `asyncio.CancelledError.__mro__` es
        # `(CancelledError, BaseException, object)`). Si este `except` fuera
        # `BaseException`, se tragaria la cancelacion del panel: emitiria un
        # `error` y el generador seguiria viviendo en vez de morir, dejando la
        # corrida gastando modelo contra un cliente que ya se fue (Task 6,
        # `test_a_disconnected_client_cancels_the_run`). Con `except
        # Exception`, una `CancelledError` no entra aca y sigue de largo sin
        # que nadie la interfiera. Si algun camino de arriba llegara a
        # atraparla igual, hay que re-lanzarla -- nunca convertirla en un
        # evento `error`. No lo cambies a `BaseException`.
        #
        # `logger.exception` deja el traceback completo en el log del
        # servidor -- las excepciones reales nacen en herramientas que pegan
        # contra Postgres via `agent_session()`, y un `str(exc)` crudo de
        # psycopg/SQLAlchemy arrastra SQL y datos de conexion hasta el panel
        # ademas de perder el traceback entero. El mensaje que sale al panel
        # es acotado a proposito: el nombre del tipo de excepcion, sin sus
        # internos.
        logger.exception("La corrida del agente termino con una excepcion")
        yield {
            "event": "error",
            "data": {"mensaje": f"Ocurrio un error inesperado ({type(exc).__name__})."},
        }
        return

    yield {"event": "fin", "data": {"estado": "pausado" if interrumpido else "completo"}}
