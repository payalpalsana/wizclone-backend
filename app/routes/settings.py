# app/routes/settings.py
# ─────────────────────────────────────────────────────────────
# POST /api/settings/load → load settings on page open
# POST /api/settings/save → save everything in one call
#
# LOAD flow (runs every time settings page opens):
#   1. Fetch boards from monday.com for THIS workspace only
#      (using workspace_ids filter in GraphQL)
#   2. Fetch boards from DB (monitored_boards)
#   3. Sync:
#      - New board in monday  → insert to DB with is_enabled=false
#      - Board deleted from monday → delete webhook + soft delete in DB
#      - Same → do nothing
#   4. Return final merged list + sensitivity + toggle
#
# SAVE handles all 3 things:
#   1. sensitivity        → update workspace_settings.ai_sensitivity
#   2. automation_enabled → global toggle
#      OFF → delete webhooks for all is_enabled=true boards
#      ON  → create webhooks for ALL boards
#   3. boards[]           → individual board enable/disable
#      board_enabled=true  → create webhook + is_enabled=true in DB
#      board_enabled=false → delete webhook + is_enabled=false in DB
#
# Session token → Authorization header
# workspaceId   → request body
# ─────────────────────────────────────────────────────────────

from datetime import datetime, timezone

from fastapi  import APIRouter, HTTPException, Request, Depends
from supabase import Client

from app.core.database   import get_db
from app.schemas.settings_schemas import (
    SettingsLoadRequest, SettingsSaveRequest,
    SettingsResponse, BoardSetting,
)
from app.services.webhook_services  import create_webhook, delete_webhook
from app.services.settings_services import (
    get_workspace_by_monday_id,
    fetch_monday_boards,
)

router = APIRouter(prefix="/api/settings", tags=["Settings"])


