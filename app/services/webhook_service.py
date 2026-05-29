# app/services/webhook_service.py

# ─────────────────────────────────────────────────────────────
# This service handles:
# 1. Creating webhook (automation) on monday.com
# 2. Deleting webhook (disabling automation)
#
# Important:
# monday.com does NOT support pause/disable webhook
# So:
#   Disable = delete webhook
#   Enable  = create new webhook
# ─────────────────────────────────────────────────────────────

import httpx
from app.core.config import settings


# Base URL for monday.com GraphQL API
MONDAY_API_URL = settings.monday_api_url

# Event type → triggers when a new item is created on board
WEBHOOK_EVENT = "create_item"


# ─────────────────────────────────────────
# create_webhook()
# ─────────────────────────────────────────
# Purpose:
# Create a webhook (automation) on a specific board
# When new item is created → monday calls our backend URL
# Returns:
# webhook_id (int) → used later to delete/update webhook
# ─────────────────────────────────────────
async def create_webhook(
    access_token: str,
    board_id:     int,
    workspace_id: str,
) -> int:
    """
    Creates a webhook on monday.com board.

    We attach workspace_id in URL so when webhook fires,
    backend knows which workspace triggered it.

    Returns webhook ID.
    """

    # Build callback URL → where monday will send data
    webhook_url = f"{settings.app_base_url}/webhook/receive/{workspace_id}"

    # GraphQL mutation to create webhook
    mutation = """
    mutation CreateWebhook($boardId: ID!, $url: String!, $event: WebhookEventType!) {
      create_webhook(board_id: $boardId, url: $url, event: $event) {
        id
        board_id
      }
    }
    """

    # Variables for GraphQL query
    variables = {
        "boardId": str(board_id),   # board ID must be string
        "url":     webhook_url,     # our backend endpoint
        "event":   WEBHOOK_EVENT,   # trigger event
    }

    # Request headers
    headers = {
        "Authorization": access_token,  # monday OAuth token
        "Content-Type":  "application/json",
        "API-Version":   "2024-01",     # fixed API version
    }

    # Send request to monday.com
    async with httpx.AsyncClient() as client:
        response = await client.post(
            MONDAY_API_URL,
            json={"query": mutation, "variables": variables},
            headers=headers,
            timeout=15,
        )

    # Raise error if request failed
    response.raise_for_status()

    # Parse JSON response
    data = response.json()

    # Extract webhook ID
    webhook_id = data["data"]["create_webhook"]["id"]

    # Return webhook ID as integer
    return int(webhook_id)


# ─────────────────────────────────────────
# delete_webhook()
# ─────────────────────────────────────────
# Purpose:
# Delete (disable) webhook from monday.com
#
# Important:
# monday.com has NO pause → only delete
#
# Returns:
# True  → success
# False → failed (but safe to ignore)
# ─────────────────────────────────────────
async def delete_webhook(
    access_token: str,
    webhook_id:   str,
) -> bool:
    """
    Deletes a webhook from monday.com.

    If webhook already deleted → we ignore error
    and return False (safe handling).
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

    # Variables
    variables = {"webhookId": str(webhook_id)}

    # Headers
    headers = {
        "Authorization": access_token,
        "Content-Type":  "application/json",
        "API-Version":   "2024-01",
    }

    # Send request
    async with httpx.AsyncClient() as client:
        response = await client.post(
            MONDAY_API_URL,
            json={"query": mutation, "variables": variables},
            headers=headers,
            timeout=15,
        )

    # Do NOT raise error here
    # Because webhook might already be deleted → not critical
    if response.status_code != 200:
        return False

    # Parse response
    data = response.json()

    # If GraphQL returns errors → failure
    return "errors" not in data