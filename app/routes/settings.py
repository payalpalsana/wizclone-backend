# app/routes/settings.py
# ─────────────────────────────────────────────────────────────
# Settings + Board Management APIs
#
# GET  /api/settings/{workspaceId}           → load settings on page open
# POST /api/settings/{workspaceId}           → save sensitivity + global toggle
# POST /api/boards/{workspaceId}/activate    → enable one board + create webhook
# POST /api/boards/{workspaceId}/deactivate  → disable one board + delete webhook
#
# ── WHY SEPARATE ACTIVATE / DEACTIVATE ENDPOINTS? ──
# The flow shows board enable/disable has its own clear steps:
#   Activate:   verify token → get access_token → check board in DB
#               → create webhook → save webhook_id
#   Deactivate: verify token → get access_token → get webhook_id
#               → delete webhook → update is_enabled=false
#
# Keeping these separate from POST /settings makes each endpoint
# single-purpose, easier to test, and cleaner for the frontend.
#
# ── TWO FLAGS ON EVERY BOARD ──
#   user_enabled  → what the user CHOSE (never changed by global toggle)
#   is_enabled    → actual runtime state (changed by global toggle)
#
# Example:
#   User enables Board A (user_enabled=true,  is_enabled=true)
#   User enables Board B (user_enabled=true,  is_enabled=true)
#   User disables Board B (user_enabled=false, is_enabled=false)
#   User hits global DISABLE:
#     Board A → is_enabled=false, user_enabled stays true
#     Board B → is_enabled=false, user_enabled stays false
#   User hits global ENABLE:
#     Board A → is_enabled=true  (user_enabled was true  → restore)
#     Board B → is_enabled=false (user_enabled was false → keep off)
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Depends
from supabase import Client

from app.core.database import get_db
from app.core.security import verify_session_token
from app.core.helpers import get_workspace_by_monday_id
from app.schemas.settings import (
    SettingsResponse, SettingsSaveRequest, BoardSetting,
    BoardActivateRequest, BoardActivateResponse,
    BoardDeactivateRequest, BoardDeactivateResponse,
)
from app.services.webhook_service import create_webhook, delete_webhook

router = APIRouter(prefix="/api", tags=["Settings"])


# ─────────────────────────────────────────
# GET /api/settings/{workspaceId}
# ─────────────────────────────────────────
@router.get("/settings/{workspaceId}", response_model=SettingsResponse)
async def get_settings(workspaceId: str, db: Client = Depends(get_db)):
    """
    Called every time the Settings page opens.

    Returns:
    - sensitivity      → current AI sensitivity level
    - automation_enabled → global toggle state
    - boards           → all monitored boards with user_enabled state
    """

    # Resolve monday workspace ID → internal UUID + other fields
    workspace      = get_workspace_by_monday_id(workspaceId, db)
    workspace_uuid = workspace["id"]

    # ── Fetch workspace settings (sensitivity + global toggle) ──
    try:
        ws_result = db.table("workspace_settings") \
            .select("ai_sensitivity, is_enabled") \
            .eq("workspace_id", workspace_uuid) \
            .single() \
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Settings not found")

    ws_data = ws_result.data or {}

    # ── Fetch all monitored boards ──
    # We return user_enabled (user's actual preference), NOT is_enabled
    # is_enabled can be forced false by global toggle — user_enabled never changes
    try:
        boards_result = db.table("monitored_boards") \
            .select("board_id, user_enabled, board_name") \
            .eq("workspace_id", workspace_uuid) \
            .is_("deleted_at", "null") \
            .execute()

        boards = [
            BoardSetting(
                board_id      = row["board_id"],
                board_enabled = row.get("user_enabled", True),
            )
            for row in (boards_result.data or [])
        ]
    except Exception:
        boards = []

    return SettingsResponse(
        workspace_id       = workspaceId,
        boards             = boards,
        sensitivity        = ws_data.get("ai_sensitivity", "BALANCED"),
        automation_enabled = ws_data.get("is_enabled", True),
    )


