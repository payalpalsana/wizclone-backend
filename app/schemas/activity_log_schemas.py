# app/schemas/activity_log.py

from pydantic import BaseModel
from typing   import Optional, List


class ActivityLogItem(BaseModel):
    """
    Single row in the activity log table.

    Collapsed view shows:
        item_name | template_matched | confidence | status | time_ago

    Expanded view shows:
        item_id, item_name       → ITEM section
        board_name               → BOARD section
        template_matched         → TEMPLATE section
        duration                 → DURATION section
        subitems_copied_names    → SUBITEMS COPIED (checkmark list)
        subitems_failed_names    → shown in red if any
        error_details            → ERROR section (red text)
    """
    id:                    str
    item_id:               Optional[int]        = None
    item_name:             str
    board_id:              Optional[int]        = None
    board_name:            str                  = "—"

    # Template match info
    template_matched:      Optional[str]        = None   # None shown as "—" on frontend
    template_id:           Optional[str]        = None

    # Confidence: 0-100 float, None if no match attempted
    # Frontend colors: ≥80 green, 60-79 orange, <60 red, None = "—"
    confidence:            Optional[float]      = None

    # Status: SUCCESS | PARTIAL_SUCCESS | NO_MATCH | FAILED
    status:                str

    # Human-readable time: "just now", "2 hours ago" etc.
    time_ago:              str

    # Human-readable duration: "Completed in 1.8s" or None
    duration:              Optional[str]        = None

    # Subitems detail (for expanded row)
    subitems_copied_names: List[str]            = []
    subitems_copied_count: int                  = 0
    subitems_failed_names: List[str]            = []
    subitems_failed_count: int                  = 0

    # Error text shown in red ("Template board not found")
    error_details:         Optional[str]        = None

    # Match method: "AI" | "EXACT_MATCH" | "FALLBACK"
    # Frontend shows "Exact match" badge when this is EXACT_MATCH
    match_method:          Optional[str]        = None
    ai_fallback_used:      bool                 = False


class ActivityLogResponse(BaseModel):
    """
    Paginated activity log response.

    tab_counts:
        Used to show badge numbers on each tab:
        { "all": 50, "success": 30, "no_match": 12, "failed": 8 }
    """
    workspace_id: str
    items:        List[ActivityLogItem]
    total:        int
    page:         int
    limit:        int
    total_pages:  int
    tab_counts:   dict                    = {}