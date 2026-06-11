# app/core/security.py
import jwt
import hmac
import hashlib
from app.core.config import settings

# ─────────────────────────────────────────
# Verify session token
# ─────────────────────────────────────────
def verify_session_token(token: str) -> dict | None:
    """
    Verify and decode a monday.com SESSION TOKEN.
    Signed with MONDAY_CLIENT_SECRET.
 
    Called in: POST /api/auth/verify
 
    Returns decoded payload on success, None on failure.
    """
    try:
        return jwt.decode(
            token,
            settings.monday_client_secret,
            algorithms=["HS256"],
            options={"verify_aud": False,},   # monday session tokens have no aud claim
            leeway=120,                       # handle clock skew
        )
    except jwt.ExpiredSignatureError:
        return None   # token expired
    except jwt.InvalidTokenError:
        return None   # tampered or malformed


# ─────────────────────────────────────────
# Verify authorization auth_token
# ─────────────────────────────────────────
def verify_authorization_token(token: str) -> dict | None:
    """
    Verify and decode the AUTHORIZATION TOKEN that monday.com
    sends when redirecting the user to your /authorization endpoint.
    Signed with MONDAY_SIGNING_SECRET.
 
    Called in: GET /api/auth/authorization
               GET /api/auth/callback (as state param)
 
    Returns decoded payload on success, None on failure.
    Decoded shape:
    { 
        "userId":    123,
        "accountId": 456,
        "backToUrl": "https://monday.com/boards/...",
        "exp":       1723137005,        // expiration timestamp of the JWT
        "iat":       1723136705         // issued-at timestamp of the JWT
    }
    """
    try:
        return jwt.decode(
            token,
            settings.monday_signing_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
            leeway=120,                       # handle clock skew
        )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ─────────────────────────────────────────
# Verify webhook HMAC signature
# ─────────────────────────────────────────
def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """
    Verify the HMAC-SHA256 signature monday.com sends on every webhook.
    Signature is in the x-monday-signature header.
    Signed with MONDAY_SIGNING_SECRET.
 
    Called in: POST /webhook/monday
 
    Returns True if valid (or if no secret configured — dev mode).
    Returns False if tampered.
    """
    if not settings.monday_signing_secret:
        return True   # dev/test mode — skip verification
 
    expected = hmac.new(
        settings.monday_signing_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
 
    return hmac.compare_digest(expected, signature)