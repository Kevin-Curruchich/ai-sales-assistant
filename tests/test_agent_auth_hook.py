"""El hook de autenticacion del servidor LangGraph.

`resolve_user` existia desde la Task 6 y no tenia ningun llamador: nada en
la rama escribia una identidad en la corrida, asi que las tres herramientas
de escritura -- que la leen de `config["configurable"]` -- o reventaban con
`AgentAuthError` en cada escritura, o (si el panel ponia el dato el mismo)
firmaban con un valor que el llamador afirmaba y nadie validaba.

Lo que cierra el hueco es el hook de autenticacion del servidor. Verificado
leyendo el fuente de los paquetes que lo implementan, no infiriendolo:

- `langgraph_sdk.auth.Auth` (langgraph-sdk 0.2.9, instalado en este venv)
  es la clase; `@auth.authenticate` registra el handler en
  `auth._authenticate_handler`. Su docstring documenta la entrada de
  `langgraph.json`: `{"auth": {"path": "./file.py:instancia"}}`.
- `langgraph_cli.config` (0.4.32) lee esa clave y la exporta como
  `LANGGRAPH_AUTH` a la imagen del servidor.
- `langgraph_api.auth.custom.CustomAuthBackend` la carga y la corre en CADA
  request. Un handler SINCRONO esta soportado: si no es corutina lo envuelve
  en `run_in_threadpool` (custom.py:152-153) -- por eso este handler puede
  hacer I/O bloqueante (Firebase + Postgres) sin frenar el event loop.
- `langgraph_api.models.run` mete el resultado en el `configurable` de la
  corrida como `langgraph_auth_user` (el objeto) y `langgraph_auth_user_id`
  (`user.identity`).
- `langgraph_api.validation.RESERVED_CONFIGURABLE_KEYS` incluye esas dos
  claves: el schema de OpenAPI del servidor RECHAZA un request que intente
  ponerlas el mismo. Por eso son server-asserted y `user_id` a secas no lo
  era -- esa era exactamente la diferencia que faltaba.
"""

import json
from pathlib import Path

import pytest
from langgraph_sdk import Auth

import app.agent.auth_hook as auth_hook_module
from app.agent.auth import AgentAuthError
from app.agent.auth_hook import AUTH_USER_ID_KEY, auth

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_UID = "8f14e45f-ceea-467e-adc0-51203ce6f0a1"


@pytest.fixture
def firebase_accepts_everything(monkeypatch):
    monkeypatch.setattr(auth_hook_module, "initialize_firebase", lambda: None)
    monkeypatch.setattr(
        "app.agent.auth.verify_firebase_token",
        lambda _token: {"uid": VALID_UID, "email": "kevin@example.com"},
    )


def _handler():
    """El handler que `@auth.authenticate` registro, tal como lo busca el
    servidor (`auth._authenticate_handler`, custom.py:277)."""
    return auth._authenticate_handler


def test_the_auth_object_registers_an_authenticate_handler():
    """Sin esto el servidor se niega a arrancar: `_get_custom_auth_middleware`
    levanta un ValueError si `_authenticate_handler` es None."""
    assert isinstance(auth, Auth)
    assert _handler() is not None


def test_the_handler_is_synchronous_on_purpose():
    """`resolve_user` hace I/O bloqueante (Firebase + Postgres). El servidor
    envuelve un handler no-corutina en `run_in_threadpool`; declararlo
    `async` y bloquear adentro frenaria el event loop de todo el proceso."""
    import inspect

    assert not inspect.iscoroutinefunction(_handler())


def test_the_handler_only_asks_for_parameters_the_server_supports():
    """`langgraph_api.auth.custom.SUPPORTED_PARAMETERS` es la lista cerrada
    de lo que el servidor sabe inyectar; pedir otra cosa falla en vivo, no
    aca."""
    import inspect

    supported = {
        "request",
        "body",
        "user",
        "path",
        "method",
        "scopes",
        "path_params",
        "query_params",
        "headers",
        "authorization",
        "scope",
    }
    parameters = set(inspect.signature(_handler()).parameters)
    assert parameters <= supported
    assert "authorization" in parameters


