"""Local dev launcher.

    python3 run.py

Equivalent to:

    uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
"""
import os

import uvicorn

from app.config import settings


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=bool(int(os.getenv("RELOAD", "1"))),
    )
