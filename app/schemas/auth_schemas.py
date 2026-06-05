# app/schemas/auth.py

from pydantic import BaseModel
from typing   import Optional


# ─────────────────────────────────────────
# POST /api/auth/verify
# Called on every app load
# ─────────────────────────────────────────

class VerifyRequest(BaseModel):
    """
    Frontend sends this on every app load.

    How frontend gets these values:
        workspaceId  → from monday context
    """
    workspaceId:  int


class VerifyResponse(BaseModel):
    """
    has_oauth = False → no access token stored yet
                        frontend shows "Connect Workspace" button
                        user clicks → browser goes to GET /api/auth/authorization

    has_oauth = True  → access token exists in DB
                        app loads normally, no OAuth needed
    """
    success:        bool
    message:        str
    has_oauth:      bool
    workspace_uuid: Optional[str]  = None   # internal UUID for all other API calls
    workspace_id: Optional[int]  = None
    user_id:      Optional[int]  = None
    is_admin:       Optional[bool] = None