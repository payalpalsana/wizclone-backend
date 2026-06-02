# app/routes/boards.py

# This file handles:
# - Fetching all boards from monday.com
# - Enabling/disabling automation per board
# - Creating/deleting webhook when board is toggled

import httpx
from fastapi import APIRouter, HTTPException
from supabase import Client

from app.core.database import get_db
from app.schemas.boards import (
    BoardsListResponse, BoardItem,
    BoardToggleRequest, BoardToggleResponse,
    WebhookStatus,
)
from app.services.webhook_service import create_webhook, delete_webhook

router = APIRouter(prefix="/api", tags=["Boards"])

MONDAY_API_URL = "https://api.monday.com/v2" 


def _get_workspace(monday_workspace_id: str) -> dict:
    """Returns {id (UUID), access_token} for the workspace."""
    
    try:
        db = get_db()
        result = db.table("workspaces") \
            .select("id, access_token") \
            .eq("monday_workspace_id", monday_workspace_id) \
            .single() \
            .execute()
    except Exception:
        # If DB query fails
        raise HTTPException(status_code=404, detail="Workspace not found")

    if not result.data:
        raise HTTPException(status_code=404, detail="Workspace not found")

    return result.data


# ─────────────────────────────────────────
# Helper: Fetch boards from monday.com
# ─────────────────────────────────────────
async def _fetch_monday_boards(access_token: str) -> list[dict]:
    """Fetches all boards from monday.com API."""
    query = """
    query {
      boards(limit: 100, order_by: created_at) {
        id
        name
      }
    }
    """

    headers = {
        "Authorization": access_token,
        "Content-Type":  "application/json",
        "API-Version":   "2024-01",
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            MONDAY_API_URL,
            json={"query": query},
            headers=headers,
            timeout=15,
        )
    response.raise_for_status()
    return response.json().get("data", {}).get("boards", [])


# # ─────────────────────────────────────────
# # GET /api/boards/{workspaceId}
# # ─────────────────────────────────────────
# @router.get("/boards/{workspaceId}", response_model=BoardsListResponse)
# async def list_boards(workspaceId: str):
#     """
#     Returns all monday.com boards merged with DB enable/disable state.
#     """
#     db = get_db()
#     workspace     = _get_workspace(workspaceId)
#     internal_uuid = workspace["id"]
#     access_token  = workspace["access_token"]

#     # Fetch boards from monday.com
#     try:
#         monday_boards = await _fetch_monday_boards(access_token)
#     except HTTPException:
#         raise
#     except Exception as e:
#         raise HTTPException(status_code=502, detail=f"Failed to fetch boards: {str(e)}")

#     # Fetch monitored_boards from DB
#     try:
#         db_result = db.table("monitored_boards") \
#             .select("board_id, is_enabled, webhook_id, webhook_status") \
#             .eq("workspace_id", internal_uuid) \
#             .is_("deleted_at", "null") \
#             .execute()
#         db_boards = {str(row["board_id"]): row for row in (db_result.data or [])}
#     except Exception:
#         db_boards = {}

#     # Merge
#     board_items = []
#     for board in monday_boards:
#         board_id = str(board["id"])
#         db_row   = db_boards.get(board_id)
#         board_items.append(BoardItem(
#             board_id       = int(board_id),
#             board_name     = board["name"],
#             is_enabled     = db_row.get("is_enabled", False) if db_row else False,
#             webhook_status = db_row.get("webhook_status")    if db_row else None,
#         ))

#     return BoardsListResponse(
#         workspace_id = workspaceId,
#         boards       = board_items,
#         message      = "Success",
#     )


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