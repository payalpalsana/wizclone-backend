# app/routes/templates.py
# ─────────────────────────────────────────────────────────────
# Template CRUD APIs
#
# POST   /api/templates/{workspaceId}              → create
# GET    /api/templates/{workspaceId}              → list all
# PUT    /api/templates/{workspaceId}/{templateId} → update name + subitems
# DELETE /api/templates/{workspaceId}/{templateId} → soft delete
#
# Subitem ORDER IS IMPORTANT — subitems are copied to monday.com
# in sort_order sequence. Always preserve it.
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Depends

from datetime import datetime, timezone

from fastapi    import APIRouter, HTTPException, Depends
from supabase   import Client

from app.core.database    import get_db
from app.core.helpers     import get_workspace_uuid, get_subitems_for_template
from app.schemas.templates import (
    TemplateCreateRequest, TemplateUpdateRequest,
    TemplateResponse,      TemplatesListResponse,
    SubitemResponse,
)

router = APIRouter(prefix="/api", tags=["Templates"])


# ─────────────────────────────────────────
# POST /api/templates/{workspaceId}
# ─────────────────────────────────────────
@router.post("/templates/{workspaceId}", response_model=TemplateResponse)
async def create_template(
    workspaceId: str,
    body:        TemplateCreateRequest,
    db:          Client = Depends(get_db),
):
    """
    Create a new template with subitems.

    sort_order rules:
    → If frontend sends sort_order → use it
    → If not sent → auto-assign 0, 1, 2 ... based on list position
    """
    workspace_uuid = get_workspace_uuid(workspaceId, db)

    # ── Insert template row ──
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

    template    = t_result.data[0]
    template_id = template["id"]

    # ── Insert subitems (order matters) ──
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

    subitems = get_subitems_for_template(template_id, db)

    return TemplateResponse(
        id          = template_id,
        name        = template["name"],
        usage_count = 0,
        created_at  = template["created_at"],
        subitems    = [SubitemResponse(**s) for s in subitems],
    )


# ─────────────────────────────────────────
# GET /api/templates/{workspaceId}
# ─────────────────────────────────────────
@router.get("/templates/{workspaceId}", response_model=TemplatesListResponse)
async def list_templates(workspaceId: str, db: Client = Depends(get_db)):
    """
    Return all active templates with their subitems (ordered by sort_order).
    Uses a single subitems query for all templates to avoid N+1 queries.
    """
    workspace_uuid = get_workspace_uuid(workspaceId, db)

    # ── Fetch all active templates ──
    try:
        t_result = db.table("templates") \
            .select("id, name, usage_count, created_at") \
            .eq("workspace_id", workspace_uuid) \
            .eq("is_deleted",   False) \
            .eq("is_active",    True) \
            .order("created_at", desc=False) \
            .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch templates: {str(e)}")

    templates = t_result.data or []

    if not templates:
        return TemplatesListResponse(
            workspace_id=workspaceId, 
            total=0, 
            templates=[]
        )

    # ── Fetch all subitems for all templates in ONE query (avoids N+1) ──
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

    # Group subitems by template_id
    subitems_map: dict[str, list] = {t["id"]: [] for t in templates}
    for sub in (s_result.data or []):
        if sub["template_id"] in subitems_map:
            subitems_map[sub["template_id"]].append(sub)

    return TemplatesListResponse(
        workspace_id = workspaceId,
        total        = len(templates),
        templates    = [
            TemplateResponse(
                id          = t["id"],
                name        = t["name"],
                usage_count = t["usage_count"],
                created_at  = t["created_at"],
                subitems    = [SubitemResponse(**s) for s in subitems_map[t["id"]]],
            )
            for t in templates
        ],
    )


