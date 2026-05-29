# app/schemas/settings.py

from pydantic import BaseModel
from typing import Optional


# ─────────────────────────────────────────
# Response Model (GET API)
# ─────────────────────────────────────────
class SettingsResponse(BaseModel):
    """
    This model is used when frontend calls:
    GET /settings/{workspaceId}

    It tells frontend:
    - Which board is selected
    - What AI sensitivity is set
    - Whether automation is ON/OFF
    """
    workspace_id:       str
    board_id:           Optional[int]  = None
    sensitivity:        str            = "BALANCED"
    automation_enabled: bool           = True
    board_enabled:      bool           = True


# ─────────────────────────────────────────
# Request Model (POST API)
# ─────────────────────────────────────────
class SettingsSaveRequest(BaseModel):
    """
    This model is used when frontend calls:
    POST /settings/{workspaceId}

    Frontend sends this data when user clicks:
    "Save Settings"
    """
    board_id:           Optional[int]  = None
    sensitivity:        Optional[str]  = None
    automation_enabled: Optional[bool] = None
    board_enabled:      Optional[bool] = None