# ─────────────────────────────────────────
# POST /api/settings/load
# ─────────────────────────────────────────
@router.post("/load", response_model=SettingsResponse)
async def load_settings(
    body: SettingsLoadRequest,
    db:   Client = Depends(get_db),
):
    """
    Called every time the Settings page opens.
    workspaceId in body, session token in Authorization header.

    Syncs monday.com boards with DB:
    - New board  → insert with is_enabled=false (toggle OFF)
    - Deleted board → delete webhook + soft delete from DB
    - Same → do nothing

    Returns final board list + sensitivity + automation toggle.
    """

    workspace      = get_workspace_by_monday_id(str(body.workspaceId), db)
    workspace_uuid = workspace["id"]
    access_token   = workspace["access_token"]

    # ── Fetch workspace settings ──
    try:
        ws_result = db.table("workspace_settings") \
            .select("ai_sensitivity, is_enabled") \
            .eq("workspace_id", workspace_uuid) \
            .single() \
            .execute()
        ws_data = ws_result.data or {}
    except Exception:
        ws_data = {}

    # ══════════════════════════════════════════════════════
    # SYNC LOGIC
    # ══════════════════════════════════════════════════════

    # ── Step 1: Fetch boards from monday.com (THIS workspace only) ──
    try:
        monday_boards    = await fetch_monday_boards(access_token, body.workspaceId)
        monday_board_ids = {str(b["id"]) for b in monday_boards}
        monday_board_map = {str(b["id"]): b["name"] for b in monday_boards}
    except Exception:
        # If monday.com call fails → skip sync, return DB data only
        monday_boards    = []
        monday_board_ids = set()
        monday_board_map = {}

    # ── Step 2: Fetch boards from DB ──
    try:
        db_result = db.table("monitored_boards") \
            .select("id, board_id, board_name, is_enabled, webhook_id") \
            .eq("workspace_id", workspace_uuid) \
            .is_("deleted_at", "null") \
            .execute()
        db_boards    = db_result.data or []
        db_board_ids = {str(b["board_id"]) for b in db_boards}
        db_board_map = {str(b["board_id"]): b for b in db_boards}
    except Exception:
        db_boards    = []
        db_board_ids = set()
        db_board_map = {}

    now = datetime.now(timezone.utc).isoformat()

    # ── Step 3a: New boards (in monday but not in DB) ──
    # Insert with is_enabled=false → shows as OFF in UI
    new_board_ids = monday_board_ids - db_board_ids
    for board_id in new_board_ids:
        try:
            db.table("monitored_boards").insert({
                "workspace_id":   workspace_uuid,
                "board_id":       int(board_id),
                "board_name":     monday_board_map.get(board_id, ""),
                "is_enabled":     False,
                "webhook_id":     None,
                "webhook_status": "DISABLED",
                "is_active":      True,
            }).execute()
        except Exception:
            pass   # non-critical — continue with other boards

    # ── Step 3b: Deleted boards (in DB but not in monday) ──
    # Delete webhook from monday.com + soft delete from DB
    deleted_board_ids = db_board_ids - monday_board_ids
    for board_id in deleted_board_ids:
        db_board = db_board_map.get(board_id)
        if not db_board:
            continue

        # Delete webhook from monday.com if exists
        if db_board.get("webhook_id"):
            try:
                await delete_webhook(
                    access_token = access_token,
                    webhook_id   = db_board["webhook_id"],
                )
            except Exception:
                pass

        # Soft delete from DB
        try:
            db.table("monitored_boards") \
                .update({
                    "deleted_at":     now,
                    "is_enabled":     False,
                    "webhook_id":     None,
                    "webhook_status": "DISABLED",
                }) \
                .eq("id", db_board["id"]) \
                .execute()
        except Exception:
            pass

    # ── Step 4: Fetch final board list from DB ──
    # Re-fetch after sync so new boards are included
    # Soft deleted boards excluded automatically
    try:
        final_result = db.table("monitored_boards") \
            .select("board_id, board_name, is_enabled") \
            .eq("workspace_id", workspace_uuid) \
            .is_("deleted_at", "null") \
            .execute()
        final_boards = final_result.data or []
    except Exception:
        final_boards = []

    boards = [
        BoardSetting(
            board_id      = row["board_id"],
            board_name    = row["board_name"],
            board_enabled = row.get("is_enabled", False),
        )
        for row in final_boards
    ]

    return SettingsResponse(
        workspace_id       = str(body.workspaceId),
        boards             = boards,
        sensitivity        = ws_data.get("ai_sensitivity", "BALANCED"),
        automation_enabled = ws_data.get("is_enabled", True),
    )