# ─────────────────────────────────────────
# PUT /api/templates/{workspaceId}/{templateId}
# ─────────────────────────────────────────
@router.put("/templates/{workspaceId}/{templateId}", response_model=TemplateResponse)
async def update_template(
    workspaceId: str,
    templateId:  str,
    body:        TemplateUpdateRequest,
    db:          Client = Depends(get_db),
):
    """
    Update template name and/or subitems.

    Subitem update strategy (preserves order):
    → Subitem WITH id    → UPDATE (name + sort_order)
    → Subitem WITHOUT id → INSERT as new
    → Subitem in DB but NOT in request → soft-delete (set deleted_at)
    """
    workspace_uuid = get_workspace_uuid(workspaceId, db)

    # ── Verify template exists and belongs to this workspace ──
    try:
        t_result = db.table("templates") \
            .select("id, name, usage_count, created_at") \
            .eq("id",           templateId) \
            .eq("workspace_id", workspace_uuid) \
            .eq("is_deleted",   False) \
            .single() \
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Template not found")

    if not t_result.data:
        raise HTTPException(status_code=404, detail="Template not found")

    template = t_result.data

    # ── Update template name if provided ──
    if body.name is not None:
        try:
            db.table("templates") \
                .update({"name": body.name}) \
                .eq("id", templateId) \
                .execute()
            template["name"] = body.name
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update template name: {str(e)}")

    # ── Update subitems if provided ──
    if body.subitems is not None:

        # IDs the frontend is keeping (existing subitems that were not removed)
        sent_ids = {sub.id for sub in body.subitems if sub.id is not None}

        # IDs currently in DB
        try:
            current = db.table("template_subitems") \
                .select("id") \
                .eq("template_id", templateId) \
                .is_("deleted_at", "null") \
                .execute()
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch current subitems: {str(e)}",
            )

        current_ids   = {row["id"] for row in (current.data or [])}

        # IDs in DB but NOT in request → soft delete
        ids_to_delete = current_ids - sent_ids   # in DB but not in request → soft delete

        if ids_to_delete:
            try:
                db.table("template_subitems") \
                    .update({"deleted_at": datetime.now(timezone.utc).isoformat()}) \
                    .in_("id", list(ids_to_delete)) \
                    .execute()
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to delete removed subitems: {str(e)}",
                )

        # Process each subitem in the request (update existing, insert new)
        for idx, sub in enumerate(body.subitems):
            sort_order = sub.sort_order if sub.sort_order is not None else idx

            if sub.id:
                # Update existing
                try:
                    db.table("template_subitems") \
                        .update({"name": sub.name, "sort_order": sort_order}) \
                        .eq("id", sub.id) \
                        .execute()
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to update subitem {sub.id}: {str(e)}",
                    )
            else:
                # Insert new
                try:
                    db.table("template_subitems") \
                        .insert({
                            "template_id": templateId,
                            "name":        sub.name,
                            "sort_order":  sort_order,
                        }) \
                        .execute()
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to insert new subitem: {str(e)}",
                    )

    subitems = get_subitems_for_template(templateId, db)

    return TemplateResponse(
        id          = template["id"],
        name        = template["name"],
        usage_count = template["usage_count"],
        created_at  = template["created_at"],
        subitems    = [SubitemResponse(**s) for s in subitems],
    )


# ─────────────────────────────────────────
# DELETE /api/templates/{workspaceId}/{templateId}
# ─────────────────────────────────────────
@router.delete("/templates/{workspaceId}/{templateId}")
async def delete_template(
    workspaceId: str,
    templateId:  str,
    db:          Client = Depends(get_db),
):
    """
    Soft-delete a template and all its subitems.
    Data is kept in DB for audit / history.
    is_deleted=true, deleted_at=now on both tables.
    """
    workspace_uuid = get_workspace_uuid(workspaceId, db)
    now            = datetime.now(timezone.utc).isoformat()

    # ── Verify template exists and belongs to this workspace ──
    try:
        existing = db.table("templates") \
            .select("id") \
            .eq("id",           templateId) \
            .eq("workspace_id", workspace_uuid) \
            .eq("is_deleted",   False) \
            .single() \
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Template not found")

    if not existing.data:
        raise HTTPException(status_code=404, detail="Template not found")

    # ── Soft-delete template ──
    try:
        db.table("templates") \
            .update({"is_deleted": True, "deleted_at": now, "is_active": False}) \
            .eq("id", templateId) \
            .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete template: {str(e)}")

    # ── Soft-delete all subitems ──
    try:
        db.table("template_subitems") \
            .update({"deleted_at": now}) \
            .eq("template_id", templateId) \
            .is_("deleted_at", "null") \
            .execute()
    except Exception:
        pass   # Non-critical — template is already deleted

    return {"success": True, "message": "Template deleted successfully"}