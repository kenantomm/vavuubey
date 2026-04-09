from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
import state
import httpx

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
    channels = state.get_all_channels(ordered=True)
    for ch in channels:
        ch_id = ch["id"]
        ch_name = ch["name"]
        ch_grp = ch["grp"]
        ch_logo = ch.get("logo", "")
        logo_param = f' tvg-logo="{ch_logo}"' if ch_logo else ""
        lines.append(f'#EXTINF:-1 group-title="{ch_grp}"{logo_param},{ch_name}')
        lines.append(f'{host}/channel/{ch_id}')
    return "\n".join(lines)

@app.get("/get.php")
async def get_m3u(request: Request, username: str = "", password: str = "", type: str = "m3u_plus"):
    host = detect_host(request)
    return PlainTextResponse(build_m3u(host), media_type="audio/x-mpegurl")

@app.get("/player_api.php")
async def player_api(request: Request, username: str = "", password: str = "", action: str = ""):
    host = detect_host(request)
    if action == "get_live_categories":
        groups = {}
        for ch in state.get_all_channels(ordered=True):
            groups[ch["grp"]] = groups.get(ch["grp"], 0) + 1
        return JSONResponse([{"category_id": str(i+1), "category_name": g, "parent_id": 0} for i, g in enumerate(groups.keys())])
    elif action == "get_live_streams":
        return JSONResponse([{"num": i+1, "name": ch["name"], "stream_type": "live", "stream_id": ch["id"], "stream_icon": ch.get("logo", ""), "epg_channel_id": "", "added": "", "category_id": "", "custom_sid": "", "tv_archive": 0, "direct_source": "", "tv_archive_duration": 0} for i, ch in enumerate(state.get_all_channels(ordered=True))])
    return PlainTextResponse(build_m3u(host), media_type="audio/x-mpegurl")

@app.get("/channel/{ch_id}")
async def channel_stream(ch_id: int):
    """Resolve channel stream and proxy it"""
    ch = state.get_channel(ch_id)
    if not ch:
        return JSONResponse({"error": "Not found"}, status_code=404)

    # Check resolve cache first
    cache_key = str(ch_id)
    if cache_key in state.RESOLVE_CACHE:
        cached = state.RESOLVE_CACHE[cache_key]
        if cached.get("expires", 0) > time.time():
            return await proxy_stream(cached["url"], ch_id)

    # Try HLS URL from catalog first
    hls = ch.get("hls", "")
    if hls:
        add_log(f"[{ch_id}] Catalog HLS resolve: {hls[:80]}")
        resolved = await state.resolve_mediahubmx(hls)
        if resolved:
            state.RESOLVE_CACHE[cache_key] = {"url": resolved, "expires": time.time() + 300}
            return await proxy_stream(resolved, ch_id)

    # Try resolving the original URL
    url = ch.get("url", "")
    if url:
        resolved = await state.resolve_mediahubmx(url)
        if resolved:
            state.RESOLVE_CACHE[cache_key] = {"url": resolved, "expires": time.time() + 300}
            return await proxy_stream(resolved, ch_id)

    # Fallback: try direct proxy of original URL
    if url:
        return await proxy_stream(url, ch_id)

    return JSONResponse({"error": "Could not resolve stream"}, status_code=502)

