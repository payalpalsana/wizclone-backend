# app/main.py

from fastapi import FastAPI
from app.core.database import get_supabase
from app.routes.auth import router as auth_router
from app.routes.settings import router as settings_router
from app.routes.boards import router as boards_router
from app.routes.templates import router as templates_router



# Create FastAPI app instance
app = FastAPI(
    title="WizClone Backend",
    version="1.0.0"
)

app.state.db = get_supabase()

# Register all auth-related routes 
app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(boards_router)
app.include_router(templates_router, prefix="/api")

# Root endpoint (for testing API is working or not)
@app.get("/")
async def root():
    return {
        "message": "WizClone API is running",
        "version": "1.0.0",
        "docs":    "/docs"
    }

