# app/services/worker.py
# ─────────────────────────────────────────────────────────────
# Background Worker — Template Matching + Subitem Copy
#
# Run as a separate process (separate terminal):
#   python -m app.services.worker
#
# WHY SEPARATE FROM main.py?
#   FastAPI (main.py) handles HTTP requests and MUST respond fast.
#   monday.com expects a webhook response in under 3 seconds.
#   Matching + copying 10 subitems can take 5–10 seconds.
#   So webhook.py saves the job to queue_jobs and returns 200 immediately.
#   This worker polls queue_jobs every 3 seconds and does the real work.
#
#   Terminal 1: uvicorn app.main:app          ← HTTP server
#   Terminal 2: python -m app.services.worker ← background jobs
#   Both share the same Supabase DB.
#
# Worker flow per job (C-04 + C-05):
#   1.  Get workspace access_token + plan_tier
#   2.  Get workspace sensitivity + check global automation toggle
#   3.  Fetch all active templates for workspace from templates table
#   4.  If no templates → NO_MATCH → done
#   5.  Call matching_service.match_item_to_template() [C-04]
#   6.  If confidence below threshold → NO_MATCH → done
#   7.  Fetch subitems from matched template (ordered by sort_order)
#   8.  Create each subitem on monday.com via create_subitem() [C-05]
#   9.  Update automation_events (SUCCESS / PARTIAL_SUCCESS / FAILED)
#   10. Update usage_metrics + usage_logs
# ─────────────────────────────────────────────────────────────

import asyncio
import traceback
from datetime import datetime, timezone, timedelta

from app.core.database   import db as supabase_db
from app.services.monday_services   import create_subitem
from app.services.matching_services  import match_item_to_template

# How often the worker polls queue_jobs (seconds)
POLL_INTERVAL = 3

# Retry back-off delays: 1st retry after 1s, 2nd after 4s, 3rd after 16s
RETRY_DELAYS = [1, 4, 16]


# ══════════════════════════════════════════════════════════════
# Main job processor
# ══════════════════════════════════════════════════════════════

