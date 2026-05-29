# app/schemas/auth.py

# Pydantic is used to define request/response data structure
from pydantic import BaseModel

# ─────────────────────────────────────────
# OAuthResponse Schema
# This model defines how your API response will look after OAuth (login/install) process
# ─────────────────────────────────────────
class OAuthResponse(BaseModel):
    success: bool
    message: str


# ─────────────────────────────────────────
# VerifyRequest Schema
# This model defines what data frontend must send
# when calling /api/auth/verify
# ─────────────────────────────────────────
class VerifyRequest(BaseModel):
    sessionToken: str
    accountId: str
    userId: str
    workspaceId: int