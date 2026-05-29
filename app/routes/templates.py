# app/routes/templates.py
# ─────────────────────────────────────────────────────────────
# Templates API
#
# POST   /templates/{workspaceId}             → create template
# GET    /templates/{workspaceId}             → list all templates
# PUT    /templates/{workspaceId}/{templateId}→ edit template + subitems
# DELETE /templates/{workspaceId}/{templateId}→ delete template
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Depends

from datetime import datetime, timezone
from app.core.database import get_supabase
from app.schemas.templates import (
    TemplateCreateRequest,
    TemplateUpdateRequest,
    TemplateResponse,
    TemplatesListResponse,
    SubitemResponse,
)

router = APIRouter(tags=["Templates"])


# ─────────────────────────────────────────
# Helper: resolve monday_workspace_id → internal UUID
# ─────────────────────────────────────────
def _get_workspace_uuid(monday_workspace_id: str, db) -> str:
    """
    Converts monday workspace ID (external) into internal UUID (used in DB).

    Why?
    → monday.com uses numeric workspace ID
    → Our DB uses UUID for better consistency and relations
    """
    try:
        result = db.table("workspaces") \
            .select("id") \
            .eq("monday_workspace_id", monday_workspace_id) \
            .single() \
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if not result.data:
        raise HTTPException(status_code=404, detail="Workspace not found")
    
    return result.data["id"]



# ─────────────────────────────────────────
# Helper: Fetch subitems for a template
# ─────────────────────────────────────────
def _get_subitems(template_id: str, db) -> list:
    """
    Fetch all subitems of a template in correct order.

    Filters:
    → Only active (not soft-deleted)
    → Ordered by sort_order (important for UI display)
    """
    try:
        result = db.table("template_subitems") \
            .select("id, name, sort_order") \
            .eq("template_id", template_id) \
            .is_("deleted_at", "null") \
            .order("sort_order", desc=False) \
            .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch subitems: {str(e)}")
    
    return result.data or []


# ─────────────────────────────────────────
# POST /templates/{workspaceId}
# Create new template with subitems
# ─────────────────────────────────────────
@router.post("/templates/{workspaceId}", response_model=TemplateResponse)
async def create_template(workspaceId: str, body: TemplateCreateRequest, db = Depends(get_supabase)):
    """
    Creates a new template and its subitems.

    Subitem sort_order:
    → If frontend sends sort_order → use it
    → If not sent → auto assign 0,1,2,3... based on list position
    """

    workspace_uuid = _get_workspace_uuid(workspaceId, db)

    # ── 1. Insert template row
    try:
        t_result = db.table("templates") \
            .insert({
                "workspace_id": workspace_uuid,
                "name":         body.name,
                "source":       "MANUAL",
                "is_active":    True,
                "is_deleted":   False,
            }) \
            .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create template: {str(e)}")

    template = t_result.data[0]
    template_id = template["id"]

    # ── 2. Insert subitems (order matters)
    if body.subitems:
        subitems_to_insert = [
            {
                "template_id": template_id,
                "name":        sub.name,
                "sort_order":  sub.sort_order if sub.sort_order is not None else idx,
            }
            for idx, sub in enumerate(body.subitems)
        ]
        try:
            db.table("template_subitems").insert(subitems_to_insert).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create subitems: {str(e)}")


    # ── 3. Fetch inserted subitems and return
    subitems = _get_subitems(template_id, db)

    return TemplateResponse(
        id          = template_id,
        name        = template["name"],
        usage_count = 0,
        created_at  = template["created_at"],
        subitems    = [SubitemResponse(**s) for s in subitems],
    )


# ─────────────────────────────────────────
# GET /templates/{workspaceId}
# List all templates with their subitems
# ─────────────────────────────────────────
@router.get("/templates/{workspaceId}", response_model=TemplatesListResponse)
async def list_templates(workspaceId: str, db = Depends(get_supabase)):
    """
    Returns all active templates for the workspace.
    Each template includes its subitems ordered by sort_order.
    """

    workspace_uuid = _get_workspace_uuid(workspaceId, db)

    # ── 1. Fetch all active templates
    try:
        t_result = db.table("templates") \
            .select("id, name, usage_count, created_at") \
            .eq("workspace_id", workspace_uuid) \
            .eq("is_deleted", False) \
            .eq("is_active", True) \
            .order("created_at", desc=False) \
            .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch templates: {str(e)}")

    templates = t_result.data or []

    if not templates:
        return TemplatesListResponse(
            workspace_id = workspaceId,
            total        = 0,
            templates    = [],
        )

    # ── 2. Fetch all subitems for all templates in one query
    template_ids = [t["id"] for t in templates]

    try:
        s_result = db.table("template_subitems") \
            .select("id, template_id, name, sort_order") \
            .in_("template_id", template_ids) \
            .is_("deleted_at", "null") \
            .order("sort_order", desc=False) \
            .execute()
    except Exception:
        s_result = None

    # ── 3. Group subitems by template_id
    subitems_map: dict[str, list] = {t["id"]: [] for t in templates}
    for sub in (s_result.data or []):
        tid = sub["template_id"]
        if tid in subitems_map:
            subitems_map[tid].append(sub)

    # ── 4. Build response
    template_responses = [
        TemplateResponse(
            id          = t["id"],
            name        = t["name"],
            usage_count = t["usage_count"],
            created_at  = t["created_at"],
            subitems    = [SubitemResponse(**s) for s in subitems_map[t["id"]]],
        )
        for t in templates
    ]

    return TemplatesListResponse(
        workspace_id = workspaceId,
        total        = len(template_responses),
        templates    = template_responses,
    )


