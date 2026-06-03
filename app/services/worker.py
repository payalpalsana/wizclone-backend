# app/services/worker.py
# ─────────────────────────────────────────────────────────────
# Background Worker — Template Matching + Subitem Copy
#
# Run as a separate process:
#   python -m app.services.worker
#
# Polls queue_jobs every POLL_INTERVAL seconds for PENDING MATCHING jobs.
# For each job, runs steps 9-17:
#
#   9.  Fetch all templates for workspace
#   10. Run AI matching (monday AI Blocks or fuzzy fallback)
#   11. Check confidence vs sensitivity threshold
#   12. NO MATCH → update event (NO_MATCH), done
#   13. MATCH → fetch subitems from matched template
#   14. Write each subitem to monday.com (create_subitem mutation)
#   15. Update automation_events (SUCCESS / PARTIAL_SUCCESS / FAILED)
#   16. Increment usage_metrics (copies_used++)
#   17. Log to usage_logs
#
# AI STRATEGY (per client requirement):
#   Primary  → monday.com AI Blocks (native, no third-party AI)
#   Fallback → fuzzy string matching via difflib
#   The fallback activates automatically if AI Blocks is unavailable.
# ─────────────────────────────────────────────────────────────

import asyncio
import traceback
import difflib
import httpx
from datetime import datetime, timezone, timedelta

from app.core.database   import db as supabase_db
from app.core.config     import settings
from app.services.monday_services import create_subitem

MONDAY_API_URL = settings.monday_api_url

# How often the worker polls for new jobs (seconds)
POLL_INTERVAL = 3

# Retry back-off delays in seconds: 1st retry after 1s, 2nd after 4s, 3rd after 16s
RETRY_DELAYS = [1, 4, 16]

# Confidence thresholds per sensitivity level (0–100 scale)
SENSITIVITY_THRESHOLDS = {
    "STRICT":   90,
    "BALANCED": 75,
    "LOOSE":    55,
}


# ══════════════════════════════════════════════════════════════
# STEP 10: AI Matching
# ══════════════════════════════════════════════════════════════

async def _run_ai_matching(
    item_name:      str,
    template_names: list[str],
    access_token:   str,
) -> dict:
    """
    Match item_name against all template_names.

    PRIMARY: monday.com AI Blocks
    FALLBACK: Python difflib fuzzy matching (activates automatically if AI fails)

    Returns:
    {
        "matched_name":  str | None,   # template name that best matches
        "confidence":    float,        # 0–100
        "credits_used":  int,
        "method":        "AI" | "EXACT_MATCH",
        "ai_fallback":   bool,         # True if fuzzy fallback was used
    }
    """

    # ── Try monday.com AI Blocks first ──
    # TODO: Replace this block with the actual monday.com AI Blocks API call
    # when the endpoint is confirmed by monday.com.
    #
    # Expected shape (to be confirmed with monday.com):
    #
    # mutation {
    #   use_ai_block(
    #     block_type: "text_match",
    #     input: {
    #       query: "{item_name}",
    #       candidates: {template_names}
    #     }
    #   ) {
    #     result        # best matching name
    #     confidence    # 0-100
    #     credits_used
    #   }
    # }
    #
    # async with httpx.AsyncClient(timeout=10) as client:
    #     response = await client.post(
    #         MONDAY_API_URL,
    #         json={"query": mutation},
    #         headers={"Authorization": access_token, "Content-Type": "application/json"},
    #     )
    # data = response.json()
    # return {
    #     "matched_name":  data["data"]["use_ai_block"]["result"],
    #     "confidence":    data["data"]["use_ai_block"]["confidence"],
    #     "credits_used":  data["data"]["use_ai_block"]["credits_used"],
    #     "method":        "AI",
    #     "ai_fallback":   False,
    # }
    #
    # ── END TODO ──

    # ── Fallback: fuzzy string matching ──
    # Active until monday AI Blocks is confirmed, and permanently active
    # as fallback when AI Blocks returns an error or is unavailable.
    return _fuzzy_match(item_name, template_names)


def _fuzzy_match(item_name: str, template_names: list[str]) -> dict:
    """
    Fuzzy string matching using Python's built-in difflib.
    No external dependencies — zero latency, zero cost.

    Scoring:
    - Base: difflib SequenceMatcher similarity ratio × 100
    - Boost to 85 if item name contains template name (substring)
    - Boost to 80 if template name contains item name (substring)
    """
    item_lower = item_name.lower().strip()
    best_name  = None
    best_score = 0.0

    for name in template_names:
        name_lower = name.lower().strip()

        # Similarity ratio (0.0 to 1.0) × 100 → percentage
        score = difflib.SequenceMatcher(None, item_lower, name_lower).ratio() * 100

        # Substring boosts — handles cases like:
        # "New Client Onboarding — Acme Corp" matches "New Client Onboarding"
        if name_lower in item_lower:
            score = max(score, 85.0)
        if item_lower in name_lower:
            score = max(score, 80.0)

        if score > best_score:
            best_score = score
            best_name  = name

    return {
        "matched_name": best_name,
        "confidence":   round(best_score, 2),
        "credits_used": 0,              # No monday.com AI credits for fuzzy match
        "method":       "EXACT_MATCH",  # Logged as EXACT_MATCH (fallback method)
        "ai_fallback":  True,
    }


