# app/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Supabase
    supabase_url:              str
    supabase_anon_key:         str
    supabase_service_role_key: str

    database_url:              str

    # Monday.com
    monday_client_id:      str 
    monday_client_secret:  str  
    monday_signing_secret: str
    app_id: int

    # App
    app_env:  str 
    app_port: int 
    app_base_url: str

    monday_api_url: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding = "utf-8",
        extra="ignore",
        case_sensitive=False
    )


# Global settings object — import this everywhere
settings = Settings()