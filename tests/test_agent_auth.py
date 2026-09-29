"""El proceso del agente valida Firebase sin depender de FastAPI."""

from pathlib import Path

import pytest

from app.agent.auth import AgentAuthError, resolve_user


def test_an_invalid_token_raises_instead_of_returning_none(db_session, monkeypatch):
    def reject(_token):
        raise ValueError("token vencido")

    monkeypatch.setattr("app.agent.auth.verify_firebase_token", reject)

    with pytest.raises(AgentAuthError):
        resolve_user("lo-que-sea", db_session)


def test_a_valid_token_resolves_to_a_user_row(db_session, monkeypatch):
    # users.id es un UUID nativo de Postgres (ver migracion inicial). En esta
    # app el uid de Firebase siempre es un UUID valido: UserService.signup()
    # crea el usuario de Firebase con uid=str(uuid.uuid4()), asi que el token
    # decodificado real nunca trae un uid con esta forma ("firebase-abc" haria
    # que get_or_create_from_firebase reviente con un InvalidTextRepresentation
    # de psycopg2 al consultar por id).
    valid_uid = "8f14e45f-ceea-467e-adc0-51203ce6f0a1"
    monkeypatch.setattr(
        "app.agent.auth.verify_firebase_token",
        lambda _token: {"uid": valid_uid, "email": "kevin@example.com"},
    )

    user = resolve_user("token-valido", db_session)
    assert user.id is not None
    assert str(user.id) == valid_uid


def test_a_user_service_failure_is_contained_as_agent_auth_error(db_session, monkeypatch):
    # UserService.get_or_create_from_firebase importa FastAPI y envuelve
    # cualquier falla en HTTPException -- ese servicio es el camino de
    # autenticacion en vivo de la app web y queda fuera del alcance de esta
    # tarea. resolve_user tiene que contener esa excepcion en el borde: un
    # proceso sin capa web nunca deberia recibir un HTTPException.
    monkeypatch.setattr(
        "app.agent.auth.verify_firebase_token",
        lambda _token: {"uid": "no-es-un-uuid", "email": "kevin@example.com"},
    )

    with pytest.raises(AgentAuthError):
        resolve_user("token-valido", db_session)


def test_this_modules_own_source_does_not_reference_fastapi():
    # Chequea el texto fuente de app/agent/auth.py, no el grafo de imports
    # transitivo: UserService (importado aca) si importa FastAPI hoy. Esta
    # asercion es honesta sobre lo que prueba -- que este modulo no agrega
    # una referencia directa a FastAPI, no que el proceso del agente este
    # libre de FastAPI en toda su cadena de dependencias.
    import app.agent.auth as mod
    source = Path(mod.__file__).read_text()
    assert "fastapi" not in source
