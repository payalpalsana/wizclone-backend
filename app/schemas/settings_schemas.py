# app/schemas/settings.py

from pydantic import BaseModel
from typing   import Optional, List


# ─────────────────────────────────────────
# Shared board model
# ─────────────────────────────────────────

class BoardSetting(BaseModel):
    """Single board toggle state."""
    board_id:      int
    board_name:    Optional[str] = None
    board_enabled: bool          = True


# ─────────────────────────────────────────
# GET /api/settings/{workspaceId}
# ─────────────────────────────────────────

class SettingsResponse(BaseModel):
    """
    Returned when frontend loads Settings page.

    boards             → all monitored boards with user_enabled state
    sensitivity        → STRICT | BALANCED | LOOSE
    automation_enabled → global on/off toggle
    """
    workspace_id:       str
    boards:             List[BoardSetting] = []
    sensitivity:        str               = "BALANCED"
    automation_enabled: bool              = True


# ─────────────────────────────────────────
# POST /api/settings/{workspaceId}
# Save sensitivity + global toggle only
# ─────────────────────────────────────────

class SettingsSaveRequest(BaseModel):
    """
    Single request that handles everything together.
    All fields optional — send only what changed.

    sensitivity        → STRICT | BALANCED | LOOSE
    automation_enabled → global toggle
                         false → delete ALL webhooks + disable all boards
                         true  → re-enable boards where user_enabled=true
    boards             → individual board toggles
                         board_enabled=true  → create webhook
                         board_enabled=false → delete webhook
    """
    sensitivity:        Optional[str]             = None
    automation_enabled: Optional[bool]            = None
    boards:             Optional[List[BoardSetting]] = None