# ══════════════════════════════════════════════════════════════
# Job processor — Steps 9-17
# ══════════════════════════════════════════════════════════════

async def process_job(job: dict):
    """
    Process a single MATCHING job end-to-end.
    Called by the main worker loop for each pending job.
    """
    job_id     = job["id"]
    payload    = job.get("payload", {})
    start_time = datetime.now(timezone.utc)

    item_id             = payload.get("item_id")
    item_name           = payload.get("item_name", "")
    workspace_uuid      = payload.get("workspace_id")
    automation_event_id = payload.get("automation_event_id")

    print(f"[Worker] Job {job_id} — item: '{item_name}' ({item_id})")

    # Mark job as RUNNING immediately
    supabase_db.table("queue_jobs").update({
        "status":     "RUNNING",
        "started_at": start_time.isoformat(),
    }).eq("id", job_id).execute()

    try:
        # ── Get workspace access token + settings ──
        ws = supabase_db.table("workspaces") \
            .select("access_token, plan_tier") \
            .eq("id", workspace_uuid) \
            .single() \
            .execute()

        if not ws.data:
            raise Exception("Workspace not found")

        access_token = ws.data["access_token"]

        # ── Get AI sensitivity + check global automation toggle ──
        ws_settings = supabase_db.table("workspace_settings") \
            .select("ai_sensitivity, is_enabled") \
            .eq("workspace_id", workspace_uuid) \
            .single() \
            .execute()

        if ws_settings.data:
            # If automation globally disabled → skip silently
            if not ws_settings.data.get("is_enabled", True):
                print(f"[Worker] Automation disabled for workspace — skipping")
                await _complete_job(job_id)
                return

            sensitivity = ws_settings.data.get("ai_sensitivity", "BALANCED")
        else:
            sensitivity = "BALANCED"

        threshold = SENSITIVITY_THRESHOLDS.get(sensitivity, 75)

        # ── Step 9: Fetch all templates ──
        templates_result = supabase_db.table("templates") \
            .select("id, name, usage_count") \
            .eq("workspace_id", workspace_uuid) \
            .eq("is_active",    True) \
            .eq("is_deleted",   False) \
            .execute()

        templates = templates_result.data or []

        if not templates:
            print(f"[Worker] No templates found — NO_MATCH")
            # await _update_event(automation_event_id, "NO_MATCH", 0, 0, None, None, 0)
            await _update_event(
                event_id      = automation_event_id,
                status        = "NO_MATCH",
                copied        = 0,
                failed        = 0,
                template_id   = None,
                template_name = None,
                confidence    = 0,
                match_method  = None,
                ai_fallback   = True,
                processing_ms = _elapsed_ms(start_time),
            )
            _update_usage(workspace_uuid, 0, 0, False, True)
            await _complete_job(job_id)
            return

        template_names = [t["name"] for t in templates]

        # ── Step 10: AI matching ──
        ai_result    = await _run_ai_matching(item_name, template_names, access_token)
        matched_name = ai_result.get("matched_name")
        confidence   = ai_result.get("confidence", 0)
        credits_used = ai_result.get("credits_used", 0)
        match_method = ai_result.get("method", "EXACT_MATCH")
        ai_fallback  = ai_result.get("ai_fallback", True)

        print(f"[Worker] Match: '{matched_name}' — {confidence}% (threshold: {threshold}%)")

        # ── Step 11-12: Confidence check ──
        if not matched_name or confidence < threshold:
            print(f"[Worker] NO_MATCH — confidence {confidence}% < {threshold}%")
            processing_ms = _elapsed_ms(start_time)
            await _update_event(
                automation_event_id,
                status       = "NO_MATCH",
                copied       = 0,
                failed       = 0,
                template_id  = None,
                template_name= None,
                confidence   = confidence,
                match_method = match_method,
                ai_fallback  = ai_fallback,
                processing_ms= processing_ms,
            )
            _update_usage(workspace_uuid, 0, credits_used, not ai_fallback, True)
            await _complete_job(job_id)
            return

        # ── Find matched template object ──
        matched_template = next((t for t in templates if t["name"] == matched_name), None)
        if not matched_template:
            await _complete_job(job_id)
            return

        template_id = matched_template["id"]

        # ── Step 13: Fetch subitems from matched template ──
        # Order by sort_order is critical — subitems must be created in the right sequence
        subitems_result = supabase_db.table("template_subitems") \
            .select("id, name, sort_order") \
            .eq("template_id", template_id) \
            .is_("deleted_at", "null") \
            .order("sort_order") \
            .execute()

        subitems = subitems_result.data or []

        if not subitems:
            print(f"[Worker] Template '{matched_name}' has no subitems — skipping")
            await _complete_job(job_id)
            return

        # ── Step 14: Write each subitem to monday.com ──
        copied_count = 0
        failed_count = 0
        failed_names = []

        for subitem in subitems:
            subitem_name = subitem.get("name", "")
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

        # Determine final status
        if   failed_count == 0:              final_status = "SUCCESS"
        elif copied_count > 0:               final_status = "PARTIAL_SUCCESS"
        else:                                final_status = "FAILED"

        processing_ms = _elapsed_ms(start_time)
        print(f"[Worker] {final_status} — {copied_count} copied, {failed_count} failed ({processing_ms}ms)")

        # ── Step 15: Update automation_events ──
        await _update_event(
            automation_event_id,
            status            = final_status,
            copied            = copied_count,
            failed            = failed_count,
            template_id       = template_id,
            template_name     = matched_name,
            confidence        = confidence,
            match_method      = match_method,
            ai_fallback       = ai_fallback,
            processing_ms     = processing_ms,
            failed_names      = failed_names,
        )

        # Update template usage count (non-critical)
        try:
            supabase_db.table("templates").update({
                "usage_count":  (matched_template.get("usage_count") or 0) + 1,
                "last_used_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", template_id).execute()
        except Exception:
            pass

        # ── Step 16 + 17: Usage metrics + logs ──
        if final_status in ("SUCCESS", "PARTIAL_SUCCESS"):
            _update_usage(
                workspace_uuid = workspace_uuid,
                copies_added   = copied_count,
                ai_credits     = credits_used,
                is_ai_match    = not ai_fallback,
                is_no_match    = False,
            )

        await _complete_job(job_id)

    except Exception as e:
        # Unexpected error — decide whether to retry or fail permanently
        attempt_count = (job.get("attempt_count") or 0) + 1
        max_attempts  = job.get("max_attempts") or 3
        print(f"[Worker] Job {job_id} error (attempt {attempt_count}/{max_attempts}): {e}")
        traceback.print_exc()
        _handle_job_failure(job_id, automation_event_id, attempt_count, max_attempts, str(e))


# ══════════════════════════════════════════════════════════════
# Step 16 + 17: Usage tracking
# ══════════════════════════════════════════════════════════════

def _update_usage(
    workspace_uuid: str,
    copies_added:   int,
    ai_credits:     int,
    is_ai_match:    bool,
    is_no_match:    bool,
):
    """
    Step 16: Upsert usage_metrics for the current billing cycle
    Step 17: Insert a row into usage_logs for granular tracking
    """
    now         = datetime.now(timezone.utc)
    cycle_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).date().isoformat()

    # Calculate cycle end (first day of next month)
    if now.month == 12:
        cycle_end = now.replace(year=now.year + 1, month=1, day=1).date().isoformat()
    else:
        cycle_end = now.replace(month=now.month + 1, day=1).date().isoformat()

    try:
        existing = supabase_db.table("usage_metrics") \
            .select("id, copies_used, ai_match_calls, exact_match_count, no_match_count, total_automation_runs, success_runs") \
            .eq("workspace_id",       workspace_uuid) \
            .eq("billing_cycle_start", cycle_start) \
            .execute()

        if existing.data:
            row = existing.data[0]
            supabase_db.table("usage_metrics").update({
                "copies_used":          row["copies_used"]          + copies_added,
                "ai_match_calls":       row["ai_match_calls"]       + (1 if is_ai_match else 0),
                "exact_match_count":    row["exact_match_count"]    + (0 if is_ai_match or is_no_match else 1),
                "no_match_count":       row["no_match_count"]       + (1 if is_no_match else 0),
                "total_automation_runs":row["total_automation_runs"] + 1,
                "success_runs":         row["success_runs"]         + (0 if is_no_match else 1),
                "last_copy_at":         now.isoformat(),
            }).eq("id", row["id"]).execute()
        else:
            # First event this billing cycle — create the row
            supabase_db.table("usage_metrics").insert({
                "workspace_id":          workspace_uuid,
                "billing_cycle_start":   cycle_start,
                "billing_cycle_end":     cycle_end,
                "copies_used":           copies_added,
                "ai_match_calls":        1 if is_ai_match else 0,
                "exact_match_count":     0 if is_ai_match or is_no_match else 1,
                "no_match_count":        1 if is_no_match else 0,
                "total_automation_runs": 1,
                "success_runs":          0 if is_no_match else 1,
                "failed_runs":           0,
                "last_copy_at":          now.isoformat(),
            }).execute()

        # Step 17: Granular log entry
        supabase_db.table("usage_logs").insert({
            "workspace_id":    workspace_uuid,
            "event_type":      "AI_MATCH" if is_ai_match else ("NO_MATCH" if is_no_match else "EXACT_MATCH"),
            "ai_credits_used": ai_credits,
            "credit_cost":     0,   # Fill in actual cost when monday AI Blocks pricing is confirmed
            "metadata_json":   {
                "copies_added": copies_added,
                "is_ai_match":  is_ai_match,
                "is_no_match":  is_no_match,
            },
        }).execute()

    except Exception as e:
        # Non-critical — don't fail the job for a metrics error
        print(f"[Worker] Usage update error: {e}")


