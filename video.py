from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse
import state
import httpx
import time

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
            content_type = r.headers.get("content-type", "")
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

@app.get("/test/{ch_id}")
async def test_channel(ch_id: int):
    ch = state.get_channel(ch_id)
    if not ch:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(ch)

@app.get("/api/test-sig")
async def test_sig():
    results = {}
    sig = await state.get_watched_sig()
    results["lokke_sig"] = bool(sig)
    results["sig_preview"] = sig[:50] + "..." if sig else None
    if sig:
        try:
            catalog = await state.fetch_catalog("Turkey", 0)
            if isinstance(catalog, dict):
                items = catalog.get("items", [])
                results["catalog_turkey"] = {"count": len(items), "first": items[0] if items else None}
        except Exception as e:
            results["catalog_turkey"] = {"error": str(e)}
        import sqlite3
        conn = sqlite3.connect(state.DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, name, url, hls FROM channels WHERE country='TR' LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row:
            test_url = row[3] if row[3] else row[2]
            resolved = await state.resolve_mediahubmx(test_url)
            results["resolve"] = {"channel": row[1], "input": test_url[:80], "resolved": resolved}
    return JSONResponse(results)

@app.get("/api/status")
async def api_status(request: Request):
    host = detect_host(request)
    import sqlite3
    info = {"data_ready": state.DATA_READY, "host": host, "watched_sig": bool(state.WATCHED_SIG), "startup_logs": state.STARTUP_LOGS[-30:]}
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
        c.execute("SELECT id, name, url, hls FROM channels WHERE country='TR' LIMIT 2")
        info["samples"] = [{"id": r[0], "name": r[1], "url": r[2], "hls": r[3]} for r in c.fetchall()]
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
    return PlainTextResponse(f"VxParser Online\n\nM3U: {host}/get.php?username=admin&password=admin&type=m3u_plus\nSig: {host}/api/test-sig\nLogs: {host}/api/logs\n")
