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
    """Quien escribe la operacion, segun lo arma el invocador del agente.

    Lee `config["configurable"]["user_id"]` y **no lo verifica contra nada**:
    acepta cualquier UUID sintacticamente valido que reciba. No puede hacer
    mas -- no tiene forma de saber si ese UUID salio de una sesion
    autenticada. Quien lo garantiza es el invocador, y hoy el unico invocador
    es `stream_agent` (`app/api/v1/endpoints/agent.py`), que arma el
    `configurable` de cada corrida exclusivamente con `current_user.id`, la
    identidad que `get_current_user` resolvio del token de Firebase, y jamas
    con nada que venga en el cuerpo del pedido. **Ese endpoint es lo unico
    que se interpone entre un cliente malicioso y una venta escrita a nombre
    de otro usuario** -- ver su docstring, que es la version larga de esto.
    Si algun dia otra cosa invoca a este grafo, tiene la misma obligacion.

    (Segunda linea de defensa, mas abajo y mas tonta: `sales.user_id` y
    `purchases.user_id` son `ForeignKey("users.id")` NOT NULL, asi que un
    UUID valido pero inexistente no escribe nada -- revienta el INSERT. No
    reemplaza a la de arriba: un UUID de OTRO usuario real si escribiria.)

    Las herramientas de escritura usan este valor para saber a nombre de
    quien va una venta o una compra sin que el modelo pueda inventarlo:
    `config` es el `RunnableConfig` de LangGraph, invisible para el modelo.
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
