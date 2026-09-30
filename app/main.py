from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
from app.core.config import settings
from app.core.security import initialize_firebase
from app.core.database import engine
from app.api.v1.router import api_router
from app.agent.graph import build_async_checkpointer, build_graph, checkpointer_schema, default_model

# This import is redundant but harmless: app.api.v1.router (above) transitively
# imports every endpoint/service/repository, each of which imports these model
# classes directly, so all mapped classes are already registered by that line.
# Alembic doesn't depend on this import either -- alembic/env.py does its own
# `import app.models`.
from app.models import User, Customer, Product, Sale, SaleItem, SaleItemLotAllocation, CustomerProductCycle  # noqa: F401

logger = logging.getLogger("app.startup")
startup_issues: list[str] = []


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
    try:
        checkpointer = await build_async_checkpointer(settings.SQLALCHEMY_DATABASE_URI, checkpointer_schema())
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