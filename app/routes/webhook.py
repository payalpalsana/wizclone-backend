# app/routes/webhook.py
# ─────────────────────────────────────────────────────────────
# Webhook Receiver
#
# POST /webhook/monday/{workspace_id}
#
# workspace_id in URL = monday WORKSPACE ID (e.g. 13120926)
#
# monday.com actual payload fields (confirmed from live test):
#   triggerUuid   → unique event ID (use for dedup)
#   pulseId       → item ID
#   pulseName     → item name
#   boardId       → board ID
#   userId        → who created it
#   subscriptionId → webhook subscription ID
#
# Note: monday does NOT send accountId in webhook events.
# We look up workspace using monday_workspace_id from the URL path.
# ─────────────────────────────────────────────────────────────

import json
import hmac
import hashlib
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.database import db as supabase_db
from app.core.config import settings

router = APIRouter(tags=["Webhook"])


def _verify_signature(body: bytes, signature: str) -> bool:
    if not settings.monday_signing_secret:
        return True
    expected = hmac.new(
        settings.monday_signing_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _is_plan_limit_reached(workspace_uuid: str, plan_tier: str) -> bool:
    if plan_tier == "BUSINESS":
        return False

    cycle_start = datetime.now(timezone.utc) \
        .replace(day=1, hour=0, minute=0, second=0, microsecond=0) \
        .date().isoformat()

    try:
        usage = supabase_db.table("usage_metrics") \
            .select("copies_used") \
            .eq("workspace_id",        workspace_uuid) \
            .eq("billing_cycle_start", cycle_start) \
            .execute()

        copies_used = usage.data[0]["copies_used"] if usage.data else 0

        plan = supabase_db.table("plans") \
            .select("max_copies_per_month") \
            .eq("plan_name", plan_tier) \
            .single() \
            .execute()

        max_copies = plan.data.get("max_copies_per_month") if plan.data else None

        if max_copies is None:
            return False

        return copies_used >= max_copies

    except Exception:
        return False


@router.post("/webhook/monday/{workspace_id}")
async def receive_webhook(request: Request, workspace_id: str):

    body = await request.body()

    # ── Step 1: Verify signature ──
    signature = request.headers.get("x-monday-signature", "")
    if signature and not _verify_signature(body, signature):
        print("[Webhook] Invalid signature")
        return JSONResponse({"status": "ignored", "reason": "invalid signature"})

    # ── Parse JSON ──
    try:
        payload = json.loads(body)
    except Exception:
        return JSONResponse({"status": "ignored", "reason": "invalid JSON"})

    # ── Step 2: Challenge echo ──
    if "challenge" in payload:
        print(f"[Webhook] Challenge echo")
        return JSONResponse({"challenge": payload["challenge"]})

    # ── Step 3: Extract event fields ──
    event = payload.get("event", {})
    if not event:
        return JSONResponse({"status": "ignored", "reason": "no event data"})

    # monday.com actual field names (confirmed from live payload):
    event_id  = str(event.get("triggerUuid", ""))
    item_id   = event.get("pulseId")
    item_name = event.get("pulseName", "")
    board_id  = event.get("boardId")

    print(f"[Webhook] item:'{item_name}' board:{board_id} event_id:{event_id}")

    if not all([event_id, item_id, board_id]):
        print(f"[Webhook] Missing fields — event_id:{event_id} item_id:{item_id} board_id:{board_id}")
        return JSONResponse({"status": "ignored", "reason": "missing required fields"})

    # ── Step 4: Find workspace ──
    # monday does NOT send accountId in webhook payloads.
    # Use workspace_id from URL path (monday workspace ID).
    # Fallback: try monday_account_id in case stored differently.
    workspace = None

    try:
        ws = supabase_db.table("workspaces") \
            .select("id, access_token, is_active, is_paused, plan_tier, status") \
            .eq("monday_workspace_id", int(workspace_id)) \
            .single() \
            .execute()
        if ws.data:
            workspace = ws.data
    except Exception:
        pass

    if not workspace:
        try:
            ws = supabase_db.table("workspaces") \
                .select("id, access_token, is_active, is_paused, plan_tier, status") \
                .eq("monday_account_id", int(workspace_id)) \
                .single() \
                .execute()
            if ws.data:
                workspace = ws.data
        except Exception as e:
            print(f"[Webhook] Workspace lookup failed: {e}")

    if not workspace:
        print(f"[Webhook] No workspace found for workspace_id {workspace_id}")
        return JSONResponse({"status": "ignored", "reason": "workspace not found"})

    workspace_uuid = workspace["id"]
    print(f"[Webhook] Workspace found: {workspace_uuid}")

    # ── Workspace active check ──
    if not workspace.get("is_active") or workspace.get("status") == "UNINSTALLED":
        return JSONResponse({"status": "ignored", "reason": "workspace inactive"})

    if workspace.get("is_paused"):
        return JSONResponse({"status": "ignored", "reason": "workspace paused"})

    # ── Step 5: Board enabled check ──
    try:
        board_result = supabase_db.table("monitored_boards") \
            .select("is_enabled, webhook_status, board_name") \
            .eq("workspace_id", workspace_uuid) \
            .eq("board_id",     int(board_id)) \
            .is_("deleted_at",  "null") \
            .single() \
            .execute()
        board_record = board_result.data
    except Exception as e:
        print(f"[Webhook] Board lookup failed: {e}")
        board_record = None

    if not board_record:
        print(f"[Webhook] Board {board_id} not monitored")
        return JSONResponse({"status": "ignored", "reason": "board not monitored"})

    if not board_record.get("is_enabled"):
        print(f"[Webhook] Board {board_id} disabled")
        return JSONResponse({"status": "ignored", "reason": "board disabled"})

    board_name = board_record.get("board_name") or ""
    print(f"[Webhook] Board '{board_name}' enabled")

    # ── Step 6: Dedup check ──
    try:
        dedup = supabase_db.table("webhook_deduplication") \
            .select("event_id") \
            .eq("event_id", event_id) \
            .execute()

        if dedup.data:
            print(f"[Webhook] Duplicate event {event_id}")
            return JSONResponse({"status": "ignored", "reason": "duplicate event"})
    except Exception:
        pass

    # ── Step 7: Plan limit check ──
    plan_tier = workspace.get("plan_tier", "FREE")
    if _is_plan_limit_reached(workspace_uuid, plan_tier):
        print(f"[Webhook] Plan limit reached: {plan_tier}")
        try:
            supabase_db.table("automation_events").insert({
                "workspace_id":  workspace_uuid,
                "item_id":       int(item_id),
                "item_name":     item_name,
                "board_id":      int(board_id),
                "event_id":      event_id,
                "trigger_type":  "WEBHOOK",
                "status":        "FAILED",
                "error_details": f"{plan_tier} plan monthly limit reached",
            }).execute()
        except Exception:
            pass
        return JSONResponse({"status": "ignored", "reason": f"plan limit reached"})

    # ── Insert dedup record ──
    try:
        supabase_db.table("webhook_deduplication").insert({
            "event_id":     event_id,
            "workspace_id": workspace_uuid,
            "board_id":     int(board_id),
            "item_id":      int(item_id),
            "is_processed": False,
            "expires_at":   (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        }).execute()
    except Exception as e:
        print(f"[Webhook] Dedup insert failed (race): {e}")
        return JSONResponse({"status": "ignored", "reason": "duplicate event (race)"})

    # ── Step 8: Save automation_event ──
    try:
        event_result = supabase_db.table("automation_events").insert({
            "workspace_id": workspace_uuid,
            "item_id":      int(item_id),
            "item_name":    item_name,
            "board_id":     int(board_id),
            "board_name":   board_name,
            "event_id":     event_id,
            "trigger_type": "WEBHOOK",
            "status":       "NO_MATCH",
        }).execute()

        automation_event_id = event_result.data[0]["id"]
        print(f"[Webhook] automation_event saved: {automation_event_id}")

    except Exception as e:
        print(f"[Webhook] Failed to save automation_event: {e}")
        return JSONResponse({"status": "error", "reason": str(e)})

    # ── Step 9: Save queue_job ──
    try:
        supabase_db.table("queue_jobs").insert({
            "workspace_id":        workspace_uuid,
            "automation_event_id": automation_event_id,
            "job_type":            "MATCHING",
            "status":              "PENDING",
            "max_attempts":        3,
            "priority":            1,
            "payload": {
                "item_id":             int(item_id),
                "item_name":           item_name,
                "board_id":            int(board_id),
                "workspace_id":        workspace_uuid,
                "event_id":            event_id,
                "automation_event_id": automation_event_id,
                "plan_tier":           plan_tier,
            },
        }).execute()
        print(f"[Webhook] queue_job saved — worker will process")

    except Exception as e:
        print(f"[Webhook] Failed to save queue_job: {e}")

    return JSONResponse({
        "status":              "queued",
        "event_id":            event_id,
        "item_name":           item_name,
        "automation_event_id": automation_event_id,
    })