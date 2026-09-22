from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
from app.core.config import settings
from app.core.security import initialize_firebase
from app.core.database import engine
from app.api.v1.router import api_router

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

    yield
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