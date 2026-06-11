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
#      OFF → keep webhook_id, just flip is_enabled=false (receiver returns 6000)
#      ON  → reuse existing webhook_id or create new if missing
#   3. boards[]           → individual board enable/disable
#      board_enabled=true  → reuse webhook_id or create new if missing
#      board_enabled=false → keep webhook_id, just flip is_enabled=false
#
# Session token → Authorization header
# workspaceId   → request body
# ─────────────────────────────────────────────────────────────


from datetime import datetime, timezone
import asyncio

from fastapi  import APIRouter, HTTPException, Request, Depends
from supabase import Client

from app.core.database   import get_db
from app.schemas.settings import (
    SettingsLoadRequest, SettingsSaveRequest,
    SettingsResponse, BoardSetting,
)
from app.services.webhook  import create_webhook, delete_webhook
from app.services.settings import (
    get_workspace_by_monday_id,
    fetch_monday_boards,
)

router = APIRouter(prefix="/api/settings", tags=["Settings"])


# ─────────────────────────────────────────
# POST /api/settings/load
# ─────────────────────────────────────────
@router.post("/load", response_model=SettingsResponse)
async def load_settings(
    request: Request,
    body:    SettingsLoadRequest,
    db:      Client = Depends(get_db),
):
    import time
    t_start = time.time()
    print(f"\n[load] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"[load] --- START LOAD SETTINGS ---")
    
    token_data = getattr(request.state, "token_data", {})
    account_id = token_data.get("account_id")
    print(f"[load] Extracted account_id from token: {account_id}")

    if account_id:
        try:
            ws_result = db.table("workspaces") \
                .select("id, access_token, monday_workspace_id") \
                .eq("monday_account_id", int(account_id)) \
                .single() \
                .execute()
            workspace = ws_result.data
        except Exception:
            workspace = None
        if not workspace:
            raise HTTPException(status_code=404, detail="Workspace not found")
        if not ws_result.data.get("monday_workspace_id"):
            try:
                db.table("workspaces").update({
                    "monday_workspace_id": int(body.workspaceId)
                }).eq("id", workspace["id"]).execute()
            except Exception:
                pass
    else:
        workspace = get_workspace_by_monday_id(str(body.workspaceId), db)

    workspace_uuid = workspace["id"]
    access_token   = workspace["access_token"]

    try:
        ws_result = db.table("workspace_settings") \
            .select("ai_sensitivity, is_enabled") \
            .eq("workspace_id", workspace_uuid) \
            .single() \
            .execute()
        ws_data = ws_result.data or {}
    except Exception:
        ws_data = {}
    
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

    print(f"[load] Starting monday.com boards sync...")
    t_monday_start = time.time()
    try:
        monday_boards    = await asyncio.wait_for(
            fetch_monday_boards(access_token, body.workspaceId),
            timeout=10,
        )
        t_monday_end = time.time()
        print(f"[load] Monday.com sync completed in {(t_monday_end - t_monday_start) * 1000:.2f} ms")
        monday_board_ids = {str(b["id"]) for b in monday_boards}
        monday_board_map = {str(b["id"]): b["name"] for b in monday_boards}

        now = datetime.now(timezone.utc).isoformat()

        # Deleted boards → delete webhook + soft delete from DB
        deleted_board_ids = db_board_ids - monday_board_ids
        for board_id in deleted_board_ids:
            db_board = db_board_map.get(board_id)
            if not db_board:
                continue
            if db_board.get("webhook_id"):
                try:
                    await delete_webhook(
                        access_token = access_token,
                        webhook_id   = db_board["webhook_id"],
                    )
                except Exception:
                    pass
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
    except asyncio.TimeoutError:
        print("[load_settings] Monday.com board sync timed out — skipping sync, returning DB boards")
    except Exception as e:
        print(f"[load_settings] Monday.com board sync failed: {e}")

    try:
        final_result = db.table("monitored_boards") \
            .select("board_id, board_name, is_enabled") \
            .eq("workspace_id", workspace_uuid) \
            .is_("deleted_at",  "null") \
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

    print(f"[load] Returning {len(boards)} boards to frontend.")
    t_end = time.time()
    print(f"[load] --- END LOAD SETTINGS --- Total time: {(t_end - t_start) * 1000:.2f} ms")
    print(f"[load] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

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
    print(f"\n[save] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"[save] --- START SAVE SETTINGS ---")
    
    token_data = getattr(request.state, "token_data", {})
    account_id = token_data.get("account_id")
    print(f"[save] Extracted account_id from token: {account_id}")

    if account_id:
        try:
            ws_result = db.table("workspaces") \
                .select("id, access_token") \
                .eq("monday_account_id", int(account_id)) \
                .single() \
                .execute()
            workspace = ws_result.data
        except Exception:
            workspace = None
        if not workspace:
            print(f"[save] ERROR: Workspace not found for account_id: {account_id}")
            raise HTTPException(status_code=404, detail="Workspace not found")
    else:
        print(f"[save] No account_id in token, falling back to body.workspaceId: {body.workspaceId}")
        workspace = get_workspace_by_monday_id(str(body.workspaceId), db)

    workspace_uuid = workspace["id"]
    access_token   = workspace["access_token"]
    print(f"[save] Workspace loaded successfully: uuid={workspace_uuid}")

    # ── DEBUG: log exact payload ──
    print(f"[save] automation_enabled={body.automation_enabled}")
    print(f"[save] sensitivity={body.sensitivity}")
    print(f"[save] boards={[(b.board_id, b.board_enabled) for b in body.boards] if body.boards else None}")

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
    #
    # Global OFF:
    #   → Delete webhooks for ALL boards that have a webhook_id
    #   → Clear webhook_id + set is_enabled=False for ALL boards
    #   → Skip Part 3 completely (ignore body.boards)
    #
    # Global ON:
    #   → Do nothing here
    #   → Part 3 handles creating webhooks based on body.boards
    # ══════════════════════════════════════════════════════
    if body.automation_enabled is not None and not body.automation_enabled:
        # ── Global OFF ──
        # Fetch ALL boards that have a webhook_id (regardless of is_enabled)
        try:
            all_boards_result = db.table("monitored_boards") \
                .select("board_id, webhook_id") \
                .eq("workspace_id", workspace_uuid) \
                .is_("deleted_at",  "null") \
                .execute()
            all_boards = all_boards_result.data or []
        except Exception:
            all_boards = []

        print(f"[save] Global OFF — processing {len(all_boards)} boards")

        async def _global_off_board(board):
            # Delete webhook from monday.com if exists
            if board.get("webhook_id"):
                print(f"[save] Deleting webhook {board['webhook_id']} for board {board['board_id']}")
                try:
                    await delete_webhook(
                        access_token = access_token,
                        webhook_id   = board["webhook_id"],
                    )
                except Exception as e:
                    print(f"[save] delete_webhook failed for board {board['board_id']}: {e}")

            # Clear webhook in DB — do NOT touch is_enabled so board stays in the list
            try:
                db.table("monitored_boards") \
                    .update({
                        "webhook_id":     None,
                        "webhook_status": "DISABLED",
                    }) \
                    .eq("workspace_id", workspace_uuid) \
                    .eq("board_id",     board["board_id"]) \
                    .execute()
            except Exception as e:
                print(f"[save] DB disable failed for board {board['board_id']}: {e}")

        await asyncio.gather(*[_global_off_board(b) for b in all_boards])

    # ══════════════════════════════════════════════════════
    # PART 3 — Individual board toggles
    # Runs for:
    #   - Normal save (sensitivity change, individual board toggles)
    #   - Global ON (frontend sends all boards with their desired states)
    #
    # ENABLE  → create webhook if missing, reuse if exists
    # DISABLE → delete webhook + clear webhook_id in DB
    # ══════════════════════════════════════════════════════
    failed_boards = []
    if body.boards is not None:

        # Fetch all current board states in one query
        try:
            all_existing_result = db.table("monitored_boards") \
                .select("*") \
                .eq("workspace_id", workspace_uuid) \
                .is_("deleted_at",  "null") \
                .execute()
            existing_map = {
                str(b["board_id"]): b
                for b in (all_existing_result.data or [])
            }
            print(f"[save] Loaded {len(existing_map)} existing boards from DB for this workspace.")
        except Exception:
            existing_map = {}

        async def process_board(board):
            existing = existing_map.get(str(board.board_id))
            error_board_name = None

            print(f"\n[save] ┌── Processing board: {board.board_id} ('{board.board_name}')")
            print(f"[save] │ Frontend sent: board_enabled={board.board_enabled}")
            print(f"[save] │ Global automation: {body.automation_enabled}")

            # Do we want to create/maintain a webhook? 
            # Only if board is checked AND global automation is not OFF
            wants_webhook = board.board_enabled and (body.automation_enabled is not False)
            print(f"[save] │ wants_webhook evaluated to: {wants_webhook}")

            # ── ENABLE or RE-ENABLE ──
            if wants_webhook:
                existing_webhook_id = existing.get("webhook_id") if existing else None

                if existing_webhook_id:
                    # Reuse existing webhook — no new automation created
                    print(f"[save] Reusing webhook {existing_webhook_id} for board {board.board_id}")
                    try:
                        db.table("monitored_boards") \
                            .update({
                                "is_enabled":     True,
                                "webhook_status": "ACTIVE",
                                "board_name":     board.board_name,
                            }) \
                            .eq("workspace_id", workspace_uuid) \
                            .eq("board_id",     board.board_id) \
                            .execute()
                    except Exception as e:
                        print(f"[save] DB update failed for board {board.board_id}: {e}")
                else:
                    # No webhook → create one
                    print(f"[save] Creating new webhook for board {board.board_id}")
                    webhook_id = await create_webhook(
                        access_token = access_token,
                        board_id     = board.board_id,
                        workspace_id = str(body.workspaceId),
                    )
                    if webhook_id is None:
                        print(f"[save] Webhook creation failed for board {board.board_id}")
                        error_board_name = board.board_name

                    row = {
                        "workspace_id":   workspace_uuid,
                        "board_id":       board.board_id,
                        "board_name":     board.board_name,
                        "is_enabled":     True if webhook_id else False,
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
                        print(f"[save] DB save failed for board {board.board_id}: {e}")

            # ── DISABLE (or Global OFF but board is checked) ──
            else:
                # Delete webhook → automation toggle goes OFF on monday.com
                if existing and existing.get("webhook_id"):
                    print(f"[save] Deleting webhook {existing['webhook_id']} for board {board.board_id}")
                    try:
                        await delete_webhook(
                            access_token = access_token,
                            webhook_id   = existing["webhook_id"],
                        )
                    except Exception as e:
                        print(f"[save] delete_webhook failed: {e}")

                row = {
                    "is_enabled":     board.board_enabled,  # Preserve the user's checkbox state!
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
                        # Insert the board if the user checked it (even if global automation is off)
                        if board.board_enabled:
                            row["workspace_id"]   = workspace_uuid
                            row["board_id"]       = board.board_id
                            row["board_name"]     = board.board_name
                            row["is_active"]      = True
                            db.table("monitored_boards").insert(row).execute()
                except Exception as e:
                    print(f"[save] DB update failed for board {board.board_id}: {e}")

            return error_board_name

        results = await asyncio.gather(*[process_board(b) for b in body.boards])
        failed_boards = [name for name in results if name is not None]

    # ── Return final board states ──
    try:
        final_boards = db.table("monitored_boards") \
            .select("board_id, board_name, is_enabled, webhook_status") \
            .eq("workspace_id", workspace_uuid) \
            .is_("deleted_at",  "null") \
            .execute()
        boards_data = final_boards.data or []
    except Exception:
        boards_data = []

    success = len(failed_boards) == 0
    message = "Settings saved successfully" if success else f"Failed to enable automation for: {', '.join(failed_boards)}"
    print(f"[save] --- END SAVE SETTINGS --- success={success}, failed_boards={len(failed_boards)}")
    print(f"[save] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    return {
        "success": success,
        "message": message,
        "failed_boards": failed_boards,
        "boards":  boards_data,
    }