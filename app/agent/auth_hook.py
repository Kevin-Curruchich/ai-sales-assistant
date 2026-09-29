"""El hook de autenticacion del servidor LangGraph.

Esto es lo que le da un dueño a cada escritura del agente. Sin este archivo,
`resolve_user` (Task 6) no tenia ningun llamador: nada escribia una
identidad en la corrida, asi que `user_id_from_config` -- el unico canal por
el que las tres herramientas de escritura saben quien firma -- o reventaba
con `AgentAuthError` en cada escritura, o leia un valor que el llamador
afirmaba y nadie validaba. El spec lo pedia desde el principio
(`docs/superpowers/specs/2026-09-24-agente-runtime-conversacional-design.md`,
"Autenticacion e identidad"): el panel manda su token de Firebase, el
servidor lo valida reusando lo que compone `app/api/dependencies.py`, y de
ahi sale el `user_id` que firma las filas.

## Como llega de aca a `config["configurable"]`

Verificado leyendo el fuente de los paquetes que lo implementan, no
infiriendolo de la documentacion -- este repo ya pago dos veces el costo de
suponer la semantica de una libreria (`interrupt()` y
`PostgresSaver.setup()`):

1. `langgraph_sdk.auth.Auth` (langgraph-sdk 0.2.9, en este venv) es la
   clase. `@auth.authenticate` guarda el handler en
   `auth._authenticate_handler`. Su propio docstring documenta la entrada de
   `langgraph.json`: `{"auth": {"path": "./archivo.py:instancia"}}`.
2. `langgraph_cli.config` (0.4.32) lee esa clave al construir la imagen y la
   exporta como la variable `LANGGRAPH_AUTH`.
3. `langgraph_api.auth.custom.CustomAuthBackend` la carga con
   `_load_auth_obj` y corre el handler en CADA request. Si el handler NO es
   una corutina lo envuelve en `run_in_threadpool` (custom.py:152-153) --
   por eso el de abajo es sincrono a proposito: hace I/O bloqueante
   (Firebase y Postgres) y declararlo `async` frenaria el event loop del
   proceso entero. El servidor solo inyecta los parametros de
   `SUPPORTED_PARAMETERS`; `authorization` (el header `Authorization` crudo,
   o `None`) es uno de ellos.
4. `langgraph_api.models.run` mete el resultado en el `configurable` de la
   corrida: `langgraph_auth_user` (el objeto normalizado) y
   `langgraph_auth_user_id` (`user.identity`).
5. `langgraph_api.validation.RESERVED_CONFIGURABLE_KEYS` incluye esas dos
   claves, y el servidor las SACA del request antes de mirarlo:
   `sanitize_reserved_keys` (`validation.py:164-170`) las borra del
   `config.configurable` entrante y loguea un warning -- no rechaza el
   request. Su docstring lo dice textual ("Instead of rejecting the request
   with a 422, silently remove the keys and log a warning"); el patron que
   SI rechazaria, `RESERVED_OR_NULL_OR_ESCAPED_PATTERN`
   (`validation.py:42`), esta definido y no se usa en ningun lado. El
   efecto para nosotros es el mismo y por partida doble: lo que el llamador
   mande se descarta a la entrada, y despues `models/run.py:277-281`
   escribe el valor real encima. Eso es lo que las hace afirmadas por el
   SERVIDOR y no por el llamador -- la diferencia exacta que le faltaba a
   un `user_id` suelto en `config.configurable`, que ni se descarta ni se
   pisa.

Las claves tambien son transitorias: `_checkpointer/_adapter.py` las lista
en `_TRANSIENT_CONFIGURABLE_KEYS`, asi que no se persisten en el
checkpoint. Se reinyectan en cada corrida, incluida la que reanuda una pausa
de `interrupt()` -- una aprobacion la firma quien la aprueba, con el token
de ESE request, no quien empezo la conversacion dias antes.

## Lo que este hook NO hace

Solo registra `@auth.authenticate`. No hay handlers de autorizacion
(`@auth.on`), asi que -- una vez autenticado -- cualquier usuario puede leer
y reanudar cualquier hilo del servidor. Para un negocio de un solo dueño con
un puñado de usuarios de confianza eso es aceptable y esta documentado en
`docs/agente.md`; aislar hilos por usuario es una pieza aparte, con su
propio criterio de producto sobre quien puede ver que conversacion.
"""

