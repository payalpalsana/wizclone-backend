# app/routes/auth.py

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException

from app.core.database import get_supabase
from app.core.security import verify_monday_token       # Function to verify monday.com session token

from app.schemas.auth import VerifyRequest              # Request schema (contains sessionToken from frontend)

# Monday service functions
from app.services.monday_service import (
    exchange_code_for_token,                # Exchange OAuth code → access token
    get_monday_me,                          # Get user info from monday.com
)


# Create router for Auth APIs
router = APIRouter(tags=["Auth"])


# ─────────────────────────────────────────
# GET /oauth/callback
# This API is called by monday.com after user installs app
# ─────────────────────────────────────────
@router.get("/oauth/callback")
async def oauth_callback(
    code: str,      # OAuth code from monday.com
    db = Depends(get_supabase)
):

    try:

        # ── Step 1: Exchange code → access token ──
        token_data = await exchange_code_for_token(code)

        # Extract access token
        access_token = token_data["access_token"]

        # ── Step 2: Get user info from monday.com ──
        monday_user = await get_monday_me(access_token)

        # Extract "me" data
        user_data = monday_user["data"]["me"]

        # Extract account info
        account_id   = user_data["account"]["id"]
        account_name = user_data["account"]["name"]

        # ── Step 3: Upsert workspace row ──
        # We use monday account ID as workspace identifier at OAuth time
        # (workspaceId is sent later via sessionToken on app load)
        try:
            workspace_result = db.table("workspaces") \
                .upsert(
                    {
                        "monday_account_id":   int(account_id),
                        "monday_workspace_id": int(account_id),   # placeholder until real workspaceId arrives via verify
                        "workspace_name":      account_name,
                        "access_token":        access_token,
                        "plan_tier":           "FREE",
                        "status":              "ACTIVE",
                        "is_active":           True,
                        "is_paused":           False,
                    },
                    on_conflict="monday_workspace_id"   # update if already exists
                ) \
                .execute()

            workspace = workspace_result.data[0]
            workspace_uuid = workspace["id"]

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to upsert workspace: {str(e)}")

        # ── Step 4: Upsert user row ──
        try:
            db.table("users") \
                .upsert(
                    {
                        "workspace_id":   workspace_uuid,
                        "monday_user_id": int(user_data["id"]),
                        "email":          user_data.get("email"),
                        "name":           user_data.get("name"),
                        "role":           "ADMIN",
                        "is_admin":       True,
                    },
                    on_conflict="workspace_id,monday_user_id"   # update if already exists
                ) \
                .execute()

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to upsert user: {str(e)}")

        # ── Step 5: Return success response ──
        return {
            "success": True,
            "message": "OAuth Success",
            "user": user_data
        }

    except HTTPException:
        raise   # re-raise known HTTP errors as-is

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))



# ─────────────────────────────────────────
# POST /api/auth/verify
# This API verifies monday.com session token
# ─────────────────────────────────────────
@router.post("/api/auth/verify")
async def verify_auth(payload: VerifyRequest):
    try:
        # ── Step 1: Verify token using your function ──
        decoded = await verify_monday_token(
            payload.sessionToken        # Token received from frontend
        )

        # ── Step 2: If token is invalid ──
        if not decoded:
            raise HTTPException(status_code=401, detail="Invalid session token")
        
        # ── Step 3: Cross-check decoded claims against the payload ──
        # This prevents token substitution attacks where a valid token
        # for workspace A is replayed as workspace B
        decoded_account_id   = str(decoded.get("accountId", decoded.get("account_id", "")))
        decoded_user_id      = str(decoded.get("userId",    decoded.get("user_id", "")))

        if decoded_account_id and decoded_account_id != str(payload.accountId):
            raise HTTPException(status_code=401, detail="Token account mismatch")

        if decoded_user_id and decoded_user_id != str(payload.userId):
            raise HTTPException(status_code=401, detail="Token user mismatch")

        # ── Step 4: If valid → return decoded data ──
        return {
            "success": True,
            "message": "Token verified successfully",
            "decoded": decoded
        }
    
    except HTTPException:
        raise   # re-raise known HTTP errors as-is
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Token verification failed: {str(e)}")