async def proxy_stream(url, ch_id):
    """Fetch and proxy a stream URL with rewriting for HLS"""
    try:
        async with httpx.AsyncClient(timeout=20, verify=False, follow_redirects=True) as client:
            r = await client.get(url, headers=VAVOO_HEADERS)
            if r.status_code != 200:
                return JSONResponse({"error": f"upstream {r.status_code}", "url": url, "body": r.text[:200]}, status_code=502)
            
            content_type = r.headers.get("content-type", "")
            text = r.text
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

            lines = text.split("\n")
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
                text = r.text
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
                lines = text.split("\n")
                rewritten = "\n".join(rewrite(l) for l in lines)
                return PlainTextResponse(rewritten, media_type="application/vnd.apple.mpegurl")
            return StreamingResponse(iter([r.content]), media_type=content_type or "video/MP2T", headers={"Content-Length": str(len(r.content))})
    except httpx.TimeoutException:
        return JSONResponse({"error": "timeout"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

def add_log(msg):
    state.add_log(msg)

import time

@app.get("/test/{ch_id}")
async def test_channel(ch_id: int):
    ch = state.get_channel(ch_id)
    if not ch:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(ch)

@app.get("/api/test-sig")
async def test_sig():
    """Test Lokke signature and MediaHubMX catalog"""
    results = {}
    # Test 1: Lokke signature
    sig = await state.get_watched_sig()
    results["lokke_sig"] = bool(sig)
    results["sig_preview"] = sig[:50] + "..." if sig else None
    results["watched_sig"] = bool(state.WATCHED_SIG)

    # Test 2: Catalog fetch
    if sig:
        try:
            catalog = await state.fetch_catalog("Turkey", 0)
            if isinstance(catalog, dict):
                results["catalog_turkey"] = {
                    "items_count": len(catalog.get("items", [])),
                    "nextCursor": catalog.get("nextCursor"),
                    "first_item": catalog["items"][0] if catalog.get("items") else None
                }
            else:
                results["catalog_turkey"] = {"error": f"unexpected type: {type(catalog)}", "data": str(catalog)[:300]}
        except Exception as e:
            results["catalog_turkey"] = {"error": str(e)}

        # Test 3: Resolve
        import sqlite3
        conn = sqlite3.connect(state.DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, name, url, hls FROM channels WHERE country='TR' LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row:
            ch_id, ch_name, ch_url, ch_hls = row
            test_url = ch_hls if ch_hls else ch_url
            resolved = await state.resolve_mediahubmx(test_url)
            results["resolve_test"] = {
                "channel": ch_name,
                "input_url": test_url,
                "resolved_url": resolved
            }

    return JSONResponse(results)

@app.get("/api/status")
async def api_status(request: Request):
    host = detect_host(request)
    import sqlite3
    info = {"data_ready": state.DATA_READY, "host": host, "watched_sig": bool(state.WATCHED_SIG), "resolve_cache_size": len(state.RESOLVE_CACHE), "startup_logs": state.STARTUP_LOGS[-30:]}
    try:
        conn = sqlite3.connect(state.DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM channels")
        info["total_channels"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM channels WHERE hls != ''")
        info["hls_channels"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM channels WHERE country='TR'")
        info["tr_channels"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM channels WHERE country='DE'")
        info["de_channels"] = c.fetchone()[0]
        c.execute("SELECT DISTINCT grp FROM channels ORDER BY grp")
        info["groups"] = [r[0] for r in c.fetchall()]
        c.execute("SELECT id, name, url, hls FROM channels WHERE country='TR' LIMIT 3")
        info["sample_tr"] = [{"id": r[0], "name": r[1], "url": r[2], "hls": r[3]} for r in c.fetchall()]
        conn.close()
    except Exception as e:
        info["db_error"] = str(e)
    return JSONResponse(info)

@app.get("/api/logs")
async def api_logs():
    return JSONResponse({"logs": state.STARTUP_LOGS})

@app.get("/stats")
async def stats():
    return JSONResponse({"status": "online" if state.DATA_READY else "loading", "channels": len(state.get_all_channels(ordered=False)) if state.DATA_READY else 0})

@app.get("/ping")
@app.head("/ping")
async def ping():
    """Lightweight health check for UptimeRobot/cron-job - returns 200 OK"""
    return PlainTextResponse("pong", status_code=200, media_type="text/plain")

@app.get("/")
async def root(request: Request):
    host = detect_host(request)
    return PlainTextResponse(
        f"VxParser Online\n\n"
        f"M3U: {host}/get.php?username=admin&password=admin&type=m3u_plus\n"
        f"Status: {host}/api/status\n"
        f"Sig Test: {host}/api/test-sig\n"
        f"Logs: {host}/api/logs\n"
    )
