# app/routes/auth.py

# ─────────────────────────────────────────────────────────────
# 2 URLs only:
#
# URL 1 → GET  /api/auth/authorization
#          monday.com calls this to start OAuth
#          → checks if already authorized
#          → if yes: redirect to backToUrl directly
#          → if no:  redirect to monday.com OAuth page
#          → after user approves, monday calls /api/auth/callback
#          → callback exchanges code → saves access_token to DB
#          → redirects to backToUrl
#
# URL 2 → POST /api/auth/verify
#          frontend calls this on every app load
#          → verifies session token
#          → checks if access_token exists in DB
#          → returns has_oauth: true/false
#          → if has_oauth=true + first load → also saves user + settings
# ─────────────────────────────────────────────────────────────

import jwt
import httpx
import urllib.parse
from fastapi           import APIRouter, HTTPException, Query, Depends
from fastapi.responses import RedirectResponse

from supabase import Client
from app.core.database import get_db
from app.core.config   import settings
from app.services.auth_service import (
    _verify_session_token, _verify_authorization_token, _init_user_and_settings
)

from app.schemas.auth  import (
    VerifyRequest, VerifyResponse
)

router = APIRouter(prefix="/api/auth", tags=["Auth"])




# ═══════════════════════════════════════════════════════════
# URL 1 — GET /api/auth/authorization
# monday.com calls this to start OAuth flow
# which token i want to pass in this url 
# ═══════════════════════════════════════════════════════════
@router.get("/authorization")
async def authorization(token: str = Query(...), db: Client = Depends(get_db)):
    """
    monday.com redirects user here when they install/open your app.

    Token is signed with SIGNING SECRET and contains:
    - userId
    - accountId
    - backToUrl  ← where to send user after auth

    Flow:
    1. Decode token with signing secret
    2. Check if workspace already has access_token in DB
    3. YES → redirect straight to backToUrl (skip OAuth)
    4. NO  → redirect to monday.com OAuth approval page
    """
    # ── Decode authorization token
    payload = _verify_authorization_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid authorization token")

    account_id  = payload.get("accountId")
    back_to_url = payload.get("backToUrl")

    if not back_to_url:
        raise HTTPException(status_code=400, detail="backToUrl missing from token")

    # ── Check if already authorized
    try:
        ws = db.table("workspaces") \
            .select("id, access_token") \
            .eq("monday_account_id", account_id) \
            .execute()
        workspace = ws.data[0] if ws.data else None
    except Exception:
        workspace = None

    already_authorized = (
        workspace is not None and
        workspace.get("access_token") not in [None, "", "test-token"]
    )

    # ── Already authorized → skip OAuth
    if already_authorized:
        return RedirectResponse(url=back_to_url)

    # ── Not authorized → redirect to monday.com OAuth
    # Pass full token as state → we recover backToUrl in callback
    params = urllib.parse.urlencode({
        "client_id":    settings.monday_client_id,
        "redirect_uri": f"{settings.app_base_url}api/auth/callback",
        "scope":        "boards:read boards:write webhooks:read webhooks:write workspaces:read",
        "state":        token,   # full token passed as state
    })

    return RedirectResponse(
        url=f"{settings.monday_authorize_url}?{params}"
    )


# ── Callback (monday.com redirects here after user approves)
@router.get("/callback")
async def oauth_callback(
    code:  str = Query(...),
    state: str = Query(...),
    db: Client = Depends(get_db)
):
    """
    monday.com sends code + state here after user approves OAuth.
    state = original authorization token (contains backToUrl).

    Flow:
    1. Decode state → get accountId + backToUrl
    2. Exchange code → access_token
    3. Save access_token to DB
    4. Redirect user to backToUrl
    """
    # ── Decode state to get backToUrl
    state_data = _verify_authorization_token(state)
    if not state_data:
        raise HTTPException(status_code=400, detail="Invalid state token")

    account_id  = state_data.get("accountId")
    back_to_url = state_data.get("backToUrl")

    # ── Exchange code for access_token
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                settings.monday_token_url,
                data={
                    "client_id":     settings.monday_client_id,
                    "client_secret": settings.monday_client_secret,
                    "code":          code,
                    "redirect_uri":  f"{settings.app_base_url}api/auth/callback",
                },
            )
        response.raise_for_status()
        access_token = response.json().get("access_token")

        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {str(e)}")

    # ── Save access_token to DB
    try:
        ws_result = db.table("workspaces").upsert(
            {
                "monday_account_id":   int(account_id),
                "monday_workspace_id": int(account_id),  # placeholder — updated in /verify
                "workspace_name":      f"Workspace {account_id}",
                "access_token":        access_token,
                "plan_tier":           "FREE",
                "status":              "ACTIVE",
                "is_active":           True,
                "is_paused":           False,
            },
            on_conflict="monday_account_id",
        ).execute()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save token: {str(e)}")

    # ── Redirect back to monday.com
    return RedirectResponse(url=back_to_url)



