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
