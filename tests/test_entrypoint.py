from pathlib import Path


def test_entrypoint_migrates_before_serving():
    script = Path("entrypoint.sh").read_text()
    assert "alembic upgrade head" in script
    upgrade_at = script.index("alembic upgrade head")
    serve_at = script.index("hypercorn")
    assert upgrade_at < serve_at, "Las migraciones van antes de levantar la app"
    assert "set -e" in script, "Un fallo de migracion debe abortar el arranque"


def test_alembic_is_a_declared_dependency():
    assert "alembic" in Path("requirements.txt").read_text().lower()


def test_dockerignore_does_not_exclude_migrations():
    ignored = Path(".dockerignore").read_text().splitlines()
    assert "alembic/" not in ignored
    assert "alembic" not in ignored
