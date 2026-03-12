from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.core.config import settings
from app.core.security import initialize_firebase
from app.core.database import engine, Base
from app.api.v1.router import api_router

# Import all models so Base.metadata knows about them
from app.models import User, Customer, Product, Sale, SaleItem, CustomerProductCycle  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    # Startup: create tables and initialize Firebase
    Base.metadata.create_all(bind=engine)
    initialize_firebase()
    yield
    # Shutdown: dispose engine
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
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Mount API routes
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/health")
def health_check():
    return {"status": "ok"}