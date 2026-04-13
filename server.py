"""
server.py - VxParser Entry Point v3.2
FastAPI + startup + cache cleanup
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
    try:
        await state.startup_sequence()
    except Exception as e:
        state.add_log(f"Startup CRITICAL: {e}")
        import traceback
        state.add_log(traceback.format_exc())

async def cache_cleanup_loop():
    """Periodic cache cleanup to prevent memory issues"""
    while True:
        await asyncio.sleep(state.CONFIG["CACHE_CLEANUP_INTERVAL"])
        try:
            state.cleanup_resolve_cache()
        except Exception as e:
            state.add_log(f"Cache cleanup error: {e}")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_startup())
    asyncio.create_task(cache_cleanup_loop())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"VxParser v3.2 starting on port {port}...")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
