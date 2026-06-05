# app/schemas/settings_schemas.py

from pydantic import BaseModel
from typing import Optional, List
from enum import Enum


# ─────────────────────────────────────────
# Webhook status enum (merged from boards.py)
# ─────────────────────────────────────────
class WebhookStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


# ─────────────────────────────────────────
# Board model
# ─────────────────────────────────────────
class BoardSetting(BaseModel):
    """Single board in settings list."""
    board_id: int
    board_name: Optional[str] = None
    board_enabled: bool = False   # default OFF


# ─────────────────────────────────────────
# POST /api/settings/load → Request
# ─────────────────────────────────────────
class SettingsLoadRequest(BaseModel):
    """
    Frontend sends workspaceId in body.
    Session token comes from Authorization header.
    """
    workspaceId: int


# ─────────────────────────────────────────
# POST /api/settings/load → Response
# ─────────────────────────────────────────
class SettingsResponse(BaseModel):
    """
    Returned when frontend loads Settings page.

    boards             → all boards for this workspace (synced with monday.com)
    sensitivity        → STRICT | BALANCED | LOOSE
    automation_enabled → global on/off toggle
    """
    workspace_id: str
    boards: List[BoardSetting] = []
    sensitivity: str = "BALANCED"
    automation_enabled: bool = True


# ─────────────────────────────────────────
# POST /api/settings/save → Request
# ─────────────────────────────────────────
class SettingsSaveRequest(BaseModel):
    """
    Single request that handles everything together.
    All fields optional except workspaceId.

    workspaceId        → which workspace to save
    sensitivity        → STRICT | BALANCED | LOOSE
    automation_enabled → global toggle
                         false → delete webhooks for all is_enabled=true boards
                         true  → create webhooks for ALL boards
    boards             → individual board toggles
                         board_enabled=true  → create webhook on monday.com
                         board_enabled=false → delete webhook from monday.com
    """
    workspaceId: int
    sensitivity: Optional[str] = None
    automation_enabled: Optional[bool] = None
    boards: Optional[List[BoardSetting]] = None