async def process_job(job: dict):
    """
    Process a single MATCHING job end-to-end.
    Called by the polling loop for each pending job.
    """
    job_id     = job["id"]
    payload    = job.get("payload", {})
    start_time = datetime.now(timezone.utc)

    item_id             = payload.get("item_id")
    item_name           = payload.get("item_name", "")
    workspace_uuid      = payload.get("workspace_id")
    automation_event_id = payload.get("automation_event_id")

    print(f"[Worker] Job {job_id} — item: '{item_name}' (id: {item_id})")

    # Mark job as RUNNING immediately
    supabase_db.table("queue_jobs").update({
        "status":     "RUNNING",
        "started_at": start_time.isoformat(),
    }).eq("id", job_id).execute()

    try:

        # ── Step 1: Get workspace access_token ──
        ws = supabase_db.table("workspaces") \
            .select("access_token, plan_tier") \
            .eq("id", workspace_uuid) \
            .single() \
            .execute()

        if not ws.data:
            raise Exception("Workspace not found")

        access_token = ws.data["access_token"]
        plan_tier    = ws.data.get("plan_tier", "FREE")

        # ── Step 2: Get sensitivity + global automation toggle ──
        ws_settings = supabase_db.table("workspace_settings") \
            .select("ai_sensitivity, is_enabled") \
            .eq("workspace_id", workspace_uuid) \
            .single() \
            .execute()

        sensitivity = "BALANCED"

        if ws_settings.data:
            # Global automation disabled → skip silently
            if not ws_settings.data.get("is_enabled", True):
                print(f"[Worker] Automation disabled for workspace — skipping job {job_id}")
                await _complete_job(job_id)
                return
            sensitivity = ws_settings.data.get("ai_sensitivity", "BALANCED")

        # ── Step 3: Fetch all active templates from templates table ──
        # These are the workspace's templates stored in our DB.
        # NOT fetched from monday.com — the user manages them inside WizClone.
        templates_result = supabase_db.table("templates") \
            .select("id, name, usage_count") \
            .eq("workspace_id", workspace_uuid) \
            .eq("is_active",    True) \
            .eq("is_deleted",   False) \
            .execute()

        templates = templates_result.data or []

        # ── Step 4: No templates → NO_MATCH ──
        if not templates:
            print(f"[Worker] No templates found for workspace — NO_MATCH")
            await _update_event(
                event_id      = automation_event_id,
                status        = "NO_MATCH",
                copied        = 0,
                failed        = 0,
                template_id   = None,
                template_name = None,
                confidence    = 0.0,
                method        = "EXACT_MATCH",
                ai_used       = False,
                processing_ms = _elapsed_ms(start_time),
            )
            _update_usage(workspace_uuid, copies_added=0, is_no_match=True)
            await _complete_job(job_id)
            return

        # ── Step 5: C-04 — Match item name to template ──
        # matching_service handles:
        #   - difflib fuzzy scoring
        #   - substring boosts
        #   - sensitivity threshold check
        #   - Models API slot (when monday approves access)
        match = await match_item_to_template(
            item_name    = item_name,
            templates    = templates,          # list of {id, name, usage_count}
            access_token = access_token,       # used by Models API when enabled
            sensitivity  = sensitivity,
        )

        print(
            f"[Worker] Match result: '{match['matched_name']}' "
            f"— {match['confidence']}% "
            f"(threshold: {match['threshold']}%) "
            f"method: {match['method']}"
        )

        # ── Step 6: Confidence below threshold → NO_MATCH ──
        if not match["above_threshold"]:
            print(f"[Worker] NO_MATCH — confidence {match['confidence']}% < {match['threshold']}%")
            await _update_event(
                event_id      = automation_event_id,
                status        = "NO_MATCH",
                copied        = 0,
                failed        = 0,
                template_id   = None,
                template_name = None,
                confidence    = match["confidence"],
                method        = match["method"],
                ai_used       = match["ai_used"],
                processing_ms = _elapsed_ms(start_time),
            )
            _update_usage(workspace_uuid, copies_added=0, is_no_match=True)
            await _complete_job(job_id)
            return

        # Match confirmed
        template_id   = match["matched_id"]
        template_name = match["matched_name"]
        confidence    = match["confidence"]

        # ── Step 7: Fetch subitems from matched template ──
        # sort_order is critical — subitems must be created in correct sequence
        subitems_result = supabase_db.table("template_subitems") \
            .select("id, name, sort_order") \
            .eq("template_id", template_id) \
            .is_("deleted_at", "null") \
            .order("sort_order") \
            .execute()

        subitems = subitems_result.data or []

        if not subitems:
            print(f"[Worker] Template '{template_name}' has no subitems — skipping")
            await _complete_job(job_id)
            return

        print(f"[Worker] Fetching {len(subitems)} subitems for template '{template_name}':")
        for idx, sub in enumerate(subitems):
            print(f"  {idx+1}. {sub.get('name', '')} (order: {sub.get('sort_order', '')})")

        # ── Step 8: C-05 — Create each subitem on monday.com ──
        copied_count = 0
        failed_count = 0
        failed_names = []

        print(f"[Worker] Commencing monday.com copy process...")
        for subitem in subitems:
            subitem_name = subitem.get("name", "").strip()
            if not subitem_name:
                continue

            result = await create_subitem(item_id, subitem_name, access_token)

            if result["success"]:
                copied_count += 1
                print(f"[Worker]   ✓ '{subitem_name}'")
            else:
                failed_count += 1
                failed_names.append(subitem_name)
                print(f"[Worker]   ✗ '{subitem_name}' — {result['error']}")

        # ── Determine final status ──
        if   failed_count == 0:   final_status = "SUCCESS"
        elif copied_count > 0:    final_status = "PARTIAL_SUCCESS"
        else:                     final_status = "FAILED"

        processing_ms = _elapsed_ms(start_time)
        print(
            f"[Worker] {final_status} — "
            f"{copied_count} copied, {failed_count} failed "
            f"({processing_ms}ms)"
        )

        # ── Step 9: Update automation_events ──
        await _update_event(
            event_id      = automation_event_id,
            status        = final_status,
            copied        = copied_count,
            failed        = failed_count,
            template_id   = template_id,
            template_name = template_name,
            confidence    = confidence,
            method        = None,
            ai_used       = match["ai_used"],
            processing_ms = processing_ms,
            failed_names  = failed_names,
        )

        # Update template usage_count (non-critical)
        try:
            matched_template = next(
                (t for t in templates if t["id"] == template_id), None
            )
            if matched_template:
                supabase_db.table("templates").update({
                    "usage_count":  (matched_template.get("usage_count") or 0) + 1,
                    "last_used_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", template_id).execute()
        except Exception:
            pass

        # ── Step 10: Usage metrics + logs ──
        if final_status in ("SUCCESS", "PARTIAL_SUCCESS"):
            _update_usage(
                workspace_uuid = workspace_uuid,
                copies_added   = copied_count,
                is_no_match    = False,
                ai_used        = match["ai_used"],
            )

        await _complete_job(job_id)

    except Exception as e:
        attempt_count = (job.get("attempt_count") or 0) + 1
        max_attempts  = job.get("max_attempts") or 3
        print(f"[Worker] Job {job_id} error (attempt {attempt_count}/{max_attempts}): {e}")
        traceback.print_exc()
        _handle_job_failure(job_id, automation_event_id, attempt_count, max_attempts, str(e))


# ══════════════════════════════════════════════════════════════
# Step 10 — Usage tracking
# ══════════════════════════════════════════════════════════════

def _update_usage(
    workspace_uuid: str,
    copies_added:   int  = 0,
    is_no_match:    bool = False,
    ai_used:        bool = False,
):
    """
    Upsert usage_metrics for current billing cycle.
    Insert a row into usage_logs for granular tracking.
    """
    now         = datetime.now(timezone.utc)
    cycle_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).date().isoformat()

    cycle_end = (
        now.replace(year=now.year + 1, month=1, day=1)
        if now.month == 12
        else now.replace(month=now.month + 1, day=1)
    ).date().isoformat()

    try:
        existing = supabase_db.table("usage_metrics") \
            .select(
                "id, copies_used, ai_match_calls, exact_match_count, "
                "no_match_count, total_automation_runs, success_runs"
            ) \
            .eq("workspace_id",        workspace_uuid) \
            .eq("billing_cycle_start", cycle_start) \
            .execute()

        if existing.data:
            row = existing.data[0]
            supabase_db.table("usage_metrics").update({
                "copies_used":           row["copies_used"]           + copies_added,
                "ai_match_calls":        row["ai_match_calls"]        + (1 if ai_used else 0),
                "exact_match_count":     row["exact_match_count"]     + (0 if ai_used or is_no_match else 1),
                "no_match_count":        row["no_match_count"]        + (1 if is_no_match else 0),
                "total_automation_runs": row["total_automation_runs"] + 1,
                "success_runs":          row["success_runs"]          + (0 if is_no_match else 1),
                "last_copy_at":          now.isoformat(),
            }).eq("id", row["id"]).execute()

        else:
            supabase_db.table("usage_metrics").insert({
                "workspace_id":          workspace_uuid,
                "billing_cycle_start":   cycle_start,
                "billing_cycle_end":     cycle_end,
                "copies_used":           copies_added,
                "ai_match_calls":        1 if ai_used else 0,
                "exact_match_count":     0 if ai_used or is_no_match else 1,
                "no_match_count":        1 if is_no_match else 0,
                "total_automation_runs": 1,
                "success_runs":          0 if is_no_match else 1,
                "failed_runs":           0,
                "last_copy_at":          now.isoformat(),
            }).execute()

        # Granular log entry
        supabase_db.table("usage_logs").insert({
            "workspace_id":    workspace_uuid,
            "event_type":      "NO_MATCH" if is_no_match else ("AI_MATCH" if ai_used else "EXACT_MATCH"),
            "ai_credits_used": 0,
            "credit_cost":     0,
            "metadata_json":   {
                "copies_added": copies_added,
                "ai_used":      ai_used,
                "is_no_match":  is_no_match,
            },
        }).execute()

    except Exception as e:
        print(f"[Worker] Usage update error: {e}")


