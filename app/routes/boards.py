# app/routes/boards.py

# This file handles:
# - Fetching all boards from monday.com
# - Enabling/disabling automation per board
# - Creating/deleting webhook when board is toggled

import httpx
from fastapi import APIRouter, HTTPException, Depends
from app.core.config import settings

from app.core.database import get_supabase
from app.schemas.boards import (
    BoardsListResponse,
    BoardItem,
    BoardToggleRequest,
    BoardToggleResponse,
    WebhookStatus,
)
from app.services.webhook_service import create_webhook, delete_webhook

router = APIRouter(tags=["Boards"])

# Monday.com GraphQL API URL
MONDAY_API_URL = settings.monday_api_url


# ─────────────────────────────────────────
# Helper: Get workspace details from DB
# ─────────────────────────────────────────
def _get_workspace(monday_workspace_id: str, db) -> dict:
    """
    This function finds workspace using monday_workspace_id.

    Why needed?
    - Frontend sends monday_workspace_id
    - But our DB uses internal UUID
    - So we convert monday ID → internal ID

    Returns:
    {
        "id": UUID (internal workspace id),
        "access_token": "..." (for calling monday API)
    }
    """

    try:
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
    """
    Calls monday.com API to get all boards of the user.

    Returns:
    [
        {"id": 123, "name": "Sales Board"},
        {"id": 456, "name": "Project Board"}
    ]
    """

    query = """
    query {
      boards(limit: 100, order_by: created_at) {
        id
        name
      }
    }
    """

    headers = {
        "Authorization": access_token,  # Required for monday API
        "Content-Type":  "application/json",
        "API-Version":   "2024-01",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                MONDAY_API_URL,
                json={"query": query},
                headers=headers,
                timeout=15,
            )

        # Raise error if API fails
        response.raise_for_status()

        data = response.json()

        # Extract boards list
        return data.get("data", {}).get("boards", [])
    
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Unexpected error fetching boards: {str(e)}")

# ─────────────────────────────────────────
# GET /boards/{workspaceId}
# ─────────────────────────────────────────
@router.get("/boards/{workspaceId}", response_model=BoardsListResponse)
async def list_boards(workspaceId: str, db = Depends(get_supabase)):
    """
    This API returns:
    - All boards from monday.com
    - Plus their enabled/disabled status from DB

    Flow:
    1. Get workspace info
    2. Fetch boards from monday.com
    3. Fetch saved board settings from DB
    4. Merge both
    """

    # Step 1: Get workspace info
    # Pass db explicitly — _get_workspace is a plain function, not a FastAPI dependency
    try:
        workspace = _get_workspace(workspaceId, db)
        internal_uuid = workspace["id"]
        access_token  = workspace["access_token"]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resolve workspace: {str(e)}")
    

    # Step 2: Fetch boards from monday
    try:
        monday_boards = await _fetch_monday_boards(access_token)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch boards: {str(e)}")
    
        # Initialize board_items before try block so it is always defined
    board_items = []
    message     = "Success"

    # Step 3: Get board settings from DB
    try:
        db_result = db.table("monitored_boards") \
            .select("board_id, is_enabled, webhook_id, webhook_status") \
            .eq("workspace_id", internal_uuid) \
            .is_("deleted_at", "null") \
            .execute()

        # Convert list → dict for easy lookup
        db_boards = {
            str(row["board_id"]): row
            for row in (db_result.data or [])
        }

        # Step 4: Merge monday boards + DB data
        for board in monday_boards:
            board_id = str(board["id"])
            db_row   = db_boards.get(board_id)

            board_items.append(BoardItem(
                board_id       = int(board_id),
                board_name     = board["name"],
                is_enabled     = db_row.get("is_enabled", False) if db_row else False,
                webhook_status = db_row.get("webhook_status")    if db_row else None,
            ))

    except HTTPException:
        raise
    except Exception as e:
        messsage = f"Error at {e}"

    return BoardsListResponse(
        workspace_id = workspaceId,
        boards       = board_items,
        messsage = messsage if messsage else "Success"
    )


# ─────────────────────────────────────────
# POST /boards/{workspaceId}/{boardId}/toggle
# ─────────────────────────────────────────
@router.post("/boards/{workspaceId}/{boardId}/toggle", response_model=BoardToggleResponse)
async def toggle_board(
    workspaceId: str,
    boardId: int,
    body: BoardToggleRequest,
    db = Depends(get_supabase)
):
    """
    This API enables or disables a board.

    If enabled:
        → create webhook in monday.com
        → save in DB

    If disabled:
        → delete webhook
        → update DB
    """

    # Step 1: Get workspace info
    # Pass db explicitly — _get_workspace is a plain function, not a FastAPI dependency
    try:
        workspace     = _get_workspace(workspaceId, db)
        internal_uuid = workspace["id"]
        access_token  = workspace["access_token"]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resolve workspace: {str(e)}")

    # Step 2: Check if board already exists in DB
    try:
        existing_result = db.table("monitored_boards") \
            .select("*") \
            .eq("workspace_id", internal_uuid) \
            .eq("board_id", boardId) \
            .is_("deleted_at", "null") \
            .execute()

        existing = existing_result.data[0] if existing_result.data else None

    except Exception:
        existing = None


    # ══════════════════════════════
    # ENABLE BOARD
    # ══════════════════════════════
    if body.is_enabled:

        # Create webhook in monday
        try:
            webhook_id = await create_webhook(
                access_token = access_token,
                board_id     = boardId,
                workspace_id = workspaceId,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to create webhook: {str(e)}")

        # Data to save in DB
        row_data = {
            "workspace_id":   internal_uuid,
            "board_id":       boardId,
            "is_enabled":     True,
            "webhook_id":     str(webhook_id),
            "webhook_status": "ACTIVE",
        }

        try:
            if existing:
                # Update existing record
                db.table("monitored_boards") \
                    .update(row_data) \
                    .eq("workspace_id", internal_uuid) \
                    .eq("board_id", boardId) \
                    .execute()
            else:
                # Insert new record
                db.table("monitored_boards") \
                    .insert(row_data) \
                    .execute()

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DB update failed: {str(e)}")

        return BoardToggleResponse(
            board_id       = boardId,
            is_enabled     = True,
            webhook_status = WebhookStatus.ACTIVE,
            message        = "Board enabled — webhook created",
        )


    # ══════════════════════════════
    # DISABLE BOARD
    # ══════════════════════════════
    else:

        # Delete webhook if exists
        try:
            if existing and existing.get("webhook_id"):
                await delete_webhook(
                    access_token = access_token,
                    webhook_id   = existing["webhook_id"],
                )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to delete webhook: {str(e)}")
        
        try:
            if existing:
                # Update existing record
                db.table("monitored_boards") \
                    .update({
                        "is_enabled":     False,
                        "webhook_status": "DISABLED",
                    }) \
                    .eq("workspace_id", internal_uuid) \
                    .eq("board_id", boardId) \
                    .execute()
            else:
                # Insert new record as disabled
                db.table("monitored_boards") \
                    .insert({
                        "workspace_id":   internal_uuid,
                        "board_id":       boardId,
                        "is_enabled":     False,
                        "webhook_status": "DISABLED",
                    }) \
                    .execute()

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DB update failed: {str(e)}")

        return BoardToggleResponse(
            board_id       = boardId,
            is_enabled     = False,
            webhook_status = WebhookStatus.DISABLED,
            message        = "Board disabled — webhook deleted from monday.com",
        )