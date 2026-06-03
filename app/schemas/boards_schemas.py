# app/schemas/boards.py

from pydantic import BaseModel
from typing import Optional, List
from enum import Enum


# ─────────────────────────────────────────
# Enum: Webhook Status
# ─────────────────────────────────────────
class WebhookStatus(str, Enum):
    """
    Represents webhook state for a board

    ACTIVE   → webhook is created and running
    DISABLED → webhook is removed / not working
    """
    ACTIVE   = "ACTIVE"
    DISABLED = "DISABLED"


# ─────────────────────────────────────────
# Single Board Model
# ─────────────────────────────────────────
class BoardItem(BaseModel):
    """
    Represents one board in the list

    Used in GET /boards API response
    """
    board_id:       int
    board_name:     str
    is_enabled:     bool           = False
    webhook_status: Optional[WebhookStatus] = None


# ─────────────────────────────────────────
# GET /boards/{workspaceId} → Response
# ─────────────────────────────────────────
class BoardsListResponse(BaseModel):
    """
    Response when frontend loads all boards

    Contains:
    - workspace_id
    - list of 
    - message (success or error detail)
    """
    workspace_id: str
    boards:       List[BoardItem]
    message:      Optional[str] = None


# ─────────────────────────────────────────
# POST /boards/{workspaceId}/{boardId}/toggle → Request
# ─────────────────────────────────────────
class BoardToggleRequest(BaseModel):
    """
    Request sent when user toggles a board ON/OFF

    Example:
    {
        "is_enabled": true
    }
    """
    is_enabled: bool


# ─────────────────────────────────────────
# POST /boards/{workspaceId}/{boardId}/toggle → Response
# ─────────────────────────────────────────
class BoardToggleResponse(BaseModel):
    """
    Response after toggling board

    Sent back to frontend to confirm action
    """
    board_id:       int
    is_enabled:     bool
    webhook_status: WebhookStatus
    message:        str