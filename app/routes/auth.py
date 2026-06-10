# app/routes/auth.py

# ─────────────────────────────────────────────────────────────
# First time install:
# ─────────────────
# monday.com → GET /api/auth/authorization?token=xxx
#                     ↓
#             decode token → get accountId, backToUrl
#                     ↓
#             check DB → no access_token yet
#                     ↓
#             redirect to monday.com OAuth page
#                     ↓
#             user approves
#                     ↓

# monday.com → GET /api/auth/callback?code=xxx&state=xxx
#                     ↓
#             exchange code → get access_token
#                     ↓
#             save access_token to DB
#                     ↓
#             redirect to backToUrl
#                     ↓

# Every app load (after install):
# ───────────────────────────────
# Frontend JS → POST /api/auth/verify
#               (sessionToken in header)
#                     ↓
#               verify token
#                     ↓
#               check DB → has access_token?
#                     ↓
#               return has_oauth: true/false
# ─────────────────────────────────────────────────────────────

import jwt
import httpx
import urllib.parse
from fastapi           import APIRouter, HTTPException, Query, Depends, Request
from fastapi.responses import RedirectResponse, HTMLResponse

from supabase import Client
from app.core.database import get_db
from app.core.config   import settings
from app.services.auth import (
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
        "redirect_uri": f"{settings.app_base_url}/api/auth/callback",
        "scope":        "boards:read boards:write webhooks:read webhooks:write workspaces:read",
        "state":        token,   # full token passed as state
    })

    return RedirectResponse(
        url=f"{settings.monday_authorize_url}?{params}"
    )




@router.post("/oauth2/authorized", include_in_schema=False)
async def monday_oauth_authorized(
    request: Request,
    db: Client = Depends(get_db)
):
    """
    Monday calls this server-to-server after user clicks Authorize.
    This is NOT the redirect callback — Monday POSTs the code directly here.
    """
    print("=" * 60)
    print("MONDAY /oauth2/authorized HIT")

    body = await request.json()
    print("BODY:", body)

    code = body.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="No code in body")

    # Exchange code → access_token
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            settings.monday_token_url,
            data={
                "client_id":     settings.monday_client_id,
                "client_secret": settings.monday_client_secret,
                "code":          code,
                "redirect_uri":  f"{settings.app_base_url}/api/auth/callback",
            },
        )
    response.raise_for_status()
    access_token = response.json().get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="No access token received")

    # Get account_id
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(
            "https://api.monday.com/v2",
            json={"query": "query { me { account { id } } }"},
            headers={"Authorization": access_token, "Content-Type": "application/json"},
        )
    account_id = res.json()["data"]["me"]["account"]["id"]

    # Save to DB
    db.table("workspaces").upsert(
        {
            "monday_account_id":   int(account_id),
            "monday_workspace_id": None,
            "workspace_name":      f"Workspace {account_id}",
            "access_token":        access_token,
            "plan_tier":           "FREE",
            "status":              "ACTIVE",
            "is_active":           True,
            "is_paused":           False,
        },
        on_conflict="monday_account_id",
    ).execute()

    print("Token saved for account:", account_id)
    return {"status": "ok"}



# ═══════════════════════════════════════════════════════════
@router.get("/callback")
async def oauth_callback(
    code: str = Query(...),
    db: Client = Depends(get_db)
):
    """
    OAuth callback WITHOUT state support.

    Flow:
    1. Exchange code → access_token
    2. Use access_token → fetch account_id from monday API
    3. Save in DB
    4. Redirect to monday home (fallback)
    """

    # ── Step 1: Exchange code → access_token
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                settings.monday_token_url,
                data={
                    "client_id": settings.monday_client_id,
                    "client_secret": settings.monday_client_secret,
                    "code": code,
                    "redirect_uri": f"{settings.app_base_url}/api/auth/callback",
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

    # ── Step 2: Get account_id from monday API
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                "https://api.monday.com/v2",
                json={
                    "query": "query { me { account { id } } }"
                },
                headers={
                    "Authorization": access_token,
                    "Content-Type": "application/json"
                },
            )

        res.raise_for_status()
        data = res.json()

        account_id = data["data"]["me"]["account"]["id"]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch account_id: {str(e)}")

    # ── Step 3: Save to DB
    try:
        db.table("workspaces").upsert(
            {
                "monday_account_id": int(account_id),
                "monday_workspace_id": None,
                "workspace_name": f"Workspace {account_id}",
                "access_token": access_token,
                "plan_tier": "FREE",
                "status": "ACTIVE",
                "is_active": True,
                "is_paused": False,
            },
            on_conflict="monday_account_id",
        ).execute()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB save failed: {str(e)}")

    # ── Step 4: Return page that closes the OAuth tab automatically
    # The app panel's polling loop will detect has_oauth=true on the next tick.
    return HTMLResponse(content="""
<!DOCTYPE html>
<html>
<head><title>WizClone - Connected</title></head>
<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f5;">
  <div style="text-align:center;padding:40px;">
    <h2 style="color:#333;">&#10003; Connected successfully</h2>
    <p style="color:#666;">You can close this tab and return to monday.com.</p>
    <script>
      // Try to close this tab; if blocked, just show the message above.
      try { window.close(); } catch(e) {}
    </script>
  </div>
</body>
</html>
""", status_code=200)



