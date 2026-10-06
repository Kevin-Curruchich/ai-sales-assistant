import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.graph import (
    build_async_checkpointer,
    build_graph,
    checkpointer_schema,
    default_model,
)
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import engine
from app.core.security import initialize_firebase

# This import is redundant but harmless: app.api.v1.router (above) transitively
# imports every endpoint/service/repository, each of which imports these model
# classes directly, so all mapped classes are already registered by that line.
# Alembic doesn't depend on this import either -- alembic/env.py does its own
# `import app.models`.
from app.models import (  # noqa: F401
    Customer,
    CustomerProductCycle,
    Product,
    Sale,
    SaleItem,
    SaleItemLotAllocation,
    User,
)

logger = logging.getLogger("app.startup")
startup_issues: list[str] = []

# Variables de entorno sin las que el agente NO funciona, y el marcador que
# cada una deja en `startup_issues`.
#
# Ninguna de las dos falla al arrancar por su cuenta, y eso era el problema:
# `ChatAnthropic(model=...)` se CONSTRUYE sin clave (la validacion es al
# llamar, no al instanciar), asi que el grafo se armaba, `startup_issues`
# quedaba vacio, `/health` devolvia `{"status": "ok"}`, el despliegue quedaba
# verde -- y cada turno del agente moria en el evento `error` generico, con la
# causa solo en el log del servidor. `AGENT_HUELLA_SECRET` era peor: nadie lo
# miraba en ninguna parte, y su ausencia solo se manifestaba cuando alguien
# intentaba registrar algo. Un operador no tenia forma de ver la diferencia
# entre "el agente esta listo" y "el agente va a fallar en cada intento".
AGENT_REQUIRED_ENV = {
    "ANTHROPIC_API_KEY": "agent_api_key_missing",
    "AGENT_HUELLA_SECRET": "agent_huella_secret_missing",
}


def missing_agent_env() -> list[str]:
    """Las variables obligatorias del agente que faltan (o estan vacias).

    Vacia cuenta como ausente: `ANTHROPIC_API_KEY=""` en un panel de Railway
    es un error de dedo mas probable que la variable sin definir, y las dos
    fallan igual.
    """
    return [
        nombre
        for nombre in AGENT_REQUIRED_ENV
        if not os.environ.get(nombre, "").strip()
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    # El esquema lo gestiona Alembic desde entrypoint.sh, no el arranque.
    startup_issues.clear()

    try:
        initialize_firebase()
    except Exception:
        logger.exception("Firebase initialization failed during startup")
        startup_issues.append("firebase_init_failed")

    # Mismo patron que Firebase arriba: que el agente no arranque no debe
    # tumbar las ventas. Un Postgres caido en el momento del arranque degrada
    # `/health` en vez de matar la API entera (antes: la excepcion se
    # propagaba y la API ni siquiera llegaba a responder /health).
    checkpointer = None
    app.state.agent_graph = None

    # Mismo patron, un paso antes: sin estas variables el agente no puede
    # funcionar, asi que no se construye -- el endpoint `/stream` ya responde
    # 503 cuando `agent_graph` es `None`, y `/health` reporta cual falta. Es
    # ruidoso a proposito: un agente que arranca "bien" y falla en cada turno
    # es la peor de las dos opciones. Ver `AGENT_REQUIRED_ENV`.
    faltantes = missing_agent_env()
    if faltantes:
        logger.error(
            "El agente no se construyo: faltan variables de entorno obligatorias (%s). "
            "La API sigue sirviendo el resto de los endpoints; /stream responde 503.",
            ", ".join(faltantes),
        )
        startup_issues.extend(AGENT_REQUIRED_ENV[nombre] for nombre in faltantes)
    else:
        try:
            checkpointer = await build_async_checkpointer(
                settings.SQLALCHEMY_DATABASE_URI, checkpointer_schema()
            )
            app.state.agent_graph = build_graph(default_model(), checkpointer)
        except Exception:
            logger.exception("Agent graph initialization failed during startup")
            startup_issues.append("agent_graph_init_failed")
            # Si `build_async_checkpointer` broto, ya cerro lo que abrio y nunca
            # llegamos a asignar `checkpointer` aca. Si broto `build_graph` con
            # el checkpointer ya construido, esta es la unica referencia que
            # queda para cerrarlo -- sin esto, el pool quedaria abierto y sin
            # dueño.
            if checkpointer is not None:
                await checkpointer.conn.close()
                checkpointer = None

    try:
        yield
    finally:
        if checkpointer is not None:
            await checkpointer.conn.close()
        engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Mount API routes
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/health")
def health_check():
    if startup_issues:
        return {"status": "degraded", "issues": startup_issues}
    return {"status": "ok"}
