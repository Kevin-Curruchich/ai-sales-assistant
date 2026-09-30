"""El error de autenticacion del agente.

Aca vivia tambien `resolve_user(token, db)`, que validaba un token de Firebase
y devolvia el `User` local sin pasar por `Depends`. La llamaba el hook de
autenticacion del servidor de LangGraph, via `langgraph.json`; esta rama borro
los dos y la funcion quedo sin un solo call site en `app/`. Era, ademas, un
duplicado exacto de `get_current_user` (`app/api/dependencies.py`) con
otra excepcion alrededor -- dos caminos de autenticacion que alguien tenia que
mantener en sincronia -- y su docstring justificaba su existencia diciendo que
"este proceso no tiene aplicacion web", que dejo de ser cierto el dia que el
grafo empezo a servirse dentro de FastAPI. No hay trabajo planificado con la
forma "tengo un token de Firebase pero no tengo un request": el token siempre
llega en el header `Authorization` de un pedido, que es exactamente el caso que
`get_current_user` cubre.

Lo que queda es `AgentAuthError`, que si esta vivo: lo levanta
`user_id_from_config` en `app/agent/session.py` cuando una corrida llega sin
identidad en su `configurable`, y lo propagan las tres herramientas de
escritura. Sigue en este modulo -- y no mudado a `session.py`, su unico
importador de produccion -- porque varios tests lo importan de aca y mover un
simbolo al final de una rama es churn con riesgo y sin beneficio.
"""


class AgentAuthError(Exception):
    """El token no sirve. El hilo se cierra con un error claro, no escribe."""
