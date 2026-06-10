# app/schemas/templates.py

from pydantic import BaseModel
from typing   import Optional, List
from datetime import datetime


# ─────────────────────────────────────────
# Subitem schemas (child items inside a template)
# ─────────────────────────────────────────
class SubitemCreate(BaseModel):
    """
    Used when creating a new subitem.
    Frontend sends this while creating a template.
    """
    name:       str
    sort_order: Optional[int] = None


class SubitemUpdate(BaseModel):
    """
    id present  → update existing subitem
    id absent   → create new subitem
    """
    id:         Optional[str] = None
    name:       str
    sort_order: Optional[int] = None


class SubitemResponse(BaseModel):
    """
    Used when returning subitem data to frontend.
    """
    id:         str
    name:       str
    sort_order: int


class TemplateCreateRequest(BaseModel):
    """
    Request body for creating a new template.
    """
    name:     str
    subitems: List[SubitemCreate] = []


class TemplateUpdateRequest(BaseModel):
    name:     Optional[str]              = None
    subitems: Optional[List[SubitemUpdate]] = None


class TemplateResponse(BaseModel):
    """
    Response returned after create / update / get template.
    """
    id:          str
    name:        str
    usage_count: int
    created_at:  datetime
    subitems:    List[SubitemResponse] = []


class TemplatesListResponse(BaseModel):
    """
    Response for GET all templates API.
    """
    workspace_id: str
    total:        int
    templates:    List[TemplateResponse]