# ═══════════════════════════════════════════════════════════
# URL 2 — POST /api/auth/verify
# Frontend calls this on every app load
# ═══════════════════════════════════════════════════════════
@router.post("/verify", response_model=VerifyResponse)
async def verify_auth(payload: VerifyRequest, db: Client = Depends(get_db)):
    """
    Called on every app load by frontend.

    Flow:
    1. Decode session token with CLIENT SECRET
    2. Cross-check accountId + userId
    3. Check if workspace has real access_token in DB
    4. If first load (no workspace row) → create workspace row
    5. Save user + default settings
    6. Return has_oauth: true/false

    Frontend:
    - has_oauth = false → show "Connect Workspace" button
    - has_oauth = true  → load app normally
    """
    # ── Step 1: Decode session token (CLIENT SECRET)
    decoded = _verify_session_token(payload.sessionToken)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")

    # ── Step 2: Cross-check claims
    dat = decoded.get("dat", {})

    if str(dat.get("account_id", "")) != str(payload.accountId):
        raise HTTPException(status_code=401, detail="Token account mismatch")

    if str(dat.get("user_id", "")) != str(payload.userId):
        raise HTTPException(status_code=401, detail="Token user mismatch")

    is_admin = dat.get("is_admin", False)

    # ── Step 3: Check if workspace exists in DB
    try:
        ws_result = db.table("workspaces") \
            .select("id, access_token") \
            .eq("monday_workspace_id", payload.workspaceId) \
            .execute()
        workspace = ws_result.data[0] if ws_result.data else None
    except Exception:
        workspace = None

    # ── Step 4: If workspace doesn't exist → create placeholder row
    if workspace is None:
        try:
            ws_result = db.table("workspaces").upsert(
                {
                    "monday_account_id":   int(payload.accountId),
                    "monday_workspace_id": int(payload.workspaceId),
                    "workspace_name":      f"Workspace {payload.workspaceId}",
                    "access_token":        None,   # no OAuth yet
                    "plan_tier":           "FREE",
                    "status":              "ACTIVE",
                    "is_active":           True,
                    "is_paused":           False,
                },
                on_conflict="monday_workspace_id",
            ).execute()
            workspace = ws_result.data[0]
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create workspace: {str(e)}")

    workspace_uuid = workspace["id"]

    # ── Step 5: Always save user + default settings
    _init_user_and_settings(
        db             = db,
        workspace_uuid = workspace_uuid,
        user_id        = int(payload.userId),
        is_admin       = is_admin,
    )

    # ── Step 6: Check has_oauth
    has_oauth = workspace.get("access_token") not in [None, "", "test-token"]

    return VerifyResponse(
        success        = True,
        message        = "Token verified",
        has_oauth      = has_oauth,
        workspace_uuid = workspace_uuid,
        workspace_id   = payload.workspaceId,
        user_id        = int(payload.userId),
        is_admin       = is_admin,
    )





# # ─────────────────────────────────────────
# # GET /api/auth/authorization
# # monday.com redirects user here to start OAuth
# # Token signed with SIGNING SECRET
# # ─────────────────────────────────────────
# @router.get("/authorization")
# async def authorization(code: str = Query(...), db: Client = Depends(get_db)):
#     """
#     monday.com calls this when user starts OAuth flow.
#     Token is JWT signed with Signing Secret.
#     Contains: userId, accountId, backToUrl

