# app/services/monday_service.py
# ─────────────────────────────────────────────────────────────
# monday.com GraphQL API helpers
#
# All calls to monday.com live here.
# Routes and workers import from this file — never call the
# monday API directly from route handlers.
# ─────────────────────────────────────────────────────────────

import httpx
from app.core.config import settings

MONDAY_API_URL = settings.monday_api_url


# ─────────────────────────────────────────
# User / workspace info
# ─────────────────────────────────────────

async def get_user_info(access_token: str) -> dict:
    """
    Fetch the current user's profile and account info from monday.com.
    Used in /api/auth/init to save workspace + user to DB.

    Returns the raw monday.com response dict.
    Raises httpx.HTTPError on failure.
    """
    query = """
    query {
      me {
        id
        name
        email
        account 
        { 
          id  
          name 
        }
      }
    }
    """
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            MONDAY_API_URL,
            json={"query": query},
            headers={
                "Authorization": access_token,
                "Content-Type":  "application/json",
            },
        )

    # Raise error if request fails
    response.raise_for_status()

    # Return user data
    return response.json()


# ─────────────────────────────────────────
# Subitem creation
# ─────────────────────────────────────────

async def create_subitem(
    parent_item_id: int,
    subitem_name:   str,
    access_token:   str,
) -> dict:
    """
    Create a single subitem under a parent item in monday.com.

    Returns:
        {"success": True,  "subitem_id": "123"}   on success
        {"success": False, "error": "..."}         on failure

    Never raises — worker loops call this per-subitem and
    a single failure must not stop the rest.
    """
    mutation = """
    mutation CreateSubitem($parentId: ID!, $name: String!) {
      create_subitem(parent_item_id: $parentId, item_name: $name) {
        id
        name
      }
    }
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                MONDAY_API_URL,
                json={
                    "query":     mutation,
                    "variables": {
                        "parentId": str(parent_item_id),
                        "name":     subitem_name,
                    },
                },
                headers={
                    "Authorization": access_token,
                    "Content-Type":  "application/json",
                    "API-Version":   "2024-01",
                },
            )

        if response.status_code != 200:
            return {"success": False, "error": f"HTTP {response.status_code}"}

        data = response.json()

        if "errors" in data:
            return {"success": False, "error": str(data["errors"])}

        subitem_id = data.get("data", {}).get("create_subitem", {}).get("id")
        if not subitem_id:
            return {"success": False, "error": "No subitem ID returned"}

        return {"success": True, "subitem_id": subitem_id}

    except Exception as e:
        return {"success": False, "error": str(e)}