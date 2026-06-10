# app/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Supabase ──
    supabase_url:              str
    supabase_anon_key:         str
    supabase_service_role_key: str

    database_url:              str

    # ── monday.com ──
    monday_client_id:      str
    monday_client_secret:  str       # Used to verify session tokens (JWT secret)
    monday_signing_secret: str       # Used to verify webhook HMAC signatures
    app_id:                int

    # ── App ──
    app_env:      str   # "development" | "production"
    app_port:     int
    app_base_url: str   # e.g. https://g4j5rg19-8000.inc1.devtunnels.ms — used to build webhook URLs

    # ── monday.com OAuth endpoints ──
    monday_authorize_url: str    # "https://auth.monday.com/oauth2/authorize"
    monday_token_url:     str    # "https://auth.monday.com/oauth2/token"

    monday_api_url:        str       # https://api.monday.com/v2

    model_config = SettingsConfigDict(
        env_file          = ".env",
        env_file_encoding = "utf-8",
        extra             = "ignore",
        case_sensitive    = False,
    )


# Global settings object — import this everywhere
settings = Settings()