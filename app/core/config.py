from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    PROJECT_NAME: str = "Revenew API"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    
    # Database Configuration
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "revenew_db"
    POSTGRES_PORT: str = "5432"
    POSTGRES_SCHEMA: str = "db_dev"
    
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
    
    # Firebase Configuration
    FIREBASE_CREDENTIALS_PATH: Optional[str] = None # Path to the firebase-adminsdk.json file
    
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

settings = Settings()
