"""
server.py - Entry point. Starts FastAPI + runs startup sequence
"""
import asyncio
import logging
import sys
import os

# Suppress httpx warnings
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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

async def run_sig_refresh():
    """Background task: signature refresh + self-ping keepalive"""
    try:
        await state.sig_refresh_loop()
    except Exception as e:
        state.add_log(f"Sig refresh CRITICAL: {e}")
        import traceback
        state.add_log(traceback.format_exc())
        # Tekrar baslat
        await asyncio.sleep(10)
        asyncio.create_task(run_sig_refresh())

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_startup())
    # Sig refresh loop hemen baslat (startup bittikten sonra da calisir)
    asyncio.create_task(run_sig_refresh())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"VxParser starting on port {port}...")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
