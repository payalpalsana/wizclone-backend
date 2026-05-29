# app/schemas/templates.py

from pydantic import BaseModel
from typing import Optional, List
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


class SubitemResponse(BaseModel):
    """
    Used when returning subitem data to frontend.
    """
    id:         str
    name:       str
    sort_order: int


class SubitemUpdate(BaseModel):
    """
    Used when updating subitems in a template.
    Logic:
    - If 'id' exists → update existing subitem
    - If 'id' is None → create new subitem
    """
    id:         Optional[str] = None
    name:       str
    sort_order: Optional[int] = None


# ─────────────────────────────────────────
# Template schemas (main template object)
# ─────────────────────────────────────────
class TemplateCreateRequest(BaseModel):
    """
    Request body for creating a new template.
    """
    name:     str
    subitems: List[SubitemCreate] = []


class TemplateUpdateRequest(BaseModel):
    """
    Request body for updating a template.
    
    All fields are optional:
    - If not provided → no change
    """
    name:     Optional[str]          = None
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