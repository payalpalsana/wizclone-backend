# app/routes/webhook.py
# ─────────────────────────────────────────────────────────────
# Webhook Receiver
#
# POST /webhook/monday?workspaceId={workspaceId}
#
# Called by monday.com every time a new item is created on
# a monitored board. MUST respond 200 in < 3 seconds.
# All heavy work is handed to the background worker via queue_jobs.
#
# Flow:
#   1. Verify HMAC signature (monday signs every webhook)
#   2. Challenge echo     → one-time verification ping from monday
#   3. Dedup check        → webhook_deduplication table
#   4. Workspace check    → workspaces table
#   5. Board enabled?     → monitored_boards table
#   6. Plan limit check   → usage_metrics + plans tables
#   7. Save event         → automation_events (status = NO_MATCH as default)
#   8. Enqueue job        → queue_jobs (worker picks this up)
#   9. Return 200 NOW
# ─────────────────────────────────────────────────────────────

import hmac
import hashlib
import json
from datetime  import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.database import db as supabase_db
from app.core.config import settings

router = APIRouter(tags=["Webhook"])


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


# ─────────────────────────────────────────
# POST /webhook/monday
# ─────────────────────────────────────────
@router.post("/webhook")
async def receive_webhook(request: Request):
    """
    Main webhook receiver.

    workspaceId is passed as a query param in the webhook URL
    (embedded when we registered the webhook via create_webhook()).
    This tells us which workspace triggered the event without a DB lookup.
    """

    # ── Read raw body (needed for signature check before parsing) ──
    body = await request.body()

    # ── Step 1: Verify HMAC signature ──
    # monday.com signs every POST with HMAC-SHA256 using the signing secret
    signature = request.headers.get("x-monday-signature", "")
    if signature and not _verify_signature(body, signature):
        # Return 200 not 401 — returning errors causes monday to retry forever
        return JSONResponse({"status": "ignored", "reason": "invalid signature"})

    # ── Parse JSON body ──
    try:
        payload = json.loads(body)
    except Exception:
        return JSONResponse({"status": "ignored", "reason": "invalid JSON"})

    # ── Step 2: Challenge echo ──
    # monday.com sends {"challenge": "..."} once when registering the webhook.
    # We must echo it back immediately to confirm ownership of the URL.
    # After this first ping, real events will start flowing.
    if "challenge" in payload:
        return JSONResponse({"challenge": payload["challenge"]})

    # ── Extract event fields ──
    event = payload.get("event", {})
    if not event:
        return JSONResponse({"status": "ignored", "reason": "no event data"})

    # monday.com calls items "pulses" internally
    event_id   = str(event.get("id", ""))          # unique event identifier
    item_id    = event.get("pulseId")               # the new item's ID
    item_name  = event.get("pulseName", "")         # the new item's name
    board_id   = event.get("boardId")               # which board it was created on
    account_id = event.get("accountId")             # monday account ID

    # workspaceId is embedded in the webhook URL as a query param
    workspace_monday_id = request.query_params.get("workspaceId", "")

    if not all([event_id, item_id, board_id]):
        return JSONResponse({"status": "ignored", "reason": "missing required fields"})

    # Cross-validate the workspaceId param against the event's accountId
    # They come from different sources — mismatch means something is wrong
    if workspace_monday_id and str(account_id) != str(workspace_monday_id):
        # Look up by workspace_id as a secondary validation
        try:
            ws_check = supabase_db.table("workspaces") \
                .select("monday_account_id") \
                .eq("monday_workspace_id", workspace_monday_id) \
                .single() \
                .execute()
            if ws_check.data and str(ws_check.data["monday_account_id"]) != str(account_id):
                return JSONResponse({"status": "ignored", "reason": "workspace/account mismatch"})
        except Exception:
            pass  # If check fails, continue — fail open

    # ── Step 3: Dedup check ──
    # monday.com guarantees at-least-once delivery — it CAN fire the same event twice.
    # Check webhook_deduplication before doing anything else.
    try:
        dedup = supabase_db.table("webhook_deduplication") \
            .select("event_id") \
            .eq("event_id", event_id) \
            .execute()

        if dedup.data:
            return JSONResponse({"status": "ignored", "reason": "duplicate event"})
    except Exception:
        pass   # If dedup check fails → continue (better to process twice than drop)

    # ── Step 4: Auth check — find workspace ──
    # Look up workspace by monday_account_id (from event payload)
    # to get internal UUID and validate the workspace is active
    try:
        ws_result = supabase_db.table("workspaces") \
            .select("id, access_token, is_active, is_paused, plan_tier, status") \
            .eq("monday_account_id", account_id) \
            .single() \
            .execute()
    except Exception:
        return JSONResponse({"status": "ignored", "reason": "workspace not found"})

    if not ws_result.data:
        return JSONResponse({"status": "ignored", "reason": "workspace not found"})

    workspace      = ws_result.data
    workspace_uuid = workspace["id"]

    # Workspace must be active
    if not workspace.get("is_active") or workspace.get("status") == "UNINSTALLED":
        return JSONResponse({"status": "ignored", "reason": "workspace inactive"})

    # Workspace must not be paused (template board deleted / token revoked)
    if workspace.get("is_paused"):
        return JSONResponse({"status": "ignored", "reason": "workspace paused"})

    # ── Step 5: Board enabled check ──
    # The webhook fires for every item on the board, but the user may have
    # disabled this specific board in WizClone settings.
    try:
        board_result = supabase_db.table("monitored_boards") \
            .select("is_enabled, webhook_status, board_name") \
            .eq("workspace_id", workspace_uuid) \
            .eq("board_id",     board_id) \
            .is_("deleted_at",  "null") \
            .single() \
            .execute()
        board_record = board_result.data
    except Exception:
        board_record = None

    if not board_record:
        return JSONResponse({"status": "ignored", "reason": "board not monitored"})

    if not board_record.get("is_enabled"):
        return JSONResponse({"status": "ignored", "reason": "board disabled"})

    board_name = board_record.get("board_name") or ""
    
    # ── Step 6: Plan limit check ──
    plan_tier = workspace.get("plan_tier", "FREE")
    if _is_plan_limit_reached(workspace_uuid, plan_tier):
        # Log the blocked event so the user can see it in Activity Log
        try:
            supabase_db.table("automation_events").insert({
                "workspace_id":  workspace_uuid,
                "item_id":       item_id,
                "item_name":     item_name,
                "board_id":      board_id,
                "event_id":      event_id,
                "trigger_type":  "WEBHOOK",
                "status":        "FAILED",
                "error_details": f"{plan_tier} plan monthly limit reached",
            }).execute()
        except Exception:
            pass

        return JSONResponse({
            "status": "ignored",
            "reason": f"plan limit reached — upgrade from {plan_tier}",
        })

    # ── Mark event as received (dedup record) ──
    # Inserted AFTER all checks pass to prevent race-condition duplicates
    try:
        supabase_db.table("webhook_deduplication").insert({
            "event_id":     event_id,
            "workspace_id": workspace_uuid,
            "board_id":     board_id,
            "item_id":      item_id,
            "is_processed": False,
            "expires_at":   (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        }).execute()
    except Exception:
        # Unique constraint violation → another request already inserted it (race condition)
        return JSONResponse({"status": "ignored", "reason": "duplicate event (race)"})

    # ── Step 7: Save automation event ──
    # Default status = NO_MATCH; worker will update to SUCCESS/FAILED/PARTIAL_SUCCESS
    try:
        event_result = supabase_db.table("automation_events").insert({
            "workspace_id": workspace_uuid,
            "item_id":      item_id,
            "item_name":    item_name,
            "board_id":     board_id,
            "board_name":   board_name,
            "event_id":     event_id,
            "trigger_type": "WEBHOOK",
            "status":       "NO_MATCH",
        }).execute()

        automation_event_id = event_result.data[0]["id"]

    except Exception as e:
        # Return 200 anyway — prevents monday.com from retrying endlessly
        return JSONResponse({"status": "error", "reason": str(e)})

    # ── Step 8: Enqueue job for background worker ──
    # Worker picks this up and runs steps 9-17 (matching + subitem copy)
    try:
        supabase_db.table("queue_jobs").insert({
            "workspace_id":        workspace_uuid,
            "automation_event_id": automation_event_id,
            "job_type":            "MATCHING",
            "status":              "PENDING",
            "max_attempts":        3,
            "priority":            1,
            "payload": {
                "item_id":             item_id,
                "item_name":           item_name,
                "board_id":            board_id,
                "workspace_id":        workspace_uuid,
                "event_id":            event_id,
                "automation_event_id": automation_event_id,
                "plan_tier":           plan_tier,
            },
        }).execute()
    except Exception:
        pass   # Non-critical — event is saved; worker will retry on next poll

    # ── Step 9: Return 200 immediately ──
    # monday.com expects a response within 3 seconds.
    # Worker processes the job asynchronously.
    return JSONResponse({
        "status":              "queued",
        "event_id":            event_id,
        "item_name":           item_name,
        "automation_event_id": automation_event_id,
    })