# ─────────────────────────────────────────
# POST /api/settings/save
# ─────────────────────────────────────────
@router.post("/save")
async def save_settings(
    body:    SettingsSaveRequest,
    request: Request,
    db:      Client = Depends(get_db),
):
    """
    Single API — saves everything together.
    workspaceId in body, session token in Authorization header.

    Part 1 — sensitivity + global toggle → workspace_settings (DB only)
    Part 2 — global automation toggle    → webhook create/delete on monday.com
    Part 3 — individual board toggles   → webhook create/delete + DB update
    """

    workspace      = get_workspace_by_monday_id(str(body.workspaceId), db)
    workspace_uuid = workspace["id"]
    access_token   = workspace["access_token"]

    # ══════════════════════════════════════════════════════
    # PART 1 — Save sensitivity + global toggle to DB only
    # ══════════════════════════════════════════════════════
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

    # ══════════════════════════════════════════════════════
    # PART 2 — Global automation toggle
    # OFF → delete webhooks for all is_enabled=true boards only
    # ON  → create webhooks for ALL boards
    # ══════════════════════════════════════════════════════
    if body.automation_enabled is not None:

        try:
            all_boards_result = db.table("monitored_boards") \
                .select("board_id, is_enabled, webhook_id") \
                .eq("workspace_id", workspace_uuid) \
                .is_("deleted_at", "null") \
                .execute()
            all_boards = all_boards_result.data or []
        except Exception:
            all_boards = []

        # ── Global OFF ──
        if not body.automation_enabled:
            for board in all_boards:
                if not board.get("is_enabled"):
                    continue   # already off → skip

                if board.get("webhook_id"):
                    try:
                        await delete_webhook(
                            access_token = access_token,
                            webhook_id   = board["webhook_id"],
                        )
                    except Exception:
                        pass

                try:
                    db.table("monitored_boards") \
                        .update({
                            "is_enabled":     False,
                            "webhook_id":     None,
                            "webhook_status": "DISABLED",
                        }) \
                        .eq("workspace_id", workspace_uuid) \
                        .eq("board_id",     board["board_id"]) \
                        .execute()
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to disable board {board['board_id']}: {str(e)}",
                    )

        # ── Global ON ──
        else:
            for board in all_boards:
                try:
                    webhook_id = await create_webhook(
                        access_token = access_token,
                        board_id     = board["board_id"],
                        workspace_id = str(body.workspaceId),
                    )
                    db.table("monitored_boards") \
                        .update({
                            "is_enabled":     True,
                            "webhook_id":     str(webhook_id) if webhook_id else None,
                            "webhook_status": "ACTIVE" if webhook_id else "DISABLED",
                        }) \
                        .eq("workspace_id", workspace_uuid) \
                        .eq("board_id",     board["board_id"]) \
                        .execute()
                except Exception:
                    pass

    # ══════════════════════════════════════════════════════
    # PART 3 — Individual board toggles
    # ENABLE/RE-ENABLE → create_webhook() + is_enabled=true
    # DISABLE          → delete_webhook() + is_enabled=false
    # ══════════════════════════════════════════════════════
    if body.boards is not None:
        for board in body.boards:

            try:
                existing_result = db.table("monitored_boards") \
                    .select("*") \
                    .eq("workspace_id", workspace_uuid) \
                    .eq("board_id",     board.board_id) \
                    .is_("deleted_at",  "null") \
                    .execute()
                existing = existing_result.data[0] if existing_result.data else None
            except Exception:
                existing = None

            # ── ENABLE or RE-ENABLE ──
            if board.board_enabled:

                webhook_id = await create_webhook(
                    access_token = access_token,
                    board_id     = board.board_id,
                    workspace_id = str(body.workspaceId),
                )

                row = {
                    "workspace_id":   workspace_uuid,
                    "board_id":       board.board_id,
                    "board_name":     board.board_name,
                    "is_enabled":     True,
                    "webhook_id":     str(webhook_id) if webhook_id else None,
                    "webhook_status": "ACTIVE" if webhook_id else "DISABLED",
                    "is_active":      True,
                }

                try:
                    if existing:
                        db.table("monitored_boards") \
                            .update(row) \
                            .eq("workspace_id", workspace_uuid) \
                            .eq("board_id",     board.board_id) \
                            .execute()
                    else:
                        db.table("monitored_boards").insert(row).execute()
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"DB save failed for board {board.board_id}: {str(e)}",
                    )

            # ── DISABLE ──
            else:

                if existing and existing.get("webhook_id"):
                    try:
                        await delete_webhook(
                            access_token = access_token,
                            webhook_id   = existing["webhook_id"],
                        )
                    except Exception:
                        pass

                row = {
                    "is_enabled":     False,
                    "webhook_id":     None,
                    "webhook_status": "DISABLED",
                }

                try:
                    if existing:
                        db.table("monitored_boards") \
                            .update(row) \
                            .eq("workspace_id", workspace_uuid) \
                            .eq("board_id",     board.board_id) \
                            .execute()
                    else:
                        db.table("monitored_boards").insert({
                            "workspace_id": workspace_uuid,
                            "board_id":     board.board_id,
                            "board_name":   board.board_name,
                            "is_active":    True,
                            **row,
                        }).execute()
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"DB update failed for board {board.board_id}: {str(e)}",
                    )

    return {"success": True, "message": "Settings saved successfully"}