# ─────────────────────────────────────────
# POST /api/settings/{workspaceId}
# Save sensitivity + global automation toggle
# ─────────────────────────────────────────
@router.post("/settings/{workspaceId}")
async def save_settings(
    workspaceId: str,
    body:        SettingsSaveRequest,
    db:          Client = Depends(get_db),
):
    """
    Saves workspace-level settings (sensitivity and/or global toggle).
    Board-level enable/disable uses /activate and /deactivate instead.

    Global automation_enabled toggle:
    - false → delete ALL webhooks, set is_enabled=false on all boards
              (user_enabled unchanged — remembers their preference)
    - true  → re-create webhooks only for boards where user_enabled=true
              (boards user disabled stay disabled)
    """

    # Resolve workspace
    workspace      = get_workspace_by_monday_id(workspaceId, db)
    workspace_uuid = workspace["id"]
    access_token   = workspace["access_token"]

    # ── Part 1: Save sensitivity + global toggle to workspace_settings ──
    ws_update = {}

    if body.sensitivity is not None:
        ws_update["ai_sensitivity"] = body.sensitivity.upper()

    if body.automation_enabled is not None:
        ws_update["is_enabled"] = body.automation_enabled

    if ws_update:
        try:
            existing_ws = db.table("workspace_settings") \
                .select("id") \
                .eq("workspace_id", workspace_uuid) \
                .execute()

            if existing_ws.data:
                db.table("workspace_settings") \
                    .update(ws_update) \
                    .eq("workspace_id", workspace_uuid) \
                    .execute()
            else:
                # First-time setup — insert with defaults
                ws_update["workspace_id"] = workspace_uuid
                ws_update.setdefault("ai_sensitivity",               "BALANCED")
                ws_update.setdefault("is_enabled",                   True)
                ws_update.setdefault("ai_enabled",                   True)
                ws_update.setdefault("exact_match_fallback_enabled",  True)
                ws_update.setdefault("onboarding_completed",          False)
                db.table("workspace_settings").insert(ws_update).execute()

        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to save workspace settings: {str(e)}",
            )

    # ── Part 2: Global automation toggle — affects ALL boards + webhooks ──
    if body.automation_enabled is not None:

        # Fetch all monitored boards so we know which webhooks to touch
        try:
            all_boards_result = db.table("monitored_boards") \
                .select("board_id, is_enabled, user_enabled, webhook_id") \
                .eq("workspace_id", workspace_uuid) \
                .is_("deleted_at", "null") \
                .execute()
            all_boards = all_boards_result.data or []
        except Exception:
            all_boards = []

        # ── Global DISABLE ──
        # Delete ALL webhooks from monday.com
        # Set is_enabled=false on all boards in one query
        # DO NOT change user_enabled — it remembers what user chose
        if not body.automation_enabled:
            for board in all_boards:
                if board.get("webhook_id"):
                    try:
                        await delete_webhook(
                            access_token = access_token,
                            webhook_id   = board["webhook_id"],
                        )
                    except Exception:
                        pass   # Non-critical — continue deleting others

            if all_boards:
                try:
                    db.table("monitored_boards") \
                        .update({
                            "is_enabled":     False,
                            "webhook_status": "DISABLED",
                            "webhook_id":     None,
                            # user_enabled intentionally NOT changed
                        }) \
                        .eq("workspace_id", workspace_uuid) \
                        .execute()
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to disable boards: {str(e)}",
                    )

        # ── Global ENABLE ──
        # Re-create webhooks ONLY for boards where user_enabled=true
        # Boards the user explicitly disabled stay disabled
        else:
            for board in all_boards:
                if board.get("user_enabled"):
                    # User wanted this board on → restore it
                    try:
                        webhook_id = await create_webhook(
                            access_token = access_token,
                            board_id     = board["board_id"],
                            workspace_id = workspaceId,
                        )
                        db.table("monitored_boards") \
                            .update({
                                "is_enabled":     True,
                                "webhook_id":     str(webhook_id) if webhook_id else None,
                                "webhook_status": "ACTIVE" if webhook_id else "DISABLED",
                            }) \
                            .eq("workspace_id", workspace_uuid) \
                            .eq("board_id", board["board_id"]) \
                            .execute()
                    except Exception:
                        pass   # Best-effort restore — continue with others
                else:
                    # User had this board off → keep it off
                    try:
                        db.table("monitored_boards") \
                            .update({"is_enabled": False}) \
                            .eq("workspace_id", workspace_uuid) \
                            .eq("board_id", board["board_id"]) \
                            .execute()
                    except Exception:
                        pass

    return {"success": True, "message": "Settings saved successfully"}



