# app/schemas/settings.py

from pydantic import BaseModel
from typing   import Optional, List


# ─────────────────────────────────────────
# Shared board model
# ─────────────────────────────────────────

class BoardSetting(BaseModel):
    """Used in GET response to show each board's current state."""
    board_id:      int
    board_enabled: bool = True


# ─────────────────────────────────────────
# GET /api/settings/{workspaceId}
# ─────────────────────────────────────────

class SettingsResponse(BaseModel):
    """
    Returned when frontend loads the Settings page.

    boards             → all monitored boards with their user_enabled state
    sensitivity        → STRICT | BALANCED | LOOSE
    automation_enabled → global on/off toggle for the whole workspace
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
    Saves workspace-level settings only.
    Board toggles are handled by dedicated activate/deactivate endpoints.

    sensitivity        → STRICT | BALANCED | LOOSE
    automation_enabled → global toggle
                         false → disable all boards + delete all webhooks
                         true  → re-enable boards where user_enabled=true
    """
    sensitivity:        Optional[str]  = None
    automation_enabled: Optional[bool] = None


# ─────────────────────────────────────────
# POST /api/boards/{workspaceId}/activate
# ─────────────────────────────────────────

class BoardActivateRequest(BaseModel):
    """
    Frontend sends board_id + sessionToken when user enables a board.

    sessionToken → verify JWT → extract workspace UUID internally
    board_id     → which board to activate
    board_name   → optional, saved for display in UI
    """
    sessionToken: str
    board_id:     int
    board_name:   Optional[str] = None


class BoardActivateResponse(BaseModel):
    success:    bool
    message:    str
    board_id:   int
    webhook_id: Optional[str] = None


# ─────────────────────────────────────────
# POST /api/boards/{workspaceId}/deactivate
# ─────────────────────────────────────────

class BoardDeactivateRequest(BaseModel):
    """
    Frontend sends board_id + sessionToken when user disables a board.
    """
    sessionToken: str
    board_id:     int


class BoardDeactivateResponse(BaseModel):
    success:  bool
    message:  str
    board_id: int