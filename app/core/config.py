from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    PROJECT_NAME: str = "Revenew API"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "production"
    FRONTEND_PRODUCTION_ORIGIN: str = "https://revenew-95e3a.web.app"
    BACKEND_CORS_ORIGINS: Optional[list[str]] = None
    
    # Database Configuration
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "revenew_db"
    POSTGRES_PORT: str = "5432"
    POSTGRES_SCHEMA: str = "db_dev"
    DATABASE_URL: Optional[str] = None
    
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        if self.DATABASE_URL:
            # Railway may provide postgres://; SQLAlchemy expects postgresql://
            return self.DATABASE_URL.replace("postgres://", "postgresql://", 1)
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def CORS_ORIGINS(self) -> list[str]:
        if self.BACKEND_CORS_ORIGINS:
            return self.BACKEND_CORS_ORIGINS

        if self.ENVIRONMENT.lower() == "development":
            return [
                "http://localhost:3000",
                "http://127.0.0.1:3000",
                "http://localhost:5173",
                "http://127.0.0.1:5173",
            ]

        return [self.FRONTEND_PRODUCTION_ORIGIN]
    
    # Firebase Configuration
    FIREBASE_CREDENTIALS_PATH: Optional[str] = None # Path to the firebase-adminsdk.json file
    
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

settings = Settings()
