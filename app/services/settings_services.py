# app/services/settings_service.py

import httpx
from fastapi import HTTPException
from supabase import Client
from app.core.config import settings

MONDAY_API_URL = settings.monday_api_url


# ─────────────────────────────────────────
# Workspace helpers
# ─────────────────────────────────────────
def get_workspace_by_monday_id(monday_workspace_id: str, db: Client) -> dict:
    """
    Resolve monday_workspace_id → full workspace row.
    Falls back to monday_account_id if workspace_id lookup misses
    (happens when OAuth callback stored account_id before workspace_id was known).
    Returns: id, access_token
    Raises HTTP 404 if not found.
    """
    # Try monday_workspace_id first
    try:
        result = db.table("workspaces") \
            .select("id, access_token, monday_account_id") \
            .eq("monday_workspace_id", str(monday_workspace_id)) \
            .single() \
            .execute()
        if result.data:
            return result.data
    except Exception:
        pass

    # Fallback: treat the value as monday_account_id
    try:
        result = db.table("workspaces") \
            .select("id, access_token, monday_account_id") \
            .eq("monday_account_id", str(monday_workspace_id)) \
            .single() \
            .execute()
        if result.data:
            return result.data
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="Workspace not found")


def get_workspace_uuid(monday_workspace_id: str, db: Client) -> str:
    """Shorthand — returns just the internal UUID string."""
    return get_workspace_by_monday_id(monday_workspace_id, db)["id"]


# ─────────────────────────────────────────
# monday.com board fetching
# Fetches boards ONLY for specific workspace
# (merged from boards.py)
# ─────────────────────────────────────────
async def fetch_monday_boards(access_token: str, workspace_id: int) -> list[dict]:
    """
    Fetch boards from monday.com for a SPECIFIC workspace only.

    Uses workspace_ids filter in GraphQL so we never get
    boards from other workspaces even if token has access to them.

    Returns list of {id, name} dicts.
    Raises exception on failure.
    """

    # All (public + private + subitem)
    query = """
    query GetBoards($workspaceIds: [ID!]) {
      boards(
        limit: 100,
        order_by: created_at,
        workspace_ids: $workspaceIds,
        board_kind: public
      ) {
        id
        name
        board_kind
      }
    }
    """

    # # Public only
    # query = """
    # query GetBoards($workspaceIds: [ID!]) {
    # boards(
    #     limit: 100,
    #     order_by: created_at,
    #     workspace_ids: $workspaceIds,
    #     board_kind: public
    # ) {
    #     id
    #     name
    # }
    # }
    # """

    # # Private only
    # query = """
    # query GetBoards($workspaceIds: [ID!]) {
    # boards(
    #     limit: 100,
    #     order_by: created_at,
    #     workspace_ids: $workspaceIds,
    #     board_kind: public
    # ) {
    #     id
    #     name
    # }
    # }
    # """

    # # Subitems only
    # query = """
    # query GetBoards($workspaceIds: [ID!]) {
    # boards(
    #     limit: 100,
    #     order_by: created_at,
    #     workspace_ids: $workspaceIds,
    #     board_kind: share
    # ) {
    #     id
    #     name
    # }
    # }
    # """

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            MONDAY_API_URL,
            json={
                "query":     query,
                "variables": {
                    "workspaceIds": [str(workspace_id)],
                },
            },
            headers={
                "Authorization": access_token,
                "Content-Type":  "application/json",
                "API-Version":   "2024-01",
            },
        )
    response.raise_for_status()
    data = response.json()

    if "errors" in data:
        raise Exception(f"monday.com GraphQL error: {data['errors']}")

    return data.get("data", {}).get("boards", [])




# ─────────────────────────────────────────
# Template / subitem helpers
# ─────────────────────────────────────────
def get_subitems_for_template(template_id: str, db: Client) -> list[dict]:
    """
    Fetch all active subitems for a template ordered by sort_order.
    Returns list of {id, name, sort_order} dicts.
    """
    try:
        result = db.table("template_subitems") \
            .select("id, name, sort_order") \
            .eq("template_id", template_id) \
            .is_("deleted_at", "null") \
            .order("sort_order", desc=False) \
            .execute()
        return result.data or []
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch subitems for template {template_id}: {str(e)}",
        )


# ─────────────────────────────────────────
# Settings helpers
# ─────────────────────────────────────────
def get_workspace_settings(workspace_uuid: str, db: Client) -> dict | None:
    """
    Fetch workspace_settings row for the given internal UUID.
    Returns the settings dict, or None if not found.
    """
    try:
        result = db.table("workspace_settings") \
            .select("*") \
            .eq("workspace_id", workspace_uuid) \
            .single() \
            .execute()
        return result.data
    except Exception:
        return None


def get_workspace_uuid_for_request(request, workspace_id_fallback: str, db: Client) -> str:
    """
    Resolve workspace UUID from session token account_id (preferred)
    or monday_workspace_id path param (fallback).
    Consistent with the pattern used in settings and auth routes.
    """
    from fastapi import HTTPException
    token_data = getattr(request.state, "token_data", {})
    account_id = token_data.get("account_id")

    if account_id:
        try:
            result = db.table("workspaces") \
                .select("id") \
                .eq("monday_account_id", int(account_id)) \
                .single() \
                .execute()
            if result.data:
                return result.data["id"]
        except Exception:
            pass

    return get_workspace_uuid(workspace_id_fallback, db)


def get_workspace_sensitivity(workspace_uuid: str, db: Client) -> str:
    """
    Returns AI sensitivity for the workspace.
    Falls back to BALANCED if not found.
    """
    settings_row = get_workspace_settings(workspace_uuid, db)
    if settings_row:
        return settings_row.get("ai_sensitivity", "BALANCED")
    return "BALANCED"