# ═══════════════════════════════════════════════════════════
# URL 2 — POST /api/auth/verify
# Frontend calls this on every app load
# ═══════════════════════════════════════════════════════════
@router.post("/verify", response_model=VerifyResponse)
async def verify_auth(payload: VerifyRequest, request: Request, db: Client = Depends(get_db)):
    """
    Called on every app load by frontend.

    Session token is preferred but not required — if missing or expired,
    accountId from the request body is used as fallback so that a
    token refresh failure never shows the Onboard screen to an already-
    connected workspace.

    Flow:
    1. Try to decode session token → get account_id + user_id
    2. Fall back to payload.accountId if token unavailable
    3. Look up workspace by monday_account_id (SELECT only — never overwrites)
    4. Create new workspace row only if genuinely not found (INSERT, not UPSERT)
    5. Init user + default settings
    6. Return has_oauth based on whether access_token is stored
    """

    # ── Step 1: Try session token (non-fatal if missing / expired)
    account_id = None
    user_id    = None
    is_admin   = False

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token   = auth_header.replace("Bearer ", "").strip()
        decoded = _verify_session_token(token)
        if decoded:
            dat        = decoded.get("dat", {})
            account_id = dat.get("account_id")
            user_id    = dat.get("user_id")
            is_admin   = dat.get("is_admin", False)

    # ── Step 2: Fallback to body params when token unavailable
    if not account_id and payload.accountId:
        account_id = payload.accountId
    if not user_id and payload.userId:
        user_id = payload.userId

    if not account_id:
        raise HTTPException(status_code=401, detail="Cannot identify account — session token missing and no accountId provided")

    # ── Step 3: Look up workspace (safe SELECT — never overwrites access_token)
    workspace = None
    try:
        ws_result = db.table("workspaces") \
            .select("id, access_token, monday_workspace_id") \
            .eq("monday_account_id", int(account_id)) \
            .execute()
        workspace = ws_result.data[0] if ws_result.data else None
    except Exception:
        workspace = None

    # Stamp monday_workspace_id if it was NULL
    if workspace and not workspace.get("monday_workspace_id") and payload.workspaceId:
        try:
            db.table("workspaces").update({
                "monday_workspace_id": int(payload.workspaceId)
            }).eq("id", workspace["id"]).execute()
        except Exception:
            pass

    # ── Step 4: Create new workspace only when genuinely absent
    # Use INSERT (not UPSERT) so we never overwrite an existing access_token
    if workspace is None:
        try:
            ws_result = db.table("workspaces").insert({
                "monday_account_id":   int(account_id),
                "monday_workspace_id": int(payload.workspaceId) if payload.workspaceId else None,
                "workspace_name":      f"Workspace {account_id}",
                "access_token":        "",
                "plan_tier":           "FREE",
                "status":              "ACTIVE",
                "is_active":           True,
                "is_paused":           False,
            }).execute()
            workspace = ws_result.data[0]
        except Exception:
            # Unique constraint hit (race condition) — row was just created, fetch it
            try:
                ws_result = db.table("workspaces") \
                    .select("id, access_token, monday_workspace_id") \
                    .eq("monday_account_id", int(account_id)) \
                    .execute()
                workspace = ws_result.data[0] if ws_result.data else None
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to resolve workspace: {str(e)}")

    if not workspace:
        raise HTTPException(status_code=500, detail="Workspace could not be created or found")

    workspace_uuid = workspace["id"]

    # ── Step 5: Init user + default settings (non-blocking)
    if user_id:
        _init_user_and_settings(
            db             = db,
            workspace_uuid = workspace_uuid,
            user_id        = int(user_id),
            is_admin       = is_admin,
        )

    # ── Step 6: Derive has_oauth from stored access_token
    access_token = workspace.get("access_token") or ""
    has_oauth    = bool(access_token.strip()) and access_token not in ["", "test-token"]

    return VerifyResponse(
        success        = True,
        message        = "Token verified",
        has_oauth      = has_oauth,
        workspace_uuid = workspace_uuid,
        workspace_id   = payload.workspaceId,
        user_id        = int(user_id) if user_id else None,
        is_admin       = is_admin,
    )






# # ── Callback (monday.com redirects here after user approves)
# @router.get("/callback", )
# async def oauth_callback(
#     code:  str = Query(...),
#     state: str|None=None,
#     db: Client = Depends(get_db)
# ):
#     print("=", 80)
#     print("MONDAY CALLBACK")
#     print("CODE:", code)
#     print("STATE:", state)
#     print("CALLBACK_URL:")
#     print("=", 80)
#     """
#     monday.com sends code + state here after user approves OAuth.
#     state = original authorization token (contains backToUrl).

#     Flow:
#     1. Decode state → get accountId + backToUrl
#     2. Exchange code → access_token
#     3. Save access_token to DB
#     4. Redirect user to backToUrl
#     """
#     # ── Decode state to get backToUrl
#     state_data = _verify_authorization_token(state)
#     if not state_data:
#         raise HTTPException(status_code=400, detail="Invalid state token")

#     account_id  = state_data.get("accountId")
#     back_to_url = state_data.get("backToUrl")

#     # ── Exchange code for access_token
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
#         access_token = response.json().get("access_token")

#         if not access_token:
#             raise HTTPException(status_code=400, detail="No access token received")

#     except HTTPException:
#         raise
#     except Exception as e:
#         raise HTTPException(status_code=502, detail=f"Token exchange failed: {str(e)}")

#     # ── Save access_token to DB
#     try:
#         ws_result = db.table("workspaces").upsert(
#             {
#                 "monday_account_id":   int(account_id),
#                 "monday_workspace_id": None,
#                 "workspace_name":      f"Workspace {account_id}",
#                 "access_token":        access_token,
#                 "plan_tier":           "FREE",
#                 "status":              "ACTIVE",
#                 "is_active":           True,
#                 "is_paused":           False,
#             },
#             on_conflict="monday_account_id",
#         ).execute()

#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Failed to save token: {str(e)}")

#     # ── Redirect back to monday.com
#     return RedirectResponse(url=back_to_url)  # ensure absolute URL



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