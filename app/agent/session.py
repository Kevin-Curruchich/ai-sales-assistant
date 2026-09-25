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


def user_id_from_config(config: dict) -> uuid.UUID:
    """Quien firma la operacion, segun lo inyecto el runtime -- nunca el modelo.

    Lee `config["configurable"]["user_id"]`. Las herramientas de escritura
    (Task 8) lo usan para saber quien firma una venta o compra sin que el
    modelo pueda inventar o falsificar ese dato: `config` es el
    `RunnableConfig` de LangGraph, invisible para el modelo.
    """
    try:
        raw_user_id = config["configurable"]["user_id"]
    except (KeyError, TypeError) as exc:
        raise AgentAuthError(
            "config['configurable']['user_id'] falta -- el grafo no autentico "
            "este hilo antes de invocar la herramienta"
        ) from exc

    if raw_user_id is None:
        raise AgentAuthError(
            "config['configurable']['user_id'] es None -- el grafo no autentico "
            "este hilo antes de invocar la herramienta"
        )

    if isinstance(raw_user_id, uuid.UUID):
        return raw_user_id

    try:
        return uuid.UUID(str(raw_user_id))
    except (ValueError, AttributeError) as exc:
        raise AgentAuthError(
            f"config['configurable']['user_id'] no es un UUID valido: {raw_user_id!r}"
        ) from exc