def test_a_valid_bearer_token_resolves_to_the_local_user_id(db_session, firebase_accepts_everything):
    result = _handler()(authorization=f"Bearer {'t' * 20}")

    assert result["identity"] == VALID_UID
    assert result["is_authenticated"] is True


def test_the_identity_is_what_the_tools_read_as_the_signing_user(db_session, firebase_accepts_everything):
    """El contrato completo: `identity` -> `langgraph_auth_user_id` ->
    `user_id_from_config` -> la fila firmada."""
    import uuid

    from app.agent.session import user_id_from_config

    identity = _handler()(authorization=f"Bearer {'t' * 20}")["identity"]
    config = {"configurable": {AUTH_USER_ID_KEY: identity}}

    assert user_id_from_config(config) == uuid.UUID(VALID_UID)


def test_a_raw_token_without_the_bearer_scheme_also_works(db_session, firebase_accepts_everything):
    result = _handler()(authorization="t" * 20)

    assert result["identity"] == VALID_UID


def test_a_missing_authorization_header_is_a_401():
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        _handler()(authorization=None)

    assert excinfo.value.status_code == 401


def test_a_blank_authorization_header_is_a_401():
    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        _handler()(authorization="Bearer    ")

    assert excinfo.value.status_code == 401


def test_an_invalid_token_becomes_a_401_not_an_agent_auth_error(db_session, monkeypatch):
    """`resolve_user` convierte cualquier falla en `AgentAuthError`. El
    servidor no sabe que es eso: solo mapea a 401/403 su propia
    `Auth.exceptions.HTTPException` (custom.py:236-238); cualquier otra
    excepcion se loguea y se relanza como un 500. Un token vencido tiene que
    llegarle al panel como 401, no como "el agente se cayo"."""
    monkeypatch.setattr(auth_hook_module, "initialize_firebase", lambda: None)

    def reject(_token):
        raise ValueError("token vencido")

    monkeypatch.setattr("app.agent.auth.verify_firebase_token", reject)

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        _handler()(authorization="Bearer vencido")

    assert excinfo.value.status_code == 401
    assert not isinstance(excinfo.value, AgentAuthError)


def test_a_firebase_initialization_failure_is_a_401_not_a_500(db_session, monkeypatch):
    """El proceso del agente no corre el `lifespan` de FastAPI, asi que nadie
    mas llama a `initialize_firebase()`. Si esa llamada falla (credenciales
    ausentes en el servicio nuevo de Railway, por ejemplo) el request no
    puede autenticarse -- pero es un 401, no una excepcion cruda."""

    def explode():
        raise RuntimeError("sin credenciales de Firebase")

    monkeypatch.setattr(auth_hook_module, "initialize_firebase", explode)

    with pytest.raises(Auth.exceptions.HTTPException) as excinfo:
        _handler()(authorization="Bearer lo-que-sea")

    assert excinfo.value.status_code == 401


def test_langgraph_json_points_at_this_auth_object():
    """Sin la entrada en `langgraph.json` el hook no se carga y el servidor
    corre sin autenticacion ninguna."""
    config = json.loads((REPO_ROOT / "langgraph.json").read_text())

    assert "auth" in config, "langgraph.json no declara el hook de autenticacion"
    path = config["auth"]["path"]
    module_path, _, attribute = path.partition(":")
    assert attribute, f"auth.path debe tener la forma './archivo.py:nombre', es {path!r}"
    resolved = (REPO_ROOT / module_path).resolve()
    assert resolved.is_file(), f"auth.path apunta a un archivo que no existe: {resolved}"
    assert resolved == Path(auth_hook_module.__file__).resolve()
    assert getattr(auth_hook_module, attribute) is auth