# ══════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════

async def _update_event(
    event_id:      str,
    status:        str,
    copied:        int,
    failed:        int,
    template_id,
    template_name,
    confidence:    float,
    match_method:  str  = None,
    ai_fallback:   bool = False,
    processing_ms: int  = 0,
    failed_names:  list = None,
):
    """Update automation_events with the final result."""
    update_data = {
        "status":           status,
        "subitems_copied":  copied,
        "subitems_failed":  failed,
        "confidence_score": confidence,
        "ai_fallback_used": ai_fallback,
        "processing_ms":    processing_ms,
        "completed_at":     datetime.now(timezone.utc).isoformat(),
    }
    if template_id:    update_data["matched_template_id"]   = template_id
    if template_name:  update_data["matched_template_name"] = template_name
    if match_method:   update_data["match_method"]          = match_method
    if failed_names:   update_data["failed_subitem_names"]  = failed_names

    try:
        supabase_db.table("automation_events") \
            .update(update_data) \
            .eq("id", event_id) \
            .execute()
    except Exception as e:
        print(f"[Worker] Event update error: {e}")


async def _complete_job(job_id: str):
    """Mark a queue_jobs row as COMPLETED."""
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
    Handle a failed job.
    If attempts remain → reset to PENDING with exponential back-off delay.
    If max attempts hit → mark FAILED permanently.
    """
    now = datetime.now(timezone.utc)

    if attempt_count < max_attempts:
        # Exponential back-off: 1s → 4s → 16s
        delay     = RETRY_DELAYS[min(attempt_count - 1, len(RETRY_DELAYS) - 1)]
        retry_at  = (now + timedelta(seconds=delay)).isoformat()

        try:
            supabase_db.table("queue_jobs").update({
                "status":        "PENDING",   # Reset so it gets picked up again
                "locked_at":     None,        # Release lock
                "attempt_count": attempt_count,
                "last_error":    error,
                "next_retry_at": retry_at,
                "available_at":  retry_at,    # Don't pick up until delay expires
            }).eq("id", job_id).execute()
        except Exception as e:
            print(f"[Worker] Reset job for retry error: {e}")
    else:
        # Permanently failed
        try:
            supabase_db.table("queue_jobs").update({
                "status":        "FAILED",
                "failed_at":     now.isoformat(),
                "attempt_count": attempt_count,
                "last_error":    error,
            }).eq("id", job_id).execute()

            if automation_event_id:
                supabase_db.table("automation_events").update({
                    "status":       "FAILED",
                    "error_details": f"Max retries exceeded. Last: {error}",
                    "completed_at":  now.isoformat(),
                }).eq("id", automation_event_id).execute()

        except Exception as e:
            print(f"[Worker] Mark failed error: {e}")


def _elapsed_ms(start: datetime) -> int:
    """Return milliseconds elapsed since start."""
    return int((datetime.now(timezone.utc) - start).total_seconds() * 1000)


# ══════════════════════════════════════════════════════════════
# Main worker loop
# ══════════════════════════════════════════════════════════════

async def run_worker():
    """
    Main polling loop.
    Picks up to 5 PENDING MATCHING jobs every POLL_INTERVAL seconds.
    Processes up to 5 jobs concurrently using asyncio.gather().
    """
    print("[Worker] WizClone background worker started")
    print(f"[Worker] Polling every {POLL_INTERVAL}s")

    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()

            # Fetch PENDING jobs that are due (available_at <= now)
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
            # Top-level safety net — worker must never crash
            print(f"[Worker] Poll error: {e}")
            traceback.print_exc()

        await asyncio.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run_worker())