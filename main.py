# ============================================================
# WAY TERO — UVICORN ENTRY POINT
# File: Backend/main.py
# Run: uvicorn main:app --reload --host 0.0.0.0 --port 8000
# ============================================================
import uvicorn
from app.main import app
from app.core.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.is_development,
        log_level="debug" if settings.APP_DEBUG else "info",
        access_log=False,  # We use our own logging middleware
    )
