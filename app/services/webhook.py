# app/services/webhook.py

# ─────────────────────────────────────────────────────────────
# monday.com Webhook Management
#
# monday.com does NOT support pausing a webhook.
# The only options are: create or delete.
#
# Our strategy (avoids duplicate automations):
#   Board enabled (first time) → create_webhook() → save webhook_id in DB
#   Board disabled             → keep webhook_id in DB, flip is_enabled=false
#                                receiver returns severityCode 6000 → monday pauses it
#   Board re-enabled           → reuse existing webhook_id → no new webhook created
#   Board deleted from monday  → delete_webhook() + clear webhook_id (hard delete)
# ─────────────────────────────────────────────────────────────

import hmac
import hashlib
from datetime  import datetime, timezone
import httpx

from app.core.database import db as supabase_db
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

    IMPORTANT: Caller must check existing webhook_id before calling this.
    Call this only when webhook_id is None/missing in DB to avoid duplicates.
    """

    # Build the URL monday.com will POST to when an item is created
    callback_url = f"{settings.app_base_url}/webhook/monday/{workspace_id}"
    print(f"[webhook_service] Registering webhook → board: {board_id} | url: {callback_url}")

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
        async with httpx.AsyncClient(timeout=8) as client:
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

        if "errors" in data:
            errors = data["errors"]
            for err in errors:
                msg = err.get("message", "")

                # ✅ Subitem board — skip silently
                if "subitems board" in msg.lower():
                    print(f"[webhook_service] Skipping subitem board {board_id}")
                    return None

                # ✅ Internal Server Error = tunnel is dead
                if "internal server error" in msg.lower():
                    print(f"[webhook_service] ❌ Monday cannot reach: {callback_url}")
                    print(f"[webhook_service] ❌ Check your APP_BASE_URL in .env — tunnel may have expired")
                    return None

            print(f"[webhook_service] GraphQL error for board {board_id}: {errors}")
            return None

        webhook_id = data.get("data", {}).get("create_webhook", {}).get("id")
        if not webhook_id:
            print(f"[webhook_service] No webhook ID returned: {data}")
            return None

        print(f"[webhook_service] ✅ Webhook created: {webhook_id} for board {board_id}")
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

    Only called when a board is permanently removed from monday.com
    (detected during load_settings sync). Never called on simple disable.

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
        async with httpx.AsyncClient(timeout=8) as client:
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
        result = "errors" not in data
        print(f"[webhook_service] delete_webhook result: {result} | response: {data}")
        return result

    except Exception as e:
        print(f"[webhook_service] delete_webhook exception: {e}")
        return False


# ─────────────────────────────────────────
# Signature verification helper
# ─────────────────────────────────────────
def _verify_signature(body: bytes, signature: str) -> bool:
    """
    Verify monday.com HMAC-SHA256 webhook signature.
    monday sends the signature in the x-monday-signature header.

    If no signing_secret configured (dev/test mode) → skip verification.
    In production this MUST be enabled.
    """
    if not settings.monday_signing_secret:
        return True   # Development mode — skip

    expected = hmac.new(
        settings.monday_signing_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    # compare_digest prevents timing attacks
    return hmac.compare_digest(expected, signature)


# ─────────────────────────────────────────
# Plan limit helper
# ─────────────────────────────────────────
def _is_plan_limit_reached(workspace_uuid: str, plan_tier: str) -> bool:
    """
    Check if this workspace has hit its monthly copy limit.

    BUSINESS plan = unlimited → always False.
    FREE  = 50 copies / month
    PRO   = 500 copies / month

    Returns True  → limit reached, block this event
    Returns False → within limit, continue
    """
    if plan_tier == "BUSINESS":
        return False

    cycle_start = datetime.now(timezone.utc) \
        .replace(day=1, hour=0, minute=0, second=0, microsecond=0) \
        .date().isoformat()

    try:
        # How many copies used this month?
        usage = supabase_db.table("usage_metrics") \
            .select("copies_used") \
            .eq("workspace_id",      workspace_uuid) \
            .eq("billing_cycle_start", cycle_start) \
            .execute()

        copies_used = usage.data[0]["copies_used"] if usage.data else 0

        # What is the plan limit?
        plan = supabase_db.table("plans") \
            .select("max_copies_per_month") \
            .eq("plan_name", plan_tier) \
            .single() \
            .execute()

        max_copies = plan.data.get("max_copies_per_month") if plan.data else None

        if max_copies is None:
            return False   # NULL = unlimited

        return copies_used >= max_copies

    except Exception:
        return False   # On error → allow through (fail open)