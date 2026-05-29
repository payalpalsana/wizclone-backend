# app/core/database.py
# Supabase client setup
# Two clients: anon (normal) and admin (bypass RLS)

from supabase import create_client, Client
from app.core.config import settings            


# ── Admin client ──
# Use for all backend operations
# Service role key bypasses Row Level Security (RLS)
supabase_admin: Client = create_client(
    settings.supabase_url,
    settings.supabase_service_role_key
)


def get_supabase() -> Client:
    """
    Dependency function for FastAPI.
    Used with Depends() to inject DB client.
    """
    return supabase_admin