#     Flow:
#     1. Decode token with signing secret
#     2. Check if workspace already has access_token in DB
#     3. If yes → redirect straight to backToUrl (no OAuth needed)
#     4. If no  → redirect to monday.com OAuth page
#     """

#     # ── Step 1: Decode authorization token
#     payload = verify_authorization_token(code)
#     if not payload:
#         raise HTTPException(status_code=401, detail="Invalid authorization token")

#     user_id     = payload.get("userId")
#     account_id  = payload.get("accountId")
#     back_to_url = payload.get("backToUrl")

#     if not back_to_url:
#         raise HTTPException(status_code=400, detail="backToUrl missing from token")

#     # ── Step 2: Check if workspace already has access token
#     try:
#         ws_result = db.table("workspaces") \
#             .select("id, access_token") \
#             .eq("monday_account_id", account_id) \
#             .execute()
#         workspace = ws_result.data[0] if ws_result.data else None
#     except Exception:
#         workspace = None

#     already_authorized = (
#         workspace is not None and
#         workspace.get("access_token") is not None and
#         workspace.get("access_token") != "test-token"
#     )

#     # ── Step 3: Already authorized → go back to monday.com directly
#     if already_authorized:
#         return RedirectResponse(url=back_to_url)

#     # ── Step 4: Not authorized → redirect to monday.com OAuth
#     # Pass original token as state so callback can recover backToUrl
#     # url : 
#     monday_oauth_url = (
#         f"{settings.monday_authorize_url}"
#         f"?client_id={settings.monday_client_id}"
#         f"&redirect_uri={settings.app_base_url}/api/auth/callback"
#         f"&scope={REQUIRED_SCOPES.replace(' ', '%20')}"
#         f"&state={code}"   # pass original token as state
#     )

#     return RedirectResponse(url=monday_oauth_url)


# # ─────────────────────────────────────────
# # GET /api/auth/callback
# # monday.com redirects here after user approves OAuth
# # ─────────────────────────────────────────
# @router.get("/callback")
# async def oauth_callback(
#     code:  str = Query(...),
#     state: str = Query(...),
#     db: Client = Depends(get_db)
# ):
#     """
#     monday.com sends code + state here after user approves.
#     state = original authorization token (contains backToUrl)
#     """

#     # ── Step 1: Recover backToUrl from state
#     state_data  = verify_authorization_token(state)
#     if not state_data:
#         raise HTTPException(status_code=400, detail="Invalid state token")

#     account_id  = state_data.get("accountId")
#     back_to_url = state_data.get("backToUrl")

#     # ── Step 2: Exchange code for access token
#     try:
#         async with httpx.AsyncClient(timeout=15) as client:
#             response = await client.post(
#                 settings.monday_token_url,
#                 data={
#                     "client_id":     settings.monday_client_id,
#                     "client_secret": settings.monday_client_secret,
#                     "code":          code,
#                     "redirect_uri":  f"{settings.app_base_url}/api/auth/callback",
#                 },
#             )
#         response.raise_for_status()
#         token_data   = response.json()
#         access_token = token_data.get("access_token")

#         if not access_token:
#             raise HTTPException(status_code=400, detail="No access token received")

#     except HTTPException:
#         raise
#     except Exception as e:
#         raise HTTPException(status_code=502, detail=f"Token exchange failed: {str(e)}")

#     # ── Step 3: Save access token to DB
#     try:
#         db.table("workspaces") \
#             .upsert(
#                 {
#                     "monday_account_id":   int(account_id),
#                     "monday_workspace_id": int(account_id),  # placeholder — updated in /init
#                     "workspace_name":      f"Workspace {account_id}",
#                     "access_token":        access_token,
#                     "plan_tier":           "FREE",
#                     "status":              "ACTIVE",
#                     "is_active":           True,
#                     "is_paused":           False,
#                 },
#                 on_conflict="monday_account_id",
#             ) \
#             .execute()

#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Failed to store token: {str(e)}")

#     # ── Step 4: Redirect back to monday.com
#     return {'url': back_to_url}


