"""Traduce un snapshot del checkpointer a lo que el panel renderiza.

## Por que existe

`app/agent/streaming.py::eventos_sse` traduce una corrida EN VIVO. Este modulo
traduce el estado GUARDADO. Son la misma informacion para dos momentos
distintos: lo que la persona ve mientras el agente trabaja, y lo que el panel
tiene que volver a pintar cuando alguien recarga la pagina.

Sin esto, un refresh con una confirmacion abierta dejaba la conversacion
inservible: el panel no sabia que habia una tarjeta pendiente, y sobre todo no
podia aprobarla -- la huella viajo una sola vez, en el evento `confirmacion` de
ese turno, y se fue con el estado del navegador.

## Por que es una funcion pura, y aparte de `streaming.py`

`eventos_sse` es un generador asincrono sobre `graph.astream`. Esto es una
funcion sincronica sobre un valor ya leido: quien llama hace el
`await graph.aget_state(config)` y pasa el resultado. Asi se prueba sin grafo,
sin base y sin HTTP.

El riesgo de tenerlos separados es la DERIVA: si uno cambia como se llama la
actividad de herramienta, o de donde saca el texto del asistente, el historial
deja de coincidir con lo que la persona acababa de ver en vivo. Eso no lo agarra
ningun test de cada lado por separado, asi que lo fija uno que compara las dos
salidas para el mismo contenido:
`tests/test_agent_historial.py::test_the_history_and_the_live_stream_speak_the_same_vocabulary`.
Es una red mas honesta que juntar los dos modulos en un archivo por proximidad.
"""

from langchain_core.messages import AIMessage, HumanMessage


def _payload(valor) -> dict:
    """El payload de un `interrupt()` como dict, para poder ponerle el
    `interrupt_id` al lado de sus claves.

    Mismo criterio que `_payload` en `app/agent/streaming.py`, y a proposito:
    las dos salidas tienen que tener la misma forma para que el panel use un
    solo renderer. Un valor que no sea dict (una herramienta futura que
    interrumpa con un string) viaja bajo `valor` en vez de hacer reventar la
    traduccion entera -- el `interrupt_id` sigue llegando, asi que el panel
    siempre puede responder la pausa.
    """
    return dict(valor) if isinstance(valor, dict) else {"valor": valor}


def _mensajes(guardados) -> list[dict]:
    """La conversacion, con la actividad de herramienta en su lugar.

    Reglas, y por que cada una:

    - `HumanMessage` -> `usuario`. Lo que la persona escribio.
    - Los `tool_calls` de un `AIMessage` -> un `herramienta` por llamada, en la
      posicion donde el modelo las pidio y en su orden. Van ANTES del texto del
      mismo mensaje porque el modelo pide la herramienta y recien despues, con
      el resultado, escribe. Con `version="v2"` las varias tool_calls de un
      turno viajan todas en UN `AIMessage`, que es el mismo motivo por el que
      `eventos_sse` las lee del canal `updates`.
    - El texto de un `AIMessage` -> `asistente`, via `.text` y NO `.content`:
      con las diez herramientas bindeadas, `langchain_anthropic` nunca
      coerciona `content` a `str`, asi que es una lista de bloques. `.text`
      extrae solo los bloques de texto. Sin eso, el JSON parcial de los
      argumentos de una tool_call terminaria en el campo `texto` como si fuera
      algo que el agente le dijo a la persona -- el bug que el evento `token`
      ya tuvo.
    - Texto vacio -> no es un mensaje. Un `AIMessage` que solo pide
      herramientas no tiene nada que escribir, y una burbuja vacia es ruido.
      Misma regla que el evento `token`.
    - `ToolMessage` y `SystemMessage` -> se filtran. El primero es resultado
      interno; el segundo es el prompt. Ninguno es algo que alguien dijo.
    """
    salida: list[dict] = []
    for mensaje in guardados:
        if isinstance(mensaje, HumanMessage):
            salida.append({"rol": "usuario", "texto": mensaje.text})
            continue

        if not isinstance(mensaje, AIMessage):
            continue

        for llamada in getattr(mensaje, "tool_calls", None) or []:
            salida.append({"rol": "herramienta", "nombre": llamada["name"]})

        if mensaje.text:
            salida.append({"rol": "asistente", "texto": mensaje.text})

    return salida


def _confirmaciones_pendientes(tareas) -> list[dict]:
    """Las confirmaciones que este hilo tiene abiertas AHORA.

    No se lee `snapshot.interrupts`: esa lista sigue trayendo las pausas que YA
    se respondieron mientras el paso no termine (verificado contra langgraph
    1.0.3 -- despues de reanudar una de dos hermanas, la respondida sigue
    apareciendo ahi). Lo que las distingue es la tarea: la respondida tiene
    `result`, la que sigue esperando lo tiene en `None`. Es el mismo criterio
    que `_pausas_pendientes` en `app/api/v1/endpoints/agent.py`, que lo usa para
    validar el `interrupt_id` de un resume.

    Si esto leyera la lista equivocada, el panel mostraria al recargar una
    tarjeta que la persona ya aprobo, y aprobarla de nuevo daria 409.

    La forma de cada una es la del `data` del evento `confirmacion`: el
    `interrupt_id` mezclado con las claves del payload, no envuelto.
    """
    return [
        {**_payload(interrupcion.value), "interrupt_id": interrupcion.id}
        for tarea in tareas
        if tarea.result is None
        for interrupcion in tarea.interrupts
    ]


def traducir_estado(snapshot) -> dict:
    """`{"mensajes": [...], "confirmaciones_pendientes": [...]}`.

    `snapshot` es lo que devuelve `graph.aget_state(config)`. Un hilo recien
    creado no tiene checkpoint, y ahi `values` viene vacio: eso es un hilo sin
    mensajes, no un error, asi que las dos listas salen vacias en vez de
    levantar un `KeyError` que el panel veria como un 500 por un hilo nuevo.
    """
    return {
        "mensajes": _mensajes((snapshot.values or {}).get("messages", [])),
        "confirmaciones_pendientes": _confirmaciones_pendientes(snapshot.tasks or []),
    }
