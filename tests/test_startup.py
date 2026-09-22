from pathlib import Path

import app.core.database as database
import app.main as main


def test_schema_bootstrap_helpers_are_gone():
    """Alembic es el unico dueno del esquema."""
    assert not hasattr(database, "prepare_schema_bootstrap")
    assert not hasattr(database, "ensure_schema_compatibility")


def test_startup_does_not_create_tables():
    source = Path(main.__file__).read_text()
    assert "create_all" not in source, "El arranque no debe crear tablas; eso es de Alembic"
    assert "prepare_schema_bootstrap" not in source
    assert "ensure_schema_compatibility" not in source


def test_health_endpoint_still_responds():
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "degraded"}
