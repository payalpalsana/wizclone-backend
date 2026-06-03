# app/services/settings_service.py
# ─────────────────────────────────────────────────────────────
# Shared DB helper functions
#
# These are plain functions (NOT FastAPI dependencies).
# They accept `db` as a parameter — call them from routes by
# passing the db instance directly.
#
# WHY A SEPARATE FILE?
# The same workspace/subitem lookups are needed in routes,
# workers, and services. Centralising them here avoids repeating
# the same DB query in every file.
# ─────────────────────────────────────────────────────────────

from fastapi import HTTPException
from supabase import Client


# ─────────────────────────────────────────
# Workspace helpers
# ─────────────────────────────────────────

def get_workspace_by_monday_id(monday_workspace_id: str, db: Client) -> dict:
    """
    Resolve monday_workspace_id (external numeric ID) → full workspace row.

    Returns the full workspace dict including:
      id            — internal UUID (used as FK everywhere)
      access_token  — monday API token (used for GraphQL calls)
      plan_tier     — FREE / PRO / BUSINESS
      is_active     — whether workspace is active
      is_paused     — whether workspace is paused

    Raises HTTP 404 if workspace not found.
    """
    try:
        result = db.table("workspaces") \
            .select("id, access_token") \
            .eq("monday_workspace_id", str(monday_workspace_id)) \
            .single() \
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if not result.data:
        raise HTTPException(status_code=404, detail="Workspace not found")

    return result.data


def get_workspace_uuid(monday_workspace_id: str, db: Client) -> str:
    """
    Shorthand — returns just the internal UUID string.
    Use when you only need the UUID and not other workspace fields.
    """
    return get_workspace_by_monday_id(monday_workspace_id, db)["id"]


# ─────────────────────────────────────────
# Template / subitem helpers
# ─────────────────────────────────────────
def get_subitems_for_template(template_id: str, db: Client) -> list[dict]:
    """
    Fetch all active (non-deleted) subitems for a template,
    ordered by sort_order ascending.

    Returns list of dicts: [{id, name, sort_order}, ...]
    Returns empty list if none found or query fails.
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


def get_workspace_sensitivity(workspace_uuid: str, db: Client) -> str:
    """
    Returns the AI sensitivity setting for the workspace.
    Falls back to "BALANCED" if settings row not found.
    """
    settings_row = get_workspace_settings(workspace_uuid, db)
    if settings_row:
        return settings_row.get("ai_sensitivity", "BALANCED")
    return "BALANCED"