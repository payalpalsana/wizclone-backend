# app/services/activity_log.py

from datetime import datetime, timezone
from supabase import Client
from app.core.database import get_db


# ─────────────────────────────────────────
# Helper: human-readable relative time
# ─────────────────────────────────────────
def _time_ago(created_at_str: str) -> str:
    """
    Converts ISO timestamp → human-readable string.
    Examples: "just now", "2 hours ago", "3 days ago"
    """
    if not created_at_str:
        return "—"

    try:
        # Parse ISO string — handle both with and without timezone
        if created_at_str.endswith("Z"):
            created_at_str = created_at_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(created_at_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        now        = datetime.now(timezone.utc)
        diff       = now - dt
        seconds    = int(diff.total_seconds())

        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            m = seconds // 60
            return f"{m} minute{'s' if m > 1 else ''} ago"
        elif seconds < 86400:
            h = seconds // 3600
            return f"{h} hour{'s' if h > 1 else ''} ago"
        elif seconds < 604800:
            d = seconds // 86400
            return f"{d} day{'s' if d > 1 else ''} ago"
        else:
            w = seconds // 604800
            return f"{w} week{'s' if w > 1 else ''} ago"

    except Exception:
        return "—"


# ─────────────────────────────────────────
# Helper: duration string
# ─────────────────────────────────────────
def _duration_str(processing_ms: int | None) -> str | None:
    """
    Converts milliseconds → human-readable duration.
    Example: 1800 → "Completed in 1.8s"
    """
    if processing_ms is None:
        return None
    seconds = processing_ms / 1000
    return f"Completed in {seconds:.1f}s"


# ─────────────────────────────────────────
# Helper: fetch subitems copied names
# ─────────────────────────────────────────
def _get_copied_subitem_names(
    template_id:    str | None,
    subitems_copied: int,
    failed_names:   list,
    db:             Client,
) -> list[str]:
    """
    Returns list of subitem names that were successfully copied.

    Strategy:
    - Fetch all subitems for the matched template
    - Remove the ones in failed_names
    - Return the rest (up to subitems_copied count)

    This gives us the checkmark list shown in the expanded row.
    """
    if not template_id or subitems_copied == 0:
        return []

    try:
        result = db.table("template_subitems") \
            .select("name, sort_order") \
            .eq("template_id", template_id) \
            .is_("deleted_at", "null") \
            .order("sort_order") \
            .execute()

        all_names    = [row["name"] for row in (result.data or [])]
        failed_set   = set(failed_names or [])

        # Copied = all subitems minus the ones that failed
        copied_names = [n for n in all_names if n not in failed_set]

        return copied_names[:subitems_copied]

    except Exception:
        return []