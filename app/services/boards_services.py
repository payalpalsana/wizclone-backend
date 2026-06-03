import httpx
from fastapi import HTTPException
from app.core.database import get_db


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