# app/routes/activity_log.py
# ─────────────────────────────────────────────────────────────
# GET /api/activity-log/{workspaceId}
#
# Returns paginated automation_events for a workspace.
# Supports filtering by status tab: all | success | no_match | failed
# Supports search by item name or template name.
#
# Response shape matches exactly what the frontend Activity Log
# page displays:
#   - item_name
#   - template_matched      (name of matched template, or None)
#   - confidence            (0-100 float, or None)
#   - status                (SUCCESS | FAILED | NO_MATCH | PARTIAL_SUCCESS)
#   - time_ago              (human-readable relative time string)
#   - expanded detail:
#       board_name, duration_ms, subitems_copied (list of names),
#       error_details
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Query, Depends
from supabase import Client
from app.core.database import get_db
from typing import Optional, List
from app.services.activity_log_services import (
    _time_ago, _duration_str, _get_copied_subitem_names,
)
from app.services.settings_services import get_workspace_uuid
from app.schemas.activity_log_schemas import ActivityLogResponse, ActivityLogItem

router = APIRouter(prefix="/api", tags=["Activity Log"])

# ═══════════════════════════════════════════════════════════
# GET /api/activity-log/{workspaceId}
# ═══════════════════════════════════════════════════════════
@router.get("/activity-log/{workspaceId}", response_model=ActivityLogResponse)
async def get_activity_log(
    workspaceId: str,
    # ── Filter tab ──
    # all | success | no_match | failed
    status:  Optional[str] = Query(default=None, description="Filter: success | no_match | failed | partial_success"),
    # ── Search ──
    search:  Optional[str] = Query(default=None, description="Search by item name or template name"),
    # ── Pagination ──
    page:    int           = Query(default=1,  ge=1),
    limit:   int           = Query(default=20, ge=1, le=100),
    db:      Client        = Depends(get_db),
):
    """
    Returns paginated activity log for the workspace.

    Tab mapping (frontend → status filter):
      ALL           → no filter
      SUCCESS       → status = SUCCESS or PARTIAL_SUCCESS
      NO MATCH      → status = NO_MATCH
      FAILED        → status = FAILED

    Each row returns:
      - id, item_name, item_id, board_id, board_name
      - template_matched (name), template_id
      - confidence (float or null)
      - status
      - time_ago (human string)
      - duration (human string, e.g. "Completed in 1.8s")
      - subitems_copied_names (list of names with checkmarks)
      - subitems_failed_names (list of names that failed)
      - subitems_copied_count
      - subitems_failed_count
      - error_details (shown in red when status=FAILED or NO_MATCH with error)
      - match_method (EXACT_MATCH | AI | FALLBACK)
      - ai_fallback_used
    """

    workspace_uuid = get_workspace_uuid(workspaceId, db)
    offset         = (page - 1) * limit

    # ── Build status filter ──
    # Frontend tabs:
    #   ALL            → fetch everything
    #   SUCCESS        → SUCCESS + PARTIAL_SUCCESS
    #   NO MATCH       → NO_MATCH
    #   FAILED         → FAILED
    status_filter: list[str] | None = None

    if status:
        s = status.lower()
        if s == "success":
            status_filter = ["SUCCESS", "PARTIAL_SUCCESS"]
        elif s == "no_match":
            status_filter = ["NO_MATCH"]
        elif s == "failed":
            status_filter = ["FAILED"]
        # "all" or unknown → no filter

    # ── Query automation_events ──
    try:
        query = db.table("automation_events") \
            .select(
                "id, item_id, item_name, board_id, board_name, "
                "matched_template_id, matched_template_name, "
                "confidence_score, status, processing_ms, "
                "subitems_copied, subitems_failed, failed_subitem_names, "
                "error_details, match_method, ai_fallback_used, "
                "created_at, completed_at",
                count="exact",
            ) \
            .eq("workspace_id", workspace_uuid) \
            .order("created_at", desc=True) \
            .range(offset, offset + limit - 1)

        # Apply status filter
        if status_filter:
            if len(status_filter) == 1:
                query = query.eq("status", status_filter[0])
            else:
                # Supabase: filter OR across multiple values
                query = query.in_("status", status_filter)

        # Apply search filter (item name or template name)
        if search and search.strip():
            s = search.strip()
            query = query.or_(
                f"item_name.ilike.%{s}%,"
                f"matched_template_name.ilike.%{s}%"
            )

        result = query.execute()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch activity log: {str(e)}")

    events     = result.data or []
    total      = result.count or 0
    total_pages = (total + limit - 1) // limit if total > 0 else 1

    # ── Build response items ──
    items: list[ActivityLogItem] = []

    for ev in events:
        # Fetch copied subitem names for expanded detail
        copied_names = _get_copied_subitem_names(
            template_id      = ev.get("matched_template_id"),
            subitems_copied  = ev.get("subitems_copied", 0),
            failed_names     = ev.get("failed_subitem_names") or [],
            db               = db,
        )

        # Confidence: convert from DB decimal (0-1 range stored) to 0-100 display
        raw_confidence = ev.get("confidence_score")
        if raw_confidence is not None:
            # DB stores as 0.87 style after the schema ALTER — convert to 87
            confidence_display = round(float(raw_confidence) * 100, 1) if float(raw_confidence) <= 1.0 else round(float(raw_confidence), 1)
        else:
            confidence_display = None

        items.append(ActivityLogItem(
            id                    = str(ev["id"]),
            item_id               = ev.get("item_id"),
            item_name             = ev.get("item_name") or "—",
            board_id              = ev.get("board_id"),
            board_name            = ev.get("board_name") or "—",
            template_matched      = ev.get("matched_template_name"),
            template_id           = ev.get("matched_template_id"),
            confidence            = confidence_display,
            status                = ev.get("status", "NO_MATCH"),
            time_ago              = _time_ago(ev.get("created_at")),
            duration              = _duration_str(ev.get("processing_ms")),
            subitems_copied_names = copied_names,
            subitems_copied_count = ev.get("subitems_copied", 0),
            subitems_failed_names = ev.get("failed_subitem_names") or [],
            subitems_failed_count = ev.get("subitems_failed", 0),
            error_details         = ev.get("error_details"),
            match_method          = ev.get("match_method"),
            ai_fallback_used      = ev.get("ai_fallback_used", False),
        ))

    # ── Tab counts (for badge numbers on tabs) ──
    # Run 3 quick count queries so frontend can show "SUCCESS (14)" etc.
    tab_counts = {"all": total, "success": 0, "no_match": 0, "failed": 0}
    try:
        for tab_status, tab_values in [
            ("success",  ["SUCCESS", "PARTIAL_SUCCESS"]),
            ("no_match", ["NO_MATCH"]),
            ("failed",   ["FAILED"]),
        ]:
            count_result = db.table("automation_events") \
                .select("id", count="exact") \
                .eq("workspace_id", workspace_uuid) \
                .in_("status", tab_values) \
                .execute()
            tab_counts[tab_status] = count_result.count or 0

        # ALL = total regardless of current filter
        all_result = db.table("automation_events") \
            .select("id", count="exact") \
            .eq("workspace_id", workspace_uuid) \
            .execute()
        tab_counts["all"] = all_result.count or 0

    except Exception:
        pass  # Non-critical — tabs still work without counts

    return ActivityLogResponse(
        workspace_id = workspaceId,
        items        = items,
        total        = total,
        page         = page,
        limit        = limit,
        total_pages  = total_pages,
        tab_counts   = tab_counts,
    )