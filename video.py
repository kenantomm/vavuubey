from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse
import state
import httpx
import time
import asyncio

app = FastAPI(title="VxParser")

VAVOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://vavoo.to/",
    "Origin": "https://vavoo.to",
    "Accept": "*/*",
}

def detect_host(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-Proto", "https")
    host = request.headers.get("Host", request.url.hostname or "localhost")
    return f"{forwarded}://{host}"

def build_m3u(host: str) -> str:
    lines = ['#EXTM3U url-tvg="" deinterlace="1"']
    for ch in state.get_all_channels(ordered=True):
        logo = ch.get("logo", "")
        logo_p = f' tvg-logo="{logo}"' if logo else ""
        lines.append(f'#EXTINF:-1 group-title="{ch["grp"]}"{logo_p},{ch["name"]}')
        lines.append(f'{host}/channel/{ch["id"]}')
    return "\n".join(lines)

@app.get("/get.php")
async def get_m3u(request: Request, username: str = "", password: str = "", type: str = "m3u_plus"):
    return PlainTextResponse(build_m3u(detect_host(request)), media_type="audio/x-mpegurl")

@app.get("/player_api.php")
async def player_api(request: Request, username: str = "", password: str = "", action: str = ""):
    host = detect_host(request)
    if action == "get_live_categories":
        groups = {}
        for ch in state.get_all_channels(ordered=True):
            groups[ch["grp"]] = groups.get(ch["grp"], 0) + 1
        return JSONResponse([{"category_id": str(i+1), "category_name": g, "parent_id": 0} for i, g in enumerate(groups.keys())])
    elif action == "get_live_streams":
        return JSONResponse([{"num": i+1, "name": ch["name"], "stream_type": "live", "stream_id": ch["id"], "stream_icon": ch.get("logo", "")} for i, ch in enumerate(state.get_all_channels(ordered=True))])
    return PlainTextResponse(build_m3u(host), media_type="audio/x-mpegurl")

@app.get("/channel/{ch_id}")
async def channel_stream(ch_id: int):
    ch = state.get_channel(ch_id)
    if not ch:
        return JSONResponse({"error": "Not found"}, status_code=404)
    cache_key = str(ch_id)
    if cache_key in state.RESOLVE_CACHE:
        cached = state.RESOLVE_CACHE[cache_key]
        if cached.get("expires", 0) > time.time():
            return await proxy_stream(cached["url"], ch_id)
    hls = ch.get("hls", "")
    if hls:
        resolved = await state.resolve_mediahubmx(hls)
        if resolved:
            state.RESOLVE_CACHE[cache_key] = {"url": resolved, "expires": time.time() + 300}
            return await proxy_stream(resolved, ch_id)
    url = ch.get("url", "")
    if url:
        resolved = await state.resolve_mediahubmx(url)
        if resolved:
            state.RESOLVE_CACHE[cache_key] = {"url": resolved, "expires": time.time() + 300}
            return await proxy_stream(resolved, ch_id)
    if url:
        return await proxy_stream(url, ch_id)
    return JSONResponse({"error": "Could not resolve"}, status_code=502)

async def proxy_stream(url, ch_id):
    try:
        async with httpx.AsyncClient(timeout=20, verify=False, follow_redirects=True) as client:
            r = await client.get(url, headers=VAVOO_HEADERS)
            if r.status_code != 200:
                return JSONResponse({"error": f"upstream {r.status_code}", "url": url[:100]}, status_code=502)
            base = url.rsplit("/", 1)[0] + "/"
            def rewrite(line):
                line = line.strip()
                if not line or line.startswith("#"):
                    return line
                if line.startswith("http"):
                    return f"/channel/{ch_id}/stream?url={line}"
                else:
                    abs_url = base + line if not line.startswith("/") else f"https://vavoo.to{line}"
                    return f"/channel/{ch_id}/stream?url={abs_url}"
            lines = r.text.split("\n")
            rewritten = "\n".join(rewrite(l) for l in lines)
            return PlainTextResponse(rewritten, media_type="application/vnd.apple.mpegurl")
    except httpx.TimeoutException:
        return JSONResponse({"error": "timeout"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/channel/{ch_id}/stream")
async def channel_substream(ch_id: int, url: str):
    if not url:
        return JSONResponse({"error": "no url"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=20, verify=False, follow_redirects=True) as client:
            r = await client.get(url, headers=VAVOO_HEADERS)
            if r.status_code != 200:
                return JSONResponse({"error": f"upstream {r.status_code}"}, status_code=502)
            content_type = r.headers.get("content-type", "")
            if "mpegurl" in content_type or ".m3u8" in url:
                base = url.rsplit("/", 1)[0] + "/"
                def rewrite(line):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        return line
                    if line.startswith("http"):
                        return f"/channel/{ch_id}/stream?url={line}"
                    else:
                        abs_url = base + line if not line.startswith("/") else f"https://vavoo.to{line}"
                        return f"/channel/{ch_id}/stream?url={abs_url}"
                lines = r.text.split("\n")
                rewritten = "\n".join(rewrite(l) for l in lines)
                return PlainTextResponse(rewritten, media_type="application/vnd.apple.mpegurl")
            return StreamingResponse(iter([r.content]), media_type=content_type or "video/MP2T", headers={"Content-Length": str(len(r.content))})
    except httpx.TimeoutException:
        return JSONResponse({"error": "timeout"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# === KEEPALIVE / HEALTH ===

@app.get("/ping")
async def ping():
    """Ultra-lightweight health check. UptimeRobot uses this.
    Also triggers sig refresh if older than 25 minutes."""
    sig_age = time.time() - state.WATCHED_SIG_TIME if state.WATCHED_SIG_TIME else 9999
    if sig_age > 1500:  # 25 minutes
        asyncio.create_task(state.refresh_watched_sig(force=True))
    return PlainTextResponse("pong", status_code=200)

@app.get("/wake")
async def wake():
    """Full health check + recovery. Use this for manual wake-up."""
    result = {"awake": True, "data_ready": state.DATA_READY, "sig": bool(state.WATCHED_SIG)}
    if not state.DATA_READY:
        asyncio.create_task(state.startup_sequence())
        result["restarting"] = True
    if not state.WATCHED_SIG:
        await state.refresh_watched_sig(force=True)
        result["sig_refreshed"] = bool(state.WATCHED_SIG)
    return JSONResponse(result)

# === DEBUG ===

@app.get("/test/{ch_id}")
async def test_channel(ch_id: int):
    ch = state.get_channel(ch_id)
    if not ch:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(ch)

@app.get("/api/status")
async def api_status(request: Request):
    host = detect_host(request)
    import sqlite3
    info = {"data_ready": state.DATA_READY, "host": host, "sig": bool(state.WATCHED_SIG), "sig_age_sec": int(time.time() - state.WATCHED_SIG_TIME) if state.WATCHED_SIG_TIME else -1, "cache_size": len(state.RESOLVE_CACHE), "startup_logs": state.STARTUP_LOGS[-20:]}
    try:
        conn = sqlite3.connect(state.DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM channels")
        info["total"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM channels WHERE hls != ''")
        info["hls"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM channels WHERE country='TR'")
        info["tr"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM channels WHERE country='DE'")
        info["de"] = c.fetchone()[0]
        conn.close()
    except Exception as e:
        info["db_error"] = str(e)
    return JSONResponse(info)

@app.get("/api/logs")
async def api_logs():
    return JSONResponse({"logs": state.STARTUP_LOGS})

@app.get("/stats")
async def stats():
    return JSONResponse({"status": "online" if state.DATA_READY else "loading", "channels": len(state.get_all_channels(False)) if state.DATA_READY else 0})

@app.get("/")
async def root(request: Request):
    host = detect_host(request)
    return PlainTextResponse(f"VxParser Online\n\nM3U: {host}/get.php?username=admin&password=admin&type=m3u_plus\nPing: {host}/ping\nLogs: {host}/api/logs\n")
