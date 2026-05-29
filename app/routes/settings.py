# app/routes/settings.py
# ─────────────────────────────────────────────────────────────
# Settings API
#
# GET  /settings/{workspaceId} → when app loads
# POST /settings/{workspaceId} → when user clicks “Save”
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Depends

# Supabase DB client (admin access)
from app.core.database import get_supabase       

# Response + request schemas
from app.schemas.settings import SettingsResponse, SettingsSaveRequest


router = APIRouter(tags=["Settings"])


# ─────────────────────────────────────────
# Helper: resolve monday_workspace_id → internal UUID
# ─────────────────────────────────────────
def _get_workspace_uuid(monday_workspace_id: str, db) -> str:
    """
    Converts monday workspace ID (external) into internal UUID (used in DB).

    Why?
    → monday.com uses numeric workspace ID
    → Our DB uses UUID for better consistency and relations
    """
    try:
        result = db.table("workspaces") \
            .select("id") \
            .eq("monday_workspace_id", monday_workspace_id) \
            .single() \
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if not result.data:
        raise HTTPException(status_code=404, detail="Workspace not found")

    return result.data["id"]


# ─────────────────────────────────────────
# GET SETTINGS API
# ─────────────────────────────────────────
@router.get("/settings/{workspaceId}")
async def get_settings(workspaceId: str, db = Depends(get_supabase)):
    """
    This API is called when the frontend loads the app.

    Purpose:
    → Fetch saved settings from database
    → Send them back to frontend
    """

    # Resolve monday workspace ID → internal UUID
    # Must use internal UUID because workspace_settings.workspace_id is a UUID FK
    workspace_uuid = _get_workspace_uuid(workspaceId, db)

    try:
        # Fetch settings row for this workspace using internal UUID
        result = db.table("workspace_settings")\
            .select("*")\
            .eq("workspace_id", workspaceId)\
            .single()\
            .execute()

        # If no data returned
        if not result.data:
            raise HTTPException(
                status_code=404,
                detail="Settings not found"
            )

        data = result.data

        # Convert DB fields → frontend format
        # automation_enabled → workspace-level global toggle (workspace_settings.is_enabled)
        # board_enabled      → per-board toggle; not stored in workspace_settings,
        #                      so we default to True here (actual per-board state
        #                      comes from GET /boards/{workspaceId} → monitored_boards)
        return SettingsResponse(
            workspace_id       = workspaceId,
            board_id           = data.get("template_board_id"),
            sensitivity        = data.get("ai_sensitivity", "BALANCED"),
            automation_enabled = data.get("is_enabled", True),       # Global toggle
            board_enabled      = data.get("is_enabled", True),       # Per-board toggle
        )
    
    except HTTPException:
        raise   # re-raise known HTTP errors (404, etc.) as-is
    
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=f"Settings not found for workspace {workspaceId}"
        )


# ─────────────────────────────────────────
# POST /settings/{workspaceId} -> SAVE SETTINGS API
# ─────────────────────────────────────────
@router.post("/settings/{workspaceId}")
async def save_settings(
    workspaceId: str,
    body: SettingsSaveRequest,
    db = Depends(get_supabase)
):
    """
        Called when user clicks "Save Settings".

        It updates:
        → template board
        → AI sensitivity
        → automation toggle
        → board toggle
    """
    try:

        # Resolve monday workspace ID → internal UUID
        # Must use internal UUID because all FK relations use UUID, not monday ID
        workspace_uuid = _get_workspace_uuid(workspaceId, db)

        # This dictionary will store fields to update
        update_data = {}

        # ── 1. Update template board
        if body.board_id is not None:
            update_data.update({"template_board_id": body.board_id})
            update_data["template_board_deleted"] = False  # reset flag

        # ── 2. Update AI sensitivity
        if body.sensitivity is not None:
            # Convert to uppercase (DB enum format)
            update_data["ai_sensitivity"] = body.sensitivity.upper()

        # ── 3. Update global automation toggle
        if body.automation_enabled is not None:
            update_data["is_enabled"] = body.automation_enabled

        # ── 4. Update board-level toggle (global kill-switch for all monitored boards)
        # Note: fine-grained per-board toggling goes through POST /boards/{workspaceId}/{boardId}/toggle
        # This flag acts as a bulk enable/disable across all boards in the workspace
        if body.board_enabled is not None:
            try:
                db.table("monitored_boards")\
                    .update({"is_enabled": body.board_enabled})\
                    .eq("workspace_id", workspaceId)\
                    .execute()
            except Exception:
                # Ignore error (non-critical)
                pass

        # If nothing to update → return early
        if not update_data:
            return {"success": True, "message": "No changes to save"}

        # ── 5. Check if settings already exist
        try:
            existing = db.table("workspace_settings")\
                .select("id")\
                .eq("workspace_id", workspaceId)\
                .execute()
        except Exception:
            existing = None

        # ── 6. UPDATE existing row
        if existing and existing.data:
            try:
                db.table("workspace_settings")\
                    .update(update_data)\
                    .eq("workspace_id", workspaceId)\
                    .execute()
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to save settings: {str(e)}"
                )

        # ── 7. INSERT new row (first-time setup)
        else:
            update_data["workspace_id"] = workspace_uuid

            # Default values
            update_data.setdefault("ai_sensitivity", "BALANCED")
            update_data.setdefault("is_enabled", True)
            update_data.setdefault("ai_enabled", True)
            update_data.setdefault("exact_match_fallback_enabled", True)
            update_data.setdefault("onboarding_completed", False)

            try:
                db.table("workspace_settings")\
                    .insert(update_data)\
                    .execute()
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create settings: {str(e)}"
                )

        return {
            "success": True,
            "message": "Settings saved successfully"
        }
    
    except HTTPException:
        raise   # re-raise known HTTP errors as-is

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error while saving settings: {str(e)}"
        )