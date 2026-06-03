# app/routes/boards.py

# This file handles:
# - Fetching all boards from monday.com
# - Enabling/disabling automation per board
# - Creating/deleting webhook when board is toggled

import httpx
from fastapi import APIRouter, HTTPException, Depends
from supabase import Client

from app.core.database import get_db
from app.services.boards_services import (
    _get_workspace, _fetch_monday_boards,
)
from app.schemas.boards_schemas import (
    BoardsListResponse, BoardItem,
    BoardToggleRequest, BoardToggleResponse,
    WebhookStatus,
)
from app.services.webhook_services import create_webhook, delete_webhook


router = APIRouter(prefix="/api", tags=["Boards"])


# ─────────────────────────────────────────
# GET /api/boards/{workspaceId}
# ─────────────────────────────────────────
@router.get("/boards/{workspaceId}", response_model=BoardsListResponse)
async def list_boards(workspaceId: str, db: Client = Depends(get_db)):
    """
    Returns all boards from monday.com for this workspace,
    merged with their enabled/webhook state from DB.
    """
    workspace      = _get_workspace(workspaceId)
    workspace_uuid = workspace["id"]
    access_token   = workspace["access_token"]

    # Fetch live boards from monday.com
    try:
        monday_boards = await _fetch_monday_boards(access_token)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch boards: {str(e)}")

    # Fetch DB state for monitored boards
    try:
        db_result = db.table("monitored_boards") \
            .select("board_id, is_enabled, webhook_status") \
            .eq("workspace_id", workspace_uuid) \
            .is_("deleted_at", "null") \
            .execute()
        db_boards = {str(row["board_id"]): row for row in (db_result.data or [])}
    except Exception:
        db_boards = {}

    boards = [
        BoardItem(
            board_id       = int(b["id"]),
            board_name     = b["name"],
            is_enabled     = db_boards.get(str(b["id"]), {}).get("is_enabled", False),
            webhook_status = db_boards.get(str(b["id"]), {}).get("webhook_status", WebhookStatus.DISABLED),
        )
        for b in monday_boards
    ]

    return BoardsListResponse(
        workspace_id = workspaceId,
        boards       = boards,
        message      = f"{len(boards)} boards fetched",
    )


# # ─────────────────────────────────────────
# # POST /api/boards/{workspaceId}/{boardId}/toggle
# # ─────────────────────────────────────────
# @router.post("/boards/{workspaceId}/{boardId}/toggle", response_model=BoardToggleResponse)
# async def toggle_board(workspaceId: str, boardId: int, body: BoardToggleRequest):
#     """
#     Enable  → create webhook on monday.com + save to DB
#     Disable → delete webhook from monday.com + update DB
#     """
#     db = get_db()
#     workspace     = _get_workspace(workspaceId)
#     internal_uuid = workspace["id"]
#     access_token  = workspace["access_token"]

#     # Check existing DB row
#     try:
#         existing_result = db.table("monitored_boards") \
#             .select("*") \
#             .eq("workspace_id", internal_uuid) \
#             .eq("board_id", boardId) \
#             .is_("deleted_at", "null") \
#             .execute()

#         existing = existing_result.data[0] if existing_result.data else None

#     except Exception:
#         existing = None

#     # ── ENABLE
#     if body.is_enabled:

#         # Create webhook in monday
#         try:
#             webhook_id = await create_webhook(
#                 access_token = access_token,
#                 board_id     = boardId,
#                 workspace_id = workspaceId,
#             )
#         except Exception as e:
#             raise HTTPException(status_code=502, detail=f"Failed to create webhook: {str(e)}")

#         # Data to save in DB
#         row_data = {
#             "workspace_id":   internal_uuid,
#             "board_id":       boardId,
#             "is_enabled":     True,
#             "webhook_id":     str(webhook_id),
#             "webhook_status": "ACTIVE",
#         }

#         try:
#             if existing:
#                 db.table("monitored_boards").update(row_data) \
#                     .eq("workspace_id", internal_uuid).eq("board_id", boardId).execute()
#             else:
#                 db.table("monitored_boards").insert(row_data).execute()
#         except Exception as e:
#             raise HTTPException(status_code=500, detail=f"DB update failed: {str(e)}")

#         return BoardToggleResponse(
#             board_id=boardId, is_enabled=True,
#             webhook_status=WebhookStatus.ACTIVE,
#             message="Board enabled — webhook created",
#         )


#     # ── DISABLE
#     else:
#         if existing and existing.get("webhook_id"):
#             await delete_webhook(access_token=access_token, webhook_id=existing["webhook_id"])

#         try:
#             if existing:
#                 # Update existing record
#                 db.table("monitored_boards") \
#                     .update({"is_enabled": False, "webhook_status": "DISABLED"}) \
#                     .eq("workspace_id", internal_uuid).eq("board_id", boardId).execute()
#             else:
#                 db.table("monitored_boards").insert({
#                     "workspace_id": internal_uuid, "board_id": boardId,
#                     "is_enabled": False, "webhook_status": "DISABLED",
#                 }).execute()
#         except Exception as e:
#             raise HTTPException(status_code=500, detail=f"DB update failed: {str(e)}")

#         return BoardToggleResponse(
#             board_id=boardId, is_enabled=False,
#             webhook_status=WebhookStatus.DISABLED,
#             message="Board disabled — webhook deleted",
#         )