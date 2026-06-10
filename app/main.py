# app/main.py
# ─────────────────────────────────────────────────────────────
# WizClone FastAPI Backend — Entry Point
#
# Run development server:
#   uvicorn app.main:app --reload --port 8000
#
# Run background worker (separate terminal):
#   python -m app.services.worker
# ─────────────────────────────────────────────────────────────

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Shared DB instance — available via app.state for any middleware or lifespan code that can't use Depends()
from app.core.database import db

from app.core.security import verify_session_token
from app.routes.auth      import router as auth_router
from app.routes.settings  import router as settings_router
from app.routes.templates import router as templates_router
from app.routes.webhook   import router as webhook_router
from app.routes.activity_log import router as activity_log_router

# Create FastAPI app instance
app = FastAPI(
    title       = "WizClone API",
    version     = "1.0.0",
    description = "Smart Template & Subitem Automation for monday.com",
    docs_url    = "/docs",
    redirect_slashes = False,
)

# ── CORS ──
# Allow monday.com app panel (iframe) to call the backend.
# In production, restrict allow_origins to your monday app domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ─────────────────────────────────────────
# Session Token Middleware
# Verifies token from Authorization header for all protected routes
# ─────────────────────────────────────────

# These paths do NOT need session token
SKIP_PATHS = [
    "/api/auth/authorization",
    "/api/auth/callback",
    "/api/auth/verify",
    "/api/auth/oauth2/authorized",
    "/webhook/monday/",
    "/docs",
    "/openapi.json",
    "/health",
]


class SessionTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):

        # ── Always pass CORS preflight requests through
        if request.method == "OPTIONS":
            return await call_next(request)

        # ── Skip unprotected paths
        path = request.url.path
        print(f"MIDDLEWARE HIT: {path}")
        for skip in SKIP_PATHS:
            if path.startswith(skip):
                return await call_next(request)

        # ── Extract token from Authorization header
        # Frontend must send: Authorization: Bearer <sessionToken>
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing Authorization header"}
            )

        token = auth_header.replace("Bearer ", "").strip()

        # ── Verify session token
        decoded = verify_session_token(token)
        if not decoded:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired session token"}
            )

        # ── Attach decoded token to request state
        # Routes can access it via: request.state.token_data
        request.state.token_data = decoded.get("dat", {})

        return await call_next(request)


app.add_middleware(SessionTokenMiddleware)


# ── Attach shared DB to app state ──
# Accessible anywhere via request.app.state.db
# This is the SAME instance as `from app.core.database import db`
app.state.db = db

# ── Register routes ──
app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(templates_router)
app.include_router(webhook_router)
app.include_router(activity_log_router) 


# ── Health check ──
@app.get("/")
async def root():
    return {
        "app":     "WizClone",
        "version": "1.0.0",
        "status":  "running",
        "docs":    "/docs",
    }


# ── DB availability check ──
@app.get("/health")
async def health():
    """
    Quick liveness probe.
    Checks that the Supabase client can reach the DB.
    """
    try:
        app.state.db.table("workspaces").select("id").limit(1).execute()
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "db": str(e)}

@app.get("/routes-debug")
async def list_routes():
    return [{"path": r.path, "methods": list(r.methods)} for r in app.routes]