# ────────────────────────────────────────────
# POST /api/boards/{workspaceId}/activate
# Enable one board + create webhook on monday.com
# ────────────────────────────────────────────
@router.post("/boards/{workspaceId}/activate", response_model=BoardActivateResponse)
async def activate_board(
    workspaceId: str,
    body:        BoardActivateRequest,
    db:          Client = Depends(get_db),
):
    """
    Activate Flow:

    1. Verify session token (JWT) → extract workspace identity
    2. Get access_token           → workspaces table
    3. Check board in monitored_boards
       EXISTS → update is_enabled=true, user_enabled=true
       NEW    → insert new row
    4. Call monday.com GraphQL    → create_webhook mutation
    5. Get back webhook_id from monday.com
    6. Save webhook_id + status=ACTIVE → monitored_boards
    7. Return success

    Frontend sends sessionToken so we verify the user is genuine
    before touching any monday.com automation.
    """

    # ── Step 1: Verify session token ──
    # Confirms the request is from a real monday.com user
    decoded = verify_session_token(body.sessionToken)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid session token")

    # ── Step 2: Get workspace → access_token ──
    # We need access_token to call monday.com GraphQL API
    workspace      = get_workspace_by_monday_id(workspaceId, db)
    workspace_uuid = workspace["id"]
    access_token   = workspace["access_token"]

    if not access_token:
        raise HTTPException(
            status_code=400,
            detail="No access token found. Please complete OAuth first."
        )

    # ── Step 3: Check if board already exists in monitored_boards ──
    try:
        existing_result = db.table("monitored_boards") \
            .select("id, webhook_id, is_enabled") \
            .eq("workspace_id", workspace_uuid) \
            .eq("board_id", body.board_id) \
            .is_("deleted_at", "null") \
            .execute()
        existing = existing_result.data[0] if existing_result.data else None
    except Exception:
        existing = None

    # ── Step 4 + 5: Call monday.com → create webhook → get webhook_id ──
    # monday.com registers the automation: "when item created → call our URL"
    if body.board_enabled:
        webhook_id = await create_webhook(
            access_token = access_token,
            board_id     = body.board_id,
            workspace_id = workspaceId,
        )

    # webhook_id can be None if monday.com rejected (app not live, invalid token)
    # We still save the row as ACTIVE=true so the user sees it enabled
    # Webhook will be retried or fixed when app goes live

    # ── Step 6: Save webhook_id + ACTIVE status to monitored_boards ──
    row_data = {
        "workspace_id":   workspace_uuid,
        "board_id":       body.board_id,
        "board_name":     body.board_name,
        "is_enabled":     True,    # Runtime state → on
        "user_enabled":   True,    # User preference → on
        "webhook_id":     str(webhook_id) if webhook_id else None,
        "webhook_status": "ACTIVE" if webhook_id else "DISABLED",
        "is_active":      True,    # Not deleted
    }
    try:
        if existing:
            db.table("monitored_boards") \
            .update(row_data) \
            .eq("workspace_id", workspace_uuid) \
            .eq("board_id", body.board_id) \
            .execute()
        else:
            # Board already in DB → UPDATE (re-activating)
            db.table("monitored_boards").insert(row_data).execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"DB update failed for board {body.board_id}: {str(e)}",
        )

    # ── Step 7: Return success ──
    return BoardActivateResponse(
        success    = True,
        message    = "Board activated — webhook created" if webhook_id
                     else "Board activated — webhook pending (app not live)",
        board_id   = body.board_id,
        webhook_id = str(webhook_id) if webhook_id else None,
    )
            
# ═══════════════════════════════════════════════════════════
# POST /api/boards/{workspaceId}/deactivate
# Disable one board + delete webhook from monday.com
# ═══════════════════════════════════════════════════════════
@router.post("/boards/{workspaceId}/deactivate", response_model=BoardDeactivateResponse)
async def deactivate_board(
    workspaceId: str,
    body:        BoardDeactivateRequest,
    db:          Client = Depends(get_db),
):
    """
    Deactivate Flow:

    1. Verify session token (JWT) → extract workspace identity
    2. Get access_token           → workspaces table
    3. Get webhook_id             → monitored_boards table
    4. Call monday.com GraphQL    → delete_webhook mutation
    5. Update is_enabled=false + webhook_status=DISABLED → monitored_boards
    6. Return success

    webhook_id is needed to call monday.com delete mutation.
    If webhook_id is missing (already deleted), we still update the DB.
    """

    # ── Step 1: Verify session token ──
    decoded = verify_session_token(body.sessionToken)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid session token")

    # ── Step 2: Get workspace → access_token ──
    workspace      = get_workspace_by_monday_id(workspaceId, db)
    workspace_uuid = workspace["id"]
    access_token   = workspace["access_token"]

    # ── Step 3: Get webhook_id from monitored_boards ──
    try:
        board_result = db.table("monitored_boards") \
            .select("id, webhook_id, is_enabled") \
            .eq("workspace_id", workspace_uuid) \
            .eq("board_id",     body.board_id) \
            .is_("deleted_at",  "null") \
            .execute()
        board_row = board_result.data[0] if board_result.data else None
    except Exception:
        board_row = None

    if not board_row:
        raise HTTPException(
            status_code=404,
            detail=f"Board {body.board_id} not found in monitored boards"
        )

    webhook_id = board_row.get("webhook_id")

    # ── Step 4: Delete webhook from monday.com ──
    # Even if this fails, we still update DB (webhook may already be gone)
    if webhook_id and access_token:
        try:
            await delete_webhook(
                access_token = access_token,
                webhook_id   = webhook_id,
            )
        except Exception:
            pass   # Non-critical — continue to update DB regardless

    # ── Step 5: Update monitored_boards → disabled ──
    try:
        db.table("monitored_boards") \
            .update({
                "is_enabled":     False,   # runtime state → off
                "user_enabled":   False,   # user preference → off
                "webhook_status": "DISABLED",
                "webhook_id":     None,    # clear stored webhook_id
            }) \
            .eq("workspace_id", workspace_uuid) \
            .eq("board_id",     body.board_id) \
            .execute()

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to deactivate board: {str(e)}",
        )

    # ── Step 6: Return success ──
    return BoardDeactivateResponse(
        success  = True,
        message  = "Board deactivated — webhook deleted",
        board_id = body.board_id,
    )