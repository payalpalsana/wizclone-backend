import jwt
from supabase import Client
from app.core.database import get_db
from app.core.config import settings


# ─────────────────────────────────────────
# HELPER: Verify session token
# Signed with CLIENT SECRET
# ─────────────────────────────────────────
def _verify_session_token(token: str) -> dict | None:
    """
    Decodes monday.com session token. Signed with MONDAY_CLIENT_SECRET.
    Decoded schema:
    {
        "dat": {
            "account_id": 123,
            "user_id": 456,
            "is_admin": true
        },
        "exp": 1234567890
    }
    """
    try:
        return jwt.decode(
            token,
            settings.monday_client_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError:
        return None   # expired
    except jwt.InvalidTokenError:
        return None   # tampered

# ─────────────────────────────────────────
# HELPER: Verify authorization token
# Signed with SIGNING SECRET
# ─────────────────────────────────────────
def _verify_authorization_token(token: str) -> dict | None:
    """
    Decodes JWT sent by monday.com to /authorization endpoint.
    Signed with MONDAY_SIGNING_SECRET.
    Decoded schema:
    {
        "userId":     123,
        "accountId":  456,
        "backToUrl":  "https://monday.com/boards/..."
    }
    """
    try:
        return jwt.decode(
            token,
            settings.monday_signing_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

# ─────────────────────────────────────────
# HELPER: Save user + default settings to DB
# ─────────────────────────────────────────
def _init_user_and_settings(
    db,
    workspace_uuid: str,
    user_id:        int,
    user_name:      str  = None,
    user_email:     str  = None,
    is_admin:       bool = False,
):
    """
    Upserts user row and creates default workspace_settings.
    Called after OAuth completes or on first app load.
    """
    # Upsert user
    try:
        db.table("users").upsert(
            {
                "workspace_id":   workspace_uuid,
                "monday_user_id": user_id,
                "name":           user_name,
                "email":          user_email,
                "role":           "ADMIN" if is_admin else "MEMBER",
                "is_admin":       is_admin,
            },
            on_conflict="workspace_id,monday_user_id",
        ).execute()
    except Exception:
        pass  # non-critical

    # Create default settings only if not exists
    try:
        existing = db.table("workspace_settings") \
            .select("id") \
            .eq("workspace_id", workspace_uuid) \
            .execute()

        if not existing.data:
            db.table("workspace_settings").insert({
                "workspace_id":                 workspace_uuid,
                "ai_sensitivity":               "BALANCED",
                "ai_enabled":                   True,
                "is_enabled":                   True,
                "exact_match_fallback_enabled": True,
                "onboarding_completed":         False,
            }).execute()
    except Exception:
        pass  # non-critical

