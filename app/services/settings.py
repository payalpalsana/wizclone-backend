# app/services/settings.py

import httpx
from fastapi import HTTPException
from supabase import Client
from app.core.config import settings

MONDAY_API_URL = settings.monday_api_url


# ─────────────────────────────────────────
# Workspace helpers
# ─────────────────────────────────────────
def get_workspace_by_monday_id(monday_workspace_id: str, db: Client) -> dict:
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
    return get_workspace_by_monday_id(monday_workspace_id, db)["id"]


# ─────────────────────────────────────────
# monday.com board fetching
# ─────────────────────────────────────────

# monday.com board_kind values:
#   "public"   → normal boards  ✅ show these
#   "private"  → private boards ✅ show these
#   "share"    → shareable boards ✅ show these
#   "subtasks" → hidden subitem boards ❌ NEVER show — webhooks not allowed on these

EXCLUDED_BOARD_KINDS = {"subtasks"}


async def fetch_monday_boards(access_token: str, workspace_id: int) -> list[dict]:
    """
    Fetch boards from monday.com for a SPECIFIC workspace only.
    Filters out subitem boards (board_kind = subtasks) — monday.com
    does not allow webhook creation on those boards.

    Returns list of {id, name} dicts — only real boards the user can monitor.
    """

    query = """
    query GetBoards($workspaceIds: [ID!]) {
      boards(
        limit: 100,
        order_by: created_at,
        workspace_ids: $workspaceIds
      ) {
        id
        name
        board_kind
      }
    }
    """

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

    all_boards = data.get("data", {}).get("boards", [])

    # for b in all_boards:
    #     print(f"[DEBUG] board: '{b['name']}' — kind: '{b['board_kind']}'")

    # ── Filter out subitem boards ──
    # board_kind = "subtasks" → hidden internal board monday creates
    # for every board's subitems. Webhooks are NOT allowed on these.

    # real_boards = [
    #     b for b in all_boards
    #     if b.get("board_kind") not in EXCLUDED_BOARD_KINDS
    # ]

    real_boards = [
        b for b in all_boards
        if not b.get("name", "").startswith("Subitems of")
    ]

    print(
        f"[settings_service] Fetched {len(all_boards)} boards, "
        f"filtered to {len(real_boards)} (removed "
        f"{len(all_boards) - len(real_boards)} subitem boards)"
    )

    return real_boards


# ─────────────────────────────────────────
# Template / subitem helpers
# ─────────────────────────────────────────
def get_subitems_for_template(template_id: str, db: Client) -> list[dict]:
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
    settings_row = get_workspace_settings(workspace_uuid, db)
    if settings_row:
        return settings_row.get("ai_sensitivity", "BALANCED")
    return "BALANCED"