# ══════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════

async def _update_event(
    event_id:      str,
    status:        str,
    copied:        int,
    failed:        int,
    template_id:   str | None,
    template_name: str | None,
    confidence:    float,
    method:        str  = "EXACT_MATCH",
    ai_used:       bool = False,
    processing_ms: int  = 0,
    failed_names:  list = None,
):
    """Update automation_events row with final job result."""
    update_data = {
        "status":           status,
        "subitems_copied":  copied,
        "subitems_failed":  failed,
        "confidence_score": confidence / 100.0,   # store as 0.0–1.0 in DB
        "match_method":     method,
        "ai_fallback_used": not ai_used,
        "processing_ms":    processing_ms,
        "completed_at":     datetime.now(timezone.utc).isoformat(),
    }

    if template_id:    update_data["matched_template_id"]   = template_id
    if template_name:  update_data["matched_template_name"] = template_name
    if failed_names:   update_data["failed_subitem_names"]  = failed_names

    try:
        supabase_db.table("automation_events") \
            .update(update_data) \
            .eq("id", event_id) \
            .execute()
    except Exception as e:
        print(f"[Worker] Event update error: {e}")


async def _complete_job(job_id: str):
    """Mark queue_jobs row as COMPLETED."""
    try:
        supabase_db.table("queue_jobs").update({
            "status":       "COMPLETED",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()
    except Exception as e:
        print(f"[Worker] Complete job error: {e}")


def _handle_job_failure(
    job_id:              str,
    automation_event_id: str,
    attempt_count:       int,
    max_attempts:        int,
    error:               str,
):
    """
    Retry with exponential back-off or mark permanently FAILED.
    Retry delays: 1s → 4s → 16s
    """
    now = datetime.now(timezone.utc)

    if attempt_count < max_attempts:
        delay    = RETRY_DELAYS[min(attempt_count - 1, len(RETRY_DELAYS) - 1)]
        retry_at = (now + timedelta(seconds=delay)).isoformat()

        try:
            supabase_db.table("queue_jobs").update({
                "status":        "PENDING",
                "attempt_count": attempt_count,
                "last_error":    error,
                "next_retry_at": retry_at,
                "available_at":  retry_at,
            }).eq("id", job_id).execute()
        except Exception as e:
            print(f"[Worker] Reset job for retry error: {e}")

    else:
        try:
            supabase_db.table("queue_jobs").update({
                "status":        "FAILED",
                "failed_at":     now.isoformat(),
                "attempt_count": attempt_count,
                "last_error":    error,
            }).eq("id", job_id).execute()

            if automation_event_id:
                supabase_db.table("automation_events").update({
                    "status":        "FAILED",
                    "error_details": f"Max retries exceeded. Last error: {error}",
                    "completed_at":  now.isoformat(),
                }).eq("id", automation_event_id).execute()

        except Exception as e:
            print(f"[Worker] Mark failed error: {e}")


def _elapsed_ms(start: datetime) -> int:
    """Return milliseconds elapsed since start."""
    return int((datetime.now(timezone.utc) - start).total_seconds() * 1000)


# ══════════════════════════════════════════════════════════════
# Main polling loop
# ══════════════════════════════════════════════════════════════

async def run_worker():
    """
    Polls queue_jobs every 3 seconds.
    Picks up to 5 PENDING MATCHING jobs per poll.
    Processes them concurrently using asyncio.gather().
    """
    print("[Worker] WizClone background worker started")
    print(f"[Worker] Polling every {POLL_INTERVAL}s")

    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()

            jobs = supabase_db.table("queue_jobs") \
                .select("*") \
                .eq("status",   "PENDING") \
                .eq("job_type", "MATCHING") \
                .or_(f"next_retry_at.is.null,next_retry_at.lte.{now}") \
                .order("priority",   desc=True) \
                .order("created_at", desc=False) \
                .limit(5) \
                .execute()

            if jobs.data:
                print(f"[Worker] Found {len(jobs.data)} job(s)")
                await asyncio.gather(*[process_job(job) for job in jobs.data])

        except Exception as e:
            print(f"[Worker] Poll error: {e}")
            traceback.print_exc()

        await asyncio.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run_worker())