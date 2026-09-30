from pathlib import Path

import app.core.database as database
import app.main as main


def test_schema_bootstrap_helpers_are_gone():
    """Alembic es el unico dueno del esquema."""
    assert not hasattr(database, "prepare_schema_bootstrap")
    assert not hasattr(database, "ensure_schema_compatibility")


def test_startup_does_not_create_tables():
    """Chequeo barato: ninguna de las llamadas viejas quedo pegada literalmente
    en main.py. No es la garantia real -- ver test_lifespan_never_calls_create_all,
    que asegura el comportamiento sin importar por donde se lo invoque."""
    source = Path(main.__file__).read_text()
    assert "create_all" not in source, "El arranque no debe crear tablas; eso es de Alembic"
    assert "prepare_schema_bootstrap" not in source
    assert "ensure_schema_compatibility" not in source


def test_lifespan_never_calls_create_all(monkeypatch):
    """Guarda de comportamiento: el lifespan no debe invocar create_all, sin
    importar la ruta -- directa, aliaseada, o a traves de un modulo que todavia
    no existe. Un grep de texto sobre main.py no detecta ninguna de esas rutas;
    parchear el metodo real si lo hace."""
    from fastapi.testclient import TestClient
    from app.core.database import Base

    calls = []
    monkeypatch.setattr(
        Base.metadata, "create_all", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    with TestClient(main.app):
        pass

    assert calls == [], "Base.metadata.create_all fue invocado durante el lifespan"


def test_health_endpoint_still_responds():
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "degraded"}


def _sin_construir_el_grafo(monkeypatch):
    """Revienta si el lifespan intenta construir el grafo -- es lo que estos
    tests tienen que demostrar que NO pasa, y una llamada real abriria un pool
    de Postgres contra el puerto de los tests."""

    def _no(*_args, **_kwargs):
        raise AssertionError("el lifespan construyo el grafo sin sus variables de entorno")

    monkeypatch.setattr("app.main.build_async_checkpointer", _no)
    monkeypatch.setattr("app.main.build_graph", _no)


def test_a_missing_agent_env_var_leaves_the_agent_unavailable_instead_of_green(monkeypatch):
    """Sin `ANTHROPIC_API_KEY`, `ChatAnthropic` se construye igual (la
    validacion es al llamar, no al instanciar): el grafo se armaba,
    `startup_issues` quedaba vacio, `/health` devolvia `ok`, el despliegue de
    Railway quedaba verde -- y cada turno del agente moria en el evento
    `error` generico, con la causa solo en el log. Lo mismo, y mas callado,
    con `AGENT_HUELLA_SECRET`, que nadie miraba en ninguna parte."""
    from fastapi.testclient import TestClient

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_HUELLA_SECRET", raising=False)
    _sin_construir_el_grafo(monkeypatch)

    with TestClient(main.app) as client:
        cuerpo = client.get("/health").json()
        assert main.app.state.agent_graph is None
    assert cuerpo["status"] == "degraded"
    assert "agent_api_key_missing" in cuerpo["issues"]
    assert "agent_huella_secret_missing" in cuerpo["issues"]


def test_an_empty_agent_env_var_counts_as_missing(monkeypatch):
    """`ANTHROPIC_API_KEY=` en un panel de Railway es un error de dedo mas
    probable que la variable sin definir, y las dos fallan igual."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")

    assert "ANTHROPIC_API_KEY" in main.missing_agent_env()