# # ─────────────────────────────────────────
# # POST /api/auth/verify
# # Called on every app load
# # ─────────────────────────────────────────
# @router.post("/verify", response_model=VerifyResponse)
# async def verify_auth(payload: VerifyRequest, db: Client = Depends(get_db)):
#     """
#     Every app load calls this.
#     Verifies session token + checks if OAuth done.
#     """

#     # ── Step 1: Verify session token (CLIENT SECRET)
#     decoded = await verify_monday_token(payload.sessionToken)
#     if not decoded:
#         raise HTTPException(status_code=401, detail="Invalid or expired session token")

#     # ── Step 2: Cross-check claims
#     dat = decoded.get("dat", {})

#     if str(dat.get("account_id", "")) != str(payload.accountId):
#         raise HTTPException(status_code=401, detail="Token account mismatch")

#     if str(dat.get("user_id", "")) != str(payload.userId):
#         raise HTTPException(status_code=401, detail="Token user mismatch")

#     # ── Step 3: Check if workspace has real access token
#     try:
#         ws_result = db.table("workspaces") \
#             .select("id, access_token") \
#             .eq("monday_workspace_id", payload.workspaceId) \
#             .execute()
#         workspace = ws_result.data[0] if ws_result.data else None
#     except Exception:
#         workspace = None

#     has_oauth = (
#         workspace is not None and
#         workspace.get("access_token") is not None and
#         workspace.get("access_token") != "test-token"
#     )

#     return VerifyResponse(
#         success        = True,
#         message        = "Token verified",
#         has_oauth      = has_oauth,
#         workspace_uuid = workspace["id"] if workspace else None,
#         is_admin       = dat.get("is_admin", False),
#     )


# # ─────────────────────────────────────────
# # POST /api/auth/init
# # Called once after OAuth completes
# # ─────────────────────────────────────────
# @router.post("/init", response_model=InitResponse)
# async def init_workspace(payload: InitRequest, db: Client = Depends(get_db)):
#     """
#     Called once after OAuth is confirmed complete.
#     Updates workspace with real workspace_id + saves user + default settings.
#     """

#     # ── Step 1: Verify session token
#     decoded = await verify_monday_token(payload.sessionToken)
#     if not decoded:
#         raise HTTPException(status_code=401, detail="Invalid or expired session token")

#     # ── Step 2: Update workspace with real workspace_id
#     try:
#         ws_result = db.table("workspaces") \
#             .upsert(
#                 {
#                     "monday_account_id":   int(payload.accountId),
#                     "monday_workspace_id": int(payload.workspaceId),
#                     "workspace_name":      payload.workspaceName or f"Workspace {payload.workspaceId}",
#                     "status":              "ACTIVE",
#                     "is_active":           True,
#                     "is_paused":           False,
#                 },
#                 on_conflict="monday_account_id",
#             ) \
#             .execute()

#         workspace      = ws_result.data[0]
#         workspace_uuid = workspace["id"]

#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Failed to update workspace: {str(e)}")

#     # ── Step 3: Upsert user
#     try:
#         db.table("users") \
#             .upsert(
#                 {
#                     "workspace_id":   workspace_uuid,
#                     "monday_user_id": int(payload.userId),
#                     "email":          payload.userEmail,
#                     "name":           payload.userName,
#                     "role":           "ADMIN",
#                     "is_admin":       True,
#                 },
#                 on_conflict="workspace_id,monday_user_id",
#             ) \
#             .execute()

#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Failed to save user: {str(e)}")

#     # ── Step 4: Create default settings only if not exists
#     try:
#         existing = db.table("workspace_settings") \
#             .select("id") \
#             .eq("workspace_id", workspace_uuid) \
#             .execute()

#         if not existing.data:
#             db.table("workspace_settings") \
#                 .insert({
#                     "workspace_id":                 workspace_uuid,
#                     "ai_sensitivity":               "BALANCED",
#                     "ai_enabled":                   True,
#                     "is_enabled":                   True,
#                     "exact_match_fallback_enabled": True,
#                     "onboarding_completed":         False,
#                 }) \
#                 .execute()

#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Failed to create settings: {str(e)}")

#     return InitResponse(
#         success        = True,
#         message        = "Workspace initialized successfully",
#         workspace_uuid = workspace_uuid,
#     )