# ─────────────────────────────────────────
# PUT /templates/{workspaceId}/{templateId}
# Edit template name and/or subitems
# ─────────────────────────────────────────
@router.put("/templates/{workspaceId}/{templateId}", response_model=TemplateResponse)
async def update_template(
    workspaceId: str,
    templateId:  str,
    body:        TemplateUpdateRequest,
    db = Depends(get_supabase)
):
    """
    Updates template name and subitems.

    Subitem update strategy:
    → Subitems with id    → UPDATE name + sort_order
    → Subitems without id → INSERT as new
    → Subitems in DB but not in request → soft DELETE (deleted_at)

    This way order is always preserved correctly.
    """

    workspace_uuid = _get_workspace_uuid(workspaceId, db)

    # ── 1. Verify template belongs to this workspace
    try:
        t_result = db.table("templates") \
            .select("id, name, usage_count, created_at") \
            .eq("id", templateId) \
            .eq("workspace_id", workspace_uuid) \
            .eq("is_deleted", False) \
            .single() \
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Template not found")

    if not t_result.data:
        raise HTTPException(status_code=404, detail="Template not found")

    template = t_result.data

    # ── 2. Update template name if provided
    if body.name is not None:
        try:
            db.table("templates") \
                .update({"name": body.name}) \
                .eq("id", templateId) \
                .execute()
            template["name"] = body.name
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update template: {str(e)}")

    # ── 3. Update subitems if provided
    if body.subitems is not None:

        # IDs sent from frontend (existing subitems being kept)
        sent_ids = {sub.id for sub in body.subitems if sub.id is not None}

        # Fetch current subitems from DB
        try:
            current = db.table("template_subitems") \
                .select("id") \
                .eq("template_id", templateId) \
                .is_("deleted_at", "null") \
                .execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch current subitems: {str(e)}")
        
        current_ids = {row["id"] for row in (current.data or [])}

        # IDs in DB but NOT in request → soft delete
        ids_to_delete = current_ids - sent_ids
        if ids_to_delete:
            try:
                db.table("template_subitems") \
                    .update({"deleted_at": datetime.now(timezone.utc).isoformat()}) \
                    .in_("id", list(ids_to_delete)) \
                    .execute()
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to delete removed subitems: {str(e)}")

        # Process each subitem in request
        for idx, sub in enumerate(body.subitems):
            sort_order = sub.sort_order if sub.sort_order is not None else idx

            if sub.id:
                # UPDATE existing
                try:
                    db.table("template_subitems") \
                        .update({"name": sub.name, "sort_order": sort_order}) \
                        .eq("id", sub.id) \
                        .execute()
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"Failed to update subitem {sub.id}: {str(e)}")
            else:
                # INSERT new
                try:
                    db.table("template_subitems") \
                        .insert({
                            "template_id": templateId,
                            "name":        sub.name,
                            "sort_order":  sort_order,
                        }) \
                        .execute()
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"Failed to insert new subitem: {str(e)}")

    # ── 4. Return updated template with subitems
    # Pass db explicitly — _get_subitems is a plain function, not a FastAPI dependency
    subitems = _get_subitems(templateId, db)

    return TemplateResponse(
        id          = template["id"],
        name        = template["name"],
        usage_count = template["usage_count"],
        created_at  = template["created_at"],
        subitems    = [SubitemResponse(**s) for s in subitems],
    )


# ─────────────────────────────────────────
# DELETE /templates/{workspaceId}/{templateId}
# Soft delete template
# ─────────────────────────────────────────
@router.delete("/templates/{workspaceId}/{templateId}")
async def delete_template(workspaceId: str, templateId: str, db = Depends(get_supabase)):
    """
    Soft deletes a template (is_deleted=true, deleted_at=now).
    Subitems are also soft deleted.
    Data is kept in DB for audit/history.
    """

    workspace_uuid = _get_workspace_uuid(workspaceId, db)

    now = datetime.now(timezone.utc).isoformat()

    # ── 1. Verify template exists and belongs to workspace
    try:
        existing = db.table("templates") \
            .select("id") \
            .eq("id", templateId) \
            .eq("workspace_id", workspace_uuid) \
            .eq("is_deleted", False) \
            .single() \
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Template not found")
    
    if not existing.data:
        raise HTTPException(status_code=404, detail="Template not found")

    # ── 2. Soft delete template
    try:
        db.table("templates") \
            .update({"is_deleted": True, "deleted_at": now, "is_active": False}) \
            .eq("id", templateId) \
            .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete template: {str(e)}")

    # ── 3. Soft delete all its subitems
    try:
        db.table("template_subitems") \
            .update({"deleted_at": now}) \
            .eq("template_id", templateId) \
            .is_("deleted_at", "null") \
            .execute()
    except Exception:
        pass  # Non-critical

    return {"success": True, "message": "Template deleted successfully"}