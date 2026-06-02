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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Shared DB instance — available via app.state for any middleware
# or lifespan code that can't use Depends()
from app.core.database import db

# Route modules
from app.routes.auth      import router as auth_router
from app.routes.settings  import router as settings_router
from app.routes.templates import router as templates_router
from app.routes.webhook   import router as webhook_router
from app.routes.boards    import router as boards_router

# Create FastAPI app instance
app = FastAPI(
    title       = "WizClone API",
    version     = "1.0.0",
    description = "Smart Template & Subitem Automation for monday.com",
    docs_url    = "/docs",
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

# ── Attach shared DB to app state ──
# Accessible anywhere via request.app.state.db
# This is the SAME instance as `from app.core.database import db`
# — not a duplicate client.
app.state.db = db

# ── Register routes ──
app.include_router(auth_router)       # GET  /api/auth/authorize
                                      # GET  /api/auth/callback
                                      # POST /api/auth/init
app.include_router(settings_router)   # GET  /api/settings/{workspaceId}
                                      # POST /api/settings/{workspaceId}
app.include_router(templates_router)  # GET  /api/templates/{workspaceId}
                                      # POST /api/templates/{workspaceId}
                                      # PUT  /api/templates/{workspaceId}/{templateId}
                                      # DELETE /api/templates/{workspaceId}/{templateId}
app.include_router(webhook_router)    # POST /webhook/monday

app.include_router(boards_router)


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