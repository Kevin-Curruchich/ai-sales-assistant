"""El .env lo comparten la API y el proceso del agente.

pydantic-settings prohibe claves extra por defecto, asi que agregar al .env una
variable que solo le importa al agente -- ANTHROPIC_API_KEY, LANGSMITH_* -- hacia
que la API entera dejara de arrancar.  docs/agente.md indica agregar cuatro.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_settings_ignores_variables_that_belong_to_the_agent(monkeypatch):
    for key, value in (
        ("ANTHROPIC_API_KEY", "sk-ant-lo-que-sea"),
        ("LANGSMITH_TRACING", "true"),
        ("LANGSMITH_API_KEY", "lsv2_lo-que-sea"),
        ("LANGSMITH_PROJECT", "revenew-agente"),
    ):
        monkeypatch.setenv(key, value)

    settings = Settings()

    # Las ignora, no las adopta: el runtime de LangGraph las lee del entorno.
    assert not hasattr(settings, "ANTHROPIC_API_KEY")
    assert settings.POSTGRES_SCHEMA


def test_settings_still_validates_the_keys_it_does_own(monkeypatch):
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", "no-es-una-lista-json")
    with pytest.raises(ValidationError):
        Settings()
