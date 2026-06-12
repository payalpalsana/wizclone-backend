# app/schemas/templates.py

from pydantic import BaseModel
from typing   import Optional, List
from datetime import datetime


# ─────────────────────────────────────────
# Subitem schemas
# ─────────────────────────────────────────
class SubitemCreate(BaseModel):
    """Used when creating a new subitem inside a template."""
    name:       str
    sort_order: Optional[int] = None


class SubitemUpdate(BaseModel):
    """
    id present  → update existing subitem
    id absent   → insert as new subitem
    """
    id:         Optional[str] = None
    name:       str
    sort_order: Optional[int] = None


class SubitemResponse(BaseModel):
    """Returned to frontend when reading subitems."""
    id:         str
    name:       str
    sort_order: int


# ─────────────────────────────────────────
# Template schemas
# ─────────────────────────────────────────
class TemplateCreateRequest(BaseModel):
    """Request body for POST /api/templates/{workspaceId}"""
    name:     str
    subitems: List[SubitemCreate] = []


class TemplateUpdateRequest(BaseModel):
    """Request body for PUT /api/templates/{workspaceId}/{templateId}"""
    name:     Optional[str]               = None
    subitems: Optional[List[SubitemUpdate]] = None


class TemplateResponse(BaseModel):
    """
    Single template returned in all responses.
    Includes subitems ordered by sort_order.
    """
    id:          str
    name:        str
    usage_count: int
    created_at:  datetime
    subitems:    List[SubitemResponse] = []


class TemplatesListResponse(BaseModel):
    """
    Response for GET /api/templates/{workspaceId}

    Includes pagination metadata so frontend can build page controls.

    Fields:
        workspace_id → monday workspace ID
        total        → total templates matching the search (not just this page)
        page         → current page number (1-based)
        limit        → items per page
        total_pages  → ceil(total / limit)
        templates    → list of templates for this page
    """
    workspace_id: str
    total:        int
    page:         int
    limit:        int
    total_pages:  int
    templates:    List[TemplateResponse]
class TestAIRequest(BaseModel):
    item_name: str
