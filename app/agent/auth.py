"""Autenticacion para el proceso del agente.

No importa FastAPI: `get_current_user` (en `app/api/dependencies.py`) esta
atado a `Depends` y este proceso no tiene aplicacion web.  Lo reutilizable de
esa funcion son las dos piezas de abajo, compuestas aqui sin FastAPI de por
medio.
"""

from sqlalchemy.orm import Session

from app.core.security import verify_firebase_token
from app.models.user import User
from app.services.user_service import UserService


class AgentAuthError(Exception):
    """El token no sirve. El hilo se cierra con un error claro, no escribe."""


def resolve_user(token: str, db: Session) -> User:
    """Valida un token de Firebase y devuelve (o crea) el `User` local.

    Levanta `AgentAuthError` si el token no es valido -- nunca devuelve None.

    `UserService.get_or_create_from_firebase` importa FastAPI y envuelve
    cualquier falla en un `HTTPException`.  Ese servicio es el camino de
    autenticacion en vivo de la app web y esta fuera del alcance de esta
    tarea, asi que no se toca -- pero su excepcion no puede fugarse a un
    proceso sin capa web.  Se contiene aca, en el borde.
    """
    try:
        decoded = verify_firebase_token(token)
    except Exception as exc:
        raise AgentAuthError("El token de Firebase no es valido o vencio") from exc

    try:
        return UserService(db).get_or_create_from_firebase(decoded)
    except Exception as exc:
        raise AgentAuthError(
            "No se pudo resolver el usuario local a partir del token de Firebase"
        ) from exc
