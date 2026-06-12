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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import asyncio
import httpx

from app.core.database  import db
from app.core.security  import verify_session_token
from app.routes.auth        import router as auth_router
from app.routes.settings    import router as settings_router
from app.routes.templates   import router as templates_router
from app.routes.webhook     import router as webhook_router
from app.routes.activity_log import router as activity_log_router


# ─────────────────────────────────────────
# App instance
# ─────────────────────────────────────────
app = FastAPI(
    title            = "WizClone API",
    version          = "1.0.0",
    description      = "Smart Template & Subitem Automation for monday.com",
    docs_url         = "/docs",
    redirect_slashes = False,
)


# ─────────────────────────────────────────
# CORS
# ─────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ─────────────────────────────────────────
# Paths that do NOT require session token
# ─────────────────────────────────────────
# These paths skip the middleware token check AND
# are marked as public in Swagger (no lock icon).
PUBLIC_PATHS = [
    "/api/auth/authorization",
    "/api/auth/callback",
    "/api/auth/verify",
    "/api/auth/oauth2/authorized",
    "/webhook/monday/",
    "/docs",
    "/openapi.json",
    "/health",
    "/",
    "/routes-debug",
]


# ─────────────────────────────────────────
# Session Token Middleware
# ─────────────────────────────────────────
class SessionTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):

        # Always pass CORS preflight
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        print(f"MIDDLEWARE HIT: {path}")

        # Skip public paths
        for skip in PUBLIC_PATHS:
            if path.startswith(skip):
                return await call_next(request)

        # Require Authorization header
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing Authorization header"},
            )

        token   = auth_header.replace("Bearer ", "").strip()
        decoded = verify_session_token(token)

        if not decoded:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired session token"},
            )

        # Attach decoded token to request state
        # Access in routes via: request.state.token_data
        request.state.token_data = decoded.get("dat", {})

        return await call_next(request)


app.add_middleware(SessionTokenMiddleware)


# ─────────────────────────────────────────
# DB on app state
# ─────────────────────────────────────────
app.state.db = db


# ─────────────────────────────────────────
# Swagger — Bearer Auth only on protected routes
#
# Public routes (no lock icon in Swagger):
#   GET  /
#   GET  /health
#   GET  /routes-debug
#   GET  /api/auth/authorization
#   GET  /api/auth/callback
#   POST /api/auth/verify
#   POST /api/auth/oauth2/authorized
#   POST /webhook/monday/{workspace_id}
#
# Protected routes (lock icon — requires Bearer token):
#   POST /api/settings/load
#   POST /api/settings/save
#   GET  /api/templates/{workspaceId}
#   POST /api/templates/{workspaceId}
#   PUT  /api/templates/{workspaceId}/{templateId}
#   DEL  /api/templates/{workspaceId}/{templateId}
#   GET  /api/activity-log/{workspaceId}
# ─────────────────────────────────────────

# Route prefixes that are PUBLIC — no auth needed in Swagger
SWAGGER_PUBLIC_PREFIXES = (
    "/api/auth/",
    "/webhook/",
    "/health",
    "/routes-debug",
    "/",
)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title       = "WizClone API",
        version     = "1.0.0",
        description = "Smart Template & Subitem Automation for monday.com",
        routes      = app.routes,
    )

    # Define Bearer auth scheme
    openapi_schema.setdefault("components", {})
    openapi_schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type":        "http",
            "scheme":      "bearer",
            "bearerFormat": "JWT",
            "description": "monday.com session token — get it from: monday.get('sessionToken')",
        }
    }

    # Apply security per-route instead of globally
    # Public routes → no security (no lock icon)
    # Protected routes → BearerAuth (lock icon shown)
    for path, path_item in openapi_schema.get("paths", {}).items():
        is_public = any(path.startswith(prefix) for prefix in SWAGGER_PUBLIC_PREFIXES)

        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue

            if is_public:
                # Explicitly mark as no security — removes lock icon
                operation["security"] = []
            else:
                # Require Bearer token — shows lock icon
                operation["security"] = [{"BearerAuth": []}]

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


# ─────────────────────────────────────────
# Register routes
# ─────────────────────────────────────────
app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(templates_router)
app.include_router(webhook_router)
app.include_router(activity_log_router)


# ─────────────────────────────────────────
# Health check
# ─────────────────────────────────────────
@app.get("/", tags=["Health"])
async def root():
    return {
        "app":     "WizClone",
        "version": "1.0.0",
        "status":  "running",
        "docs":    "/docs",
    }


@app.get("/health", tags=["Health"])
async def health():
    """Liveness probe — checks Supabase connection."""
    try:
        app.state.db.table("workspaces").select("id").limit(1).execute()
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "db": str(e)}


@app.get("/routes-debug", tags=["Health"])
async def list_routes():
    """Lists all registered routes — for debugging only."""
    return [
        {"path": r.path, "methods": list(r.methods)}
        for r in app.routes
    ]

# ─────────────────────────────────────────
# Keep-Alive Task (Render Free Tier)
# ─────────────────────────────────────────
async def keep_alive():
    """Background task to ping the server every 10 minutes (600 seconds) so Render doesn't sleep."""
    url = "https://wizclone-backend.onrender.com/health"
    while True:
        await asyncio.sleep(600)
        try:
            async with httpx.AsyncClient() as client:
                await client.get(url, timeout=10)
            print(f"[Keep-Alive] Pinged {url} to keep server awake.")
        except Exception as e:
            print(f"[Keep-Alive] Ping failed: {e}")

from app.services.worker import run_worker

@app.on_event("startup")
async def startup_event():
    # Start the keep-alive ping for the free tier
    asyncio.create_task(keep_alive())
    
    # Start the background worker inside the web server process
    # so we don't have to pay for a separate worker instance on Render!
    print("[startup] Starting embedded background worker...")
    asyncio.create_task(run_worker())