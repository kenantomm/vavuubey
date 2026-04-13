"""
server.py - VxParser Entry Point
Starts FastAPI + runs startup sequence
"""
import asyncio
import logging
import sys
import os

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)

import state
from video import app

async def run_startup():
    """Run startup sequence in background"""
    try:
        await state.startup_sequence()
    except Exception as e:
        state.add_log(f"Startup CRITICAL: {e}")
        import traceback
        state.add_log(traceback.format_exc())

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_startup())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"VxParser starting on port {port}...")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
