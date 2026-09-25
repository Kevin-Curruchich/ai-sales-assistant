"""Sesion de base de datos y contexto de autorizacion para el agente.

El servidor LangGraph es un proceso aparte del de FastAPI: no hay `Depends`
que abra y cierre la sesion, asi que cada herramienta la administra.
"""

import uuid
from contextlib import contextmanager

from app.agent.auth import AgentAuthError
from app.core.database import SessionLocal


@contextmanager
def agent_session():
    """Una sesion de base por invocacion de herramienta."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


#: Lo que el servidor LangGraph pone en el `configurable` de cada corrida a
#: partir del handler de `app/agent/auth_hook.py`: `user.identity` en texto
#: (`langgraph_api/models/run.py`) y el objeto normalizado. Las dos estan en
#: `langgraph_api.validation.RESERVED_CONFIGURABLE_KEYS`, asi que el
#: validador del servidor RECHAZA un request que intente mandarlas: son
#: afirmadas por el servidor, no por quien llama.
AUTH_USER_ID_KEY = "langgraph_auth_user_id"
AUTH_USER_KEY = "langgraph_auth_user"


def _identity_from_user_object(user) -> str | None:
    """`user.identity` del objeto que el servidor deja en `configurable`.

    Es un `BaseUser` normalizado (`ProxyUser`/`SimpleUser`), no el dict que
    devolvio el handler, asi que se lee por atributo con un `.get()` de
    respaldo por si algun dia llega como mapa.
    """
    identity = getattr(user, "identity", None)
    if identity is None and isinstance(user, dict):
        identity = user.get("identity")
    return identity


def user_id_from_config(config: dict) -> uuid.UUID:
    """Quien firma la operacion, segun lo autentico el SERVIDOR.

    Lee `config["configurable"]["langgraph_auth_user_id"]` -- el valor que el
    servidor LangGraph derivo del token de Firebase que mando el panel, via
    el handler de `app/agent/auth_hook.py` (ahi esta documentada la cadena
    completa, leida del fuente de `langgraph-api`). Las herramientas de
    escritura (Task 8) lo usan para saber quien firma una venta o una compra
    sin que el modelo pueda inventar o falsificar ese dato: `config` es el
    `RunnableConfig` de LangGraph, invisible para el modelo.

    **No** lee un `user_id` suelto. Esa era la version anterior y era una
    identidad que el LLAMADOR afirmaba: `user_id` no esta en las claves
    reservadas del servidor, asi que cualquiera que alcanzara el puerto
    podia mandar el `user_id` que quisiera y escribir como quien quisiera.
    La clave de arriba si esta reservada -- un request que la traiga se
    rechaza -- y por eso es la unica que se acepta aca.
    """
    try:
        configurable = config["configurable"]
    except (KeyError, TypeError) as exc:
        raise AgentAuthError(
            f"config['configurable'] falta -- el servidor no autentico este "
            f"hilo antes de invocar la herramienta (se esperaba "
            f"'{AUTH_USER_ID_KEY}')"
        ) from exc

    raw_user_id = configurable.get(AUTH_USER_ID_KEY)
    if raw_user_id is None:
        raw_user_id = _identity_from_user_object(configurable.get(AUTH_USER_KEY))

    if raw_user_id is None:
        raise AgentAuthError(
            f"config['configurable']['{AUTH_USER_ID_KEY}'] falta -- el "
            "servidor no autentico este hilo antes de invocar la "
            "herramienta. Lo inyecta el hook de app/agent/auth_hook.py a "
            "partir del token de Firebase del panel; un 'user_id' puesto por "
            "el llamador no lo reemplaza, porque nadie lo valida."
        )

    if isinstance(raw_user_id, uuid.UUID):
        return raw_user_id

    try:
        return uuid.UUID(str(raw_user_id))
    except (ValueError, AttributeError) as exc:
        raise AgentAuthError(
            f"config['configurable']['{AUTH_USER_ID_KEY}'] no es un UUID "
            f"valido: {raw_user_id!r}"
        ) from exc