from langgraph_sdk import Auth

from app.agent.auth import AgentAuthError, resolve_user
from app.agent.session import agent_session
from app.core.security import initialize_firebase

#: La clave que el servidor pone en `config["configurable"]` con
#: `user.identity`. Reservada: el servidor la descarta si el llamador la
#: manda, y despues escribe la suya encima.
#: `app/agent/session.py` la lee por este nombre.
AUTH_USER_ID_KEY = "langgraph_auth_user_id"

BEARER_PREFIX = "bearer "

auth = Auth()


def _token_from_header(authorization: str | None) -> str:
    """El token crudo de un header `Authorization: Bearer <token>`.

    Acepta tambien el token pelado: el panel usa `useStream` del SDK de JS,
    que arma el header el mismo, pero un cliente de prueba (curl, el Studio)
    puede mandarlo sin esquema y rechazarlo ahi no protege nada -- la
    validacion de verdad la hace Firebase abajo.
    """
    if not authorization or not authorization.strip():
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Falta el header Authorization con el token de Firebase",
        )

    token = authorization.strip()
    if token.lower().startswith(BEARER_PREFIX):
        token = token[len(BEARER_PREFIX) :].strip()

    if not token:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="El header Authorization no trae ningun token",
        )
    return token


@auth.authenticate
def authenticate(authorization: str | None) -> Auth.types.MinimalUserDict:
    """Valida el token de Firebase del panel y devuelve quien firma.

    Sincrono a proposito -- ver el punto 3 del docstring del modulo.

    `identity` es el UUID del `User` LOCAL (`users.id`), en texto: es lo que
    `langgraph_api` copia a `langgraph_auth_user_id` y lo que
    `user_id_from_config` convierte en el `user_id` con el que
    `SaleService.create` y `PurchaseService.create` firman las filas. No es
    el uid de Firebase por casualidad: `resolve_user` lo resuelve (o lo crea)
    contra la tabla local, que es de donde tiene que salir una FK.
    """
    token = _token_from_header(authorization)

    try:
        # El proceso del agente no corre el `lifespan` de FastAPI
        # (`app/main.py`), asi que nadie mas inicializa el SDK de Firebase.
        # Es idempotente (`if firebase_admin._apps: return`), asi que
        # llamarla en cada request no cuesta mas que un chequeo de dict.
        initialize_firebase()
    except Exception as exc:
        # Sin Firebase inicializado no se puede validar nada. Es un fallo de
        # configuracion del servicio, pero el request no esta autenticado:
        # 401, no una excepcion cruda que el servidor loguearia como 500.
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="No se pudo inicializar Firebase para validar el token",
        ) from exc

    with agent_session() as db:
        try:
            user = resolve_user(token, db)
        except AgentAuthError as exc:
            # `resolve_user` ya contiene cualquier falla (token vencido,
            # firma mala, el usuario local que no se pudo resolver) en
            # `AgentAuthError`. El servidor no sabe que es eso: solo mapea a
            # 401/403 su propia `Auth.exceptions.HTTPException`
            # (`custom.py:236-238`); cualquier otra excepcion la loguea y la
            # relanza, y al panel le llega un 500. Un token vencido tiene
            # que llegar como 401 para que el panel sepa refrescarlo.
            raise Auth.exceptions.HTTPException(status_code=401, detail=str(exc)) from exc

        return {
            "identity": str(user.id),
            "display_name": user.display_name or user.email,
            "is_authenticated": True,
        }
