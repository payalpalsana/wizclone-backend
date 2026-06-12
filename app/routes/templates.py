# app/routes/templates.py
# ─────────────────────────────────────────────────────────────
# Template CRUD APIs
#
# POST   /api/templates/{workspaceId}                          → create
# GET    /api/templates/{workspaceId}?page=1&limit=20&search=  → list (paginated + search)
# PUT    /api/templates/{workspaceId}/{templateId}             → update name + subitems
# DELETE /api/templates/{workspaceId}/{templateId}             → soft delete
#
# Subitem ORDER IS IMPORTANT — subitems are copied to monday.com
# in sort_order sequence. Always preserve it.
# ─────────────────────────────────────────────────────────────

from datetime import datetime, timezone
from typing   import Optional

from fastapi  import APIRouter, HTTPException, Depends, Request, Query
from supabase import Client

from app.core.database  import get_db
from app.services.settings import get_workspace_uuid_for_request, get_subitems_for_template
from app.schemas.templates import (
    TemplateCreateRequest, TemplateUpdateRequest,
    TemplateResponse,      TemplatesListResponse,
    SubitemResponse,       AIRequest,
)
from app.services.matching_services import _ai_semantic_match

router = APIRouter(prefix="/api", tags=["Templates"])


# ─────────────────────────────────────────
# POST /api/templates/{workspaceId}
# ─────────────────────────────────────────
@router.post("/templates/{workspaceId}", response_model=TemplateResponse)
async def create_template(
    request:     Request,
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
    workspace_uuid = get_workspace_uuid_for_request(request, workspaceId, db)

    # ── Check if template already exists ──
    existing = db.table("templates") \
        .select("*") \
        .eq("workspace_id", workspace_uuid) \
        .eq("name", body.name) \
        .is_("deleted_at", "null") \
        .execute()

    if existing.data:
        template    = existing.data[0]
        template_id = template["id"]
        is_new      = False
    else:
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
            template    = t_result.data[0]
            template_id = template["id"]
            is_new      = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create template: {str(e)}")

    # ── Insert subitems (order matters) ──
    if body.subitems:
        start_idx = 0
        if not is_new:
            # Find the highest existing sort_order
            existing_subs = get_subitems_for_template(template_id, db)
            if existing_subs:
                start_idx = max((s["sort_order"] for s in existing_subs if s.get("sort_order") is not None), default=-1) + 1

        subitems_to_insert = []
        for idx, sub in enumerate(body.subitems):
            sort_order = sub.sort_order if sub.sort_order is not None else (start_idx + idx)
            subitems_to_insert.append({
                "template_id": template_id,
                "name":        sub.name,
                "sort_order":  sort_order,
            })
            
        try:
            db.table("template_subitems").insert(subitems_to_insert).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create subitems: {str(e)}")
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
async def list_templates(
    request:     Request,
    workspaceId: str,
    # ── Pagination ──
    page:    int           = Query(default=1,  ge=1,          description="Page number (1-based)"),
    limit:   int           = Query(default=20, ge=1,  le=100, description="Items per page (max 100)"),
    # ── Search ──
    search:  Optional[str] = Query(default=None,              description="Search by template name"),
    db:      Client        = Depends(get_db),
):
    """
    Return paginated templates with their subitems.

    Query params:
        page   → page number, starts at 1 (default: 1)
        limit  → items per page, max 100 (default: 20)
        search → filter templates by name (case-insensitive partial match)

    Examples:
        GET /api/templates/13120926
        GET /api/templates/13120926?page=2&limit=10
        GET /api/templates/13120926?search=onboarding
        GET /api/templates/13120926?search=client&page=1&limit=5
    """
    workspace_uuid = get_workspace_uuid_for_request(request, workspaceId, db)
    offset         = (page - 1) * limit

    try:
        # ── 1. Get total count ──
        count_query = db.table("templates") \
            .select("id", count="exact") \
            .eq("workspace_id", workspace_uuid) \
            .eq("is_deleted",   False) \
            .eq("is_active",    True)

        if search and search.strip():
            count_query = count_query.ilike("name", f"%{search.strip()}%")
            
        count_result = count_query.execute()
        total = count_result.count or 0
        total_pages = max(1, -(-total // limit))

        # ── 2. Fetch paginated data safely ──
        if offset >= total and total > 0:
            templates = []
        else:
            data_query = db.table("templates") \
                .select("id, name, usage_count, created_at") \
                .eq("workspace_id", workspace_uuid) \
                .eq("is_deleted",   False) \
                .eq("is_active",    True) \
                .order("created_at", desc=False) \
                .range(offset, offset + limit - 1)

            if search and search.strip():
                data_query = data_query.ilike("name", f"%{search.strip()}%")

            result = data_query.execute()
            templates = result.data or []

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch templates: {str(e)}")

    # ── Return empty response if no templates found ──
    if not templates:
        return TemplatesListResponse(
            workspace_id = workspaceId,
            total        = total,
            page         = page,
            limit        = limit,
            total_pages  = total_pages,
            templates    = [],
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

    # ── Group subitems by template_id ──
    subitems_map: dict[str, list] = {t["id"]: [] for t in templates}
    for sub in (s_result.data or []):
        if sub["template_id"] in subitems_map:
            subitems_map[sub["template_id"]].append(sub)

    return TemplatesListResponse(
        workspace_id = workspaceId,
        total        = total,
        page         = page,
        limit        = limit,
        total_pages  = total_pages,
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
    request:     Request,
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
    workspace_uuid = get_workspace_uuid_for_request(request, workspaceId, db)

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

        # IDs the frontend is keeping
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
        ids_to_delete = current_ids - sent_ids

        # Soft delete removed subitems
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

        # Update existing or insert new subitems
        for idx, sub in enumerate(body.subitems):
            sort_order = sub.sort_order if sub.sort_order is not None else idx

            if sub.id:
                # Update existing subitem
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
                # Insert new subitem
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
    request:     Request,
    workspaceId: str,
    templateId:  str,
    db:          Client = Depends(get_db),
):
    """
    Soft-delete a template and all its subitems.
    Data is kept in DB for audit/history.
    Sets is_deleted=true, deleted_at=now on both tables.
    """
    workspace_uuid = get_workspace_uuid_for_request(request, workspaceId, db)
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
# ─────────────────────────────────────────
# POST /api/ai-match/{workspaceId}
# ─────────────────────────────────────────
@router.post("/ai-match/{workspaceId}")
async def ai_match(
    request:     Request,
    workspaceId: str,
    body:        AIRequest,
    db:          Client = Depends(get_db),
):
    """
    The Groq AI matching directly!
    Pass any item_name to see which template the AI picks.
    """
    workspace_uuid = get_workspace_uuid_for_request(request, workspaceId, db)

    # Run AI generation
    from app.services.matching_services import generate_template_from_ai
    result = await generate_template_from_ai(body.item_name)
    
    if result:
        return {
            "item": body.item_name,
            "ai_result": result,
        }
    else:
        return {
            "item": body.item_name,
            "error": "AI failed to generate a template.",
        }
