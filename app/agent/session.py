"""Sesion de base de datos y contexto de autorizacion para el agente.

El agente corre dentro del mismo proceso FastAPI: no hay `Depends` de
FastAPI que abra y cierre la sesion para una tool call de LangGraph, asi que
cada herramienta la administra por su cuenta con `agent_session()`.
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


#: La clave bajo `config["configurable"]` donde el endpoint deja el
#: `user_id` que salio de `get_current_user`.
USER_ID_CONFIGURABLE_KEY = "user_id"


def user_id_from_config(config: dict) -> uuid.UUID:
    """Quien firma la operacion, segun lo arma el endpoint que invoca al agente.

    Lee `config["configurable"]["user_id"]`. El agente corre en el mismo
    proceso que FastAPI: es el endpoint el que arma el `configurable` de
    cada corrida a partir de `get_current_user`, no un cliente externo, asi
    que no hay nadie a quien un `user_id` suelto le permita falsificar la
    identidad. Las herramientas de escritura (Task 8) usan este valor para
    saber quien firma una venta o una compra sin que el modelo pueda
    inventarlo: `config` es el `RunnableConfig` de LangGraph, invisible para
    el modelo.
    """
    try:
        configurable = config["configurable"]
    except (KeyError, TypeError) as exc:
        raise AgentAuthError(
            f"config['configurable'] falta -- se esperaba "
            f"'{USER_ID_CONFIGURABLE_KEY}' puesto por el endpoint"
        ) from exc

    raw_user_id = configurable.get(USER_ID_CONFIGURABLE_KEY)

    if raw_user_id is None:
        raise AgentAuthError(
            f"config['configurable']['{USER_ID_CONFIGURABLE_KEY}'] falta -- el "
            "endpoint no armo el `configurable` con la identidad de "
            "`get_current_user` antes de invocar al agente."
        )

    if isinstance(raw_user_id, uuid.UUID):
        return raw_user_id

    try:
        return uuid.UUID(str(raw_user_id))
    except (ValueError, AttributeError) as exc:
        raise AgentAuthError(
            f"config['configurable']['{USER_ID_CONFIGURABLE_KEY}'] no es un UUID "
            f"valido: {raw_user_id!r}"
        ) from exc
