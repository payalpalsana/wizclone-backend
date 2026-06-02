# app/services/webhook_service.py

# ─────────────────────────────────────────────────────────────
# monday.com Webhook Management
#
# monday.com does NOT support pausing a webhook.
# The only options are: create or delete.
#
# So our logic is:
#   Board enabled  → create_webhook()  (registers automation on monday)
#   Board disabled → delete_webhook()  (removes automation from monday)
# ─────────────────────────────────────────────────────────────

import httpx
from app.core.config import settings

MONDAY_API_URL  = settings.monday_api_url
WEBHOOK_EVENT   = "create_item"    # fires when a new item is created on the board


async def create_webhook(
    access_token: str,
    board_id:     int,
    workspace_id: str,   # monday workspace ID (embedded in the callback URL)
) -> str | None:
    """
    Register a "create_item" webhook on a monday.com board.

    The callback URL contains the workspace ID so the receiver
    knows which workspace this event belongs to without any lookup.
    URL format: {app_base_url}/webhook/monday?workspaceId={workspace_id}

    Returns webhook_id (str) on success.
    Returns None if monday.com rejects (e.g. token issue, app not live).
    Errors are printed but never raised — caller handles None.
    """

    # Build the URL monday.com will POST to when an item is created
    callback_url = f"{settings.app_base_url}/webhook/monday?workspaceId={workspace_id}"

    # GraphQL mutation to create webhook
    mutation = """
    mutation CreateWebhook($boardId: ID!, $url: String!, $event: WebhookEventType!) {
      create_webhook(board_id: $boardId, url: $url, event: $event) {
        id
        board_id
      }
    }
    """

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                MONDAY_API_URL,
                json={
                    "query":     mutation,
                    "variables": {
                        "boardId": str(board_id),
                        "url":     callback_url,
                        "event":   WEBHOOK_EVENT,
                    },
                },
                headers={
                    "Authorization": access_token,
                    "Content-Type":  "application/json",
                    "API-Version":   "2024-01",
                },
            )

        data = response.json()

        # GraphQL errors (e.g. invalid token, board not found)
        if "errors" in data:
            print(f"[webhook_service] create_webhook GraphQL error: {data['errors']}")
            return None

        webhook_id = data.get("data", {}).get("create_webhook", {}).get("id")
        if not webhook_id:
            print(f"[webhook_service] create_webhook: no ID in response: {data}")
            return None

        return str(webhook_id)

    except Exception as e:
        print(f"[webhook_service] create_webhook exception: {e}")
        return None


async def delete_webhook(
    access_token: str,
    webhook_id:   str,
) -> bool:
    """
    Delete a webhook from monday.com.

    Returns True on success.
    Returns False on failure (safe to ignore — webhook may already be gone).
    Never raises — caller always gets a bool.
    """

    # GraphQL mutation to delete webhook
    mutation = """
    mutation DeleteWebhook($webhookId: ID!) {
      delete_webhook(id: $webhookId) {
        id
        board_id
      }
    }
    """

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                MONDAY_API_URL,
                json={
                    "query":     mutation,
                    "variables": {"webhookId": str(webhook_id)},
                },
                headers={
                    "Authorization": access_token,
                    "Content-Type":  "application/json",
                    "API-Version":   "2024-01",
                },
            )

        if response.status_code != 200:
            return False

        data = response.json()
        return "errors" not in data

    except Exception as e:
        print(f"[webhook_service] delete_webhook exception: {e}")
        return False