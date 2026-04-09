from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse, Response, HTMLResponse
from fastapi.routing import APIRoute
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
    """Build M3U playlist with tvg-id, tvg-logo, tvg-name"""
    lines = ['#EXTM3U url-tvg="" deinterlace="1"']
    channels = state.get_all_channels(ordered=True)
    for ch in channels:
        ch_id = ch["id"]
        ch_name = ch["name"]
        ch_grp = ch["grp"]
        ch_logo = ch.get("picon", "") or ch.get("logo", "")
        ch_tvg_id = ch.get("tvg_id", "")
        
        attrs = []
        if ch_tvg_id:
            attrs.append(f'tvg-id="{ch_tvg_id}"')
        if ch_logo:
            attrs.append(f'tvg-logo="{ch_logo}"')
        attrs.append(f'group-title="{ch_grp}"')
        
        attr_str = " ".join(attrs)
        lines.append(f'#EXTINF:-1 {attr_str},{ch_name}')
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
        return JSONResponse([{
            "num": i+1,
            "name": ch["name"],
            "stream_type": "live",
            "stream_id": ch["id"],
            "stream_icon": ch.get("picon", "") or ch.get("logo", ""),
            "epg_channel_id": ch.get("tvg_id", ""),
            "added": "",
            "category_id": "",
            "custom_sid": "",
            "tv_archive": 0,
            "direct_source": "",
            "tv_archive_duration": 0
        } for i, ch in enumerate(state.get_all_channels(ordered=True))])
    return PlainTextResponse(build_m3u(host), media_type="audio/x-mpegurl")

# ===== EPG Endpoints =====

@app.get("/epg.xml")
async def get_epg_xml():
    """Serve EPG XML file"""
    try:
        import epg
        xml_str = epg.get_cached_epg_xml()
        if xml_str:
            return PlainTextResponse(xml_str, media_type="application/xml; charset=utf-8")
    except Exception as e:
        state.add_log(f"EPG XML serve error: {e}")
    return PlainTextResponse('<?xml version="1.0" encoding="utf-8"?><tv></tv>', media_type="application/xml; charset=utf-8")

@app.get("/epg.xml.gz")
async def get_epg_gz():
    """Serve gzipped EPG XML"""
    try:
        import epg
        gz_data = epg.get_cached_epg_gz()
        if gz_data:
            return Response(content=gz_data, media_type="application/x-gzip",
                          headers={"Content-Disposition": "attachment; filename=epg.xml.gz"})
    except Exception as e:
        state.add_log(f"EPG GZ serve error: {e}")
    import gzip
    empty = gzip.compress(b'<?xml version="1.0" encoding="utf-8"?><tv></tv>')
    return Response(content=empty, media_type="application/x-gzip")

@app.get("/xmltv.xml")
async def get_xmltv():
    """Serve XMLTV format EPG (alias for epg.xml)"""
    return await get_epg_xml()

# ===== Picon Proxy Endpoint =====

@app.get("/picon/{ch_name:path}")
async def get_picon(ch_name: str):
    """Proxy picon images to avoid CORS and mixed content issues"""
    try:
        channels = state.get_all_channels(ordered=False)
        for ch in channels:
            ch_norm = ch["name"].upper().strip()
            req_norm = ch_name.upper().strip().replace("_", " ").replace("-", " ")
            if ch_norm == req_norm or ch_norm in req_norm or req_norm in ch_norm:
                picon_url = ch.get("picon", "") or ch.get("logo", "")
                if picon_url:
                    async with httpx.AsyncClient(timeout=10, verify=False) as client:
                        r = await client.get(picon_url, follow_redirects=True)
                        if r.status_code == 200:
                            ct = r.headers.get("content-type", "image/png")
                            return Response(content=r.content, media_type=ct)
                break
    except Exception:
        pass
    import base64
    tiny_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPj/HwADBwIAMCbHYQAAAABJRU5ErkJggg==")
    return Response(content=tiny_png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

# ===== Channel Stream Endpoints =====

@app.get("/channel/{ch_id}")
async def channel_stream(ch_id: int):
    """Resolve channel stream and proxy it"""
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
        state.add_log(f"[{ch_id}] Catalog HLS resolve: {hls[:80]}")
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

# ===== Utility Endpoints =====

def add_log(msg):
    state.add_log(msg)

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
    sig = await state.get_watched_sig()
    results["lokke_sig"] = bool(sig)
    results["sig_preview"] = sig[:50] + "..." if sig else None
    results["watched_sig"] = bool(state.WATCHED_SIG)

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
    info = {
        "data_ready": state.DATA_READY,
        "epg_ready": state.EPG_READY,
        "host": host,
        "watched_sig": bool(state.WATCHED_SIG),
        "resolve_cache_size": len(state.RESOLVE_CACHE),
        "startup_logs": state.STARTUP_LOGS[-30:]
    }
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
        c.execute("SELECT COUNT(*) FROM channels WHERE tvg_id != ''")
        info["epg_mapped"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM channels WHERE picon != ''")
        info["picon_mapped"] = c.fetchone()[0]
        c.execute("SELECT DISTINCT grp FROM channels ORDER BY grp")
        info["groups"] = [r[0] for r in c.fetchall()]
        c.execute("SELECT id, name, url, hls, tvg_id, picon FROM channels WHERE country='TR' LIMIT 3")
        info["sample_tr"] = [{"id": r[0], "name": r[1], "url": r[2], "hls": r[3], "tvg_id": r[4], "picon": r[5]} for r in c.fetchall()]
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
        f"EPG: {host}/epg.xml.gz\n"
        f"Admin: {host}/admin\n"
        f"Status: {host}/api/status\n"
        f"Sig Test: {host}/api/test-sig\n"
        f"Logs: {host}/api/logs\n"
    )

# ===== Admin Panel =====

ALL_GROUPS = [
    "TR ULUSAL", "TR SPOR", "TR SINEMA", "TR SINEMA VOD", "TR DIZI", "TR 7/24 DIZI",
    "TR BELGESEL", "TR COCUK", "TR MUZIK", "TR HABER", "TR DINI", "TR YEREL",
    "TR RADYO", "TR 4K", "TR 8K", "TR RAW",
    "DE VOLLPROGRAMM", "DE NACHRICHTEN", "DE DOKU", "DE KINDER", "DE FILM",
    "DE MUSIK", "DE SPORT", "DE SONSTIGE",
]

@app.get("/admin")
async def admin_page():
    return HTMLResponse(ADMIN_HTML)

@app.get("/api/admin/channels")
async def admin_get_channels():
    channels = state.get_all_channels(ordered=True)
    overrides = state.get_all_overrides()
    return JSONResponse([{
        "name": c["name"], "grp": c["grp"], "country": c["country"],
        "id": c["id"], "logo": c.get("picon", "") or c.get("logo", ""),
        "has_override": c["name"].upper().strip() in overrides
    } for c in channels])

@app.get("/api/admin/overrides")
async def admin_get_overrides():
    return JSONResponse(state.get_all_overrides())

@app.post("/api/admin/overrides")
async def admin_save_overrides(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        return JSONResponse({"error": "dict expected"}, status_code=400)
    for name, group in data.items():
        state.set_override(name, group)
    return JSONResponse({"ok": True, "count": len(data)})

@app.delete("/api/admin/overrides")
async def admin_clear_overrides():
    state.delete_all_overrides()
    return JSONResponse({"ok": True})

@app.get("/api/admin/overrides/export")
async def admin_export_overrides():
    overrides = state.get_all_overrides()
    import json
    text = json.dumps(overrides, indent=2, ensure_ascii=False)
    return PlainTextResponse(text, media_type="application/json",
                             headers={"Content-Disposition": "attachment; filename=vxparser-overrides.json"})

@app.post("/api/admin/overrides/import")
async def admin_import_overrides(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        return JSONResponse({"error": "dict expected"}, status_code=400)
    count = state.import_overrides(data)
    return JSONResponse({"ok": True, "imported": count})

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VxParser Admin</title>
<style>
:root{
  --bg:#0a0e17;--bg2:#111827;--bg3:#1a2236;--bg4:#232d42;
  --border:#2a3550;--border2:#3a4a6b;
  --text:#e2e8f0;--text2:#94a3b8;--text3:#64748b;
  --blue:#3b82f6;--blue2:#2563eb;--blue-bg:rgba(59,130,246,.1);
  --green:#22c55e;--green-bg:rgba(34,197,94,.1);
  --red:#ef4444;--red-bg:rgba(239,68,68,.1);
  --yellow:#f59e0b;--yellow-bg:rgba(245,158,11,.1);
  --purple:#a855f7;--purple-bg:rgba(168,85,247,.1);
  --sidebar-w:280px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow:hidden}
.app{display:flex;height:100vh}
.sidebar{width:var(--sidebar-w);min-width:var(--sidebar-w);background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;height:100vh;overflow:hidden;transition:transform .3s}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}

/* SIDEBAR */
.sb-hdr{padding:20px;border-bottom:1px solid var(--border);background:linear-gradient(135deg,var(--bg2),var(--bg3))}
.sb-hdr h1{font-size:18px;font-weight:700;display:flex;align-items:center;gap:10px}
.sb-hdr h1 .icon{width:32px;height:32px;background:linear-gradient(135deg,var(--blue),var(--purple));border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;color:#fff}
.sb-hdr p{font-size:11px;color:var(--text3);margin-top:4px;letter-spacing:.3px}

.sb-search{padding:12px}
.sb-search .search-box{position:relative}
.sb-search input{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:9px 12px 9px 36px;border-radius:8px;font-size:13px;outline:none;transition:.2s}
.sb-search input:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(59,130,246,.15)}
.sb-search .s-icon{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--text3);font-size:14px;pointer-events:none}
.sb-search .s-clear{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text3);cursor:pointer;font-size:16px;display:none;padding:2px 4px;border-radius:4px}
.sb-search .s-clear:hover{color:var(--text);background:var(--bg3)}
.sb-search input:not(:placeholder-shown)~.s-clear{display:block}

.sb-groups{flex:1;overflow-y:auto;padding:4px 8px 16px}
.sb-groups::-webkit-scrollbar{width:4px}
.sb-groups::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
.sb-section{margin-top:8px}
.sb-section-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;color:var(--text3);padding:8px 10px 4px;display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none}
.sb-section-title:hover{color:var(--text2)}
.sb-section-title .arrow{font-size:8px;transition:transform .2s}
.sb-section-title.collapsed .arrow{transform:rotate(-90deg)}
.sb-section-body.collapsed{display:none}
.grp-item{display:flex;align-items:center;padding:7px 10px;border-radius:6px;cursor:pointer;transition:.15s;gap:8px;font-size:13px;margin:1px 0;position:relative}
.grp-item:hover{background:var(--bg3)}
.grp-item.active{background:var(--blue-bg);color:var(--blue);font-weight:600}
.grp-item .g-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.grp-item .g-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.grp-item .g-cnt{font-size:11px;color:var(--text3);background:var(--bg);padding:1px 7px;border-radius:10px;font-weight:600}
.grp-item.active .g-cnt{background:rgba(59,130,246,.2);color:var(--blue)}

/* DROP TARGET styles */
.grp-item.drop-target{background:rgba(34,197,94,.15)!important;border:2px dashed var(--green);border-radius:6px;padding:5px 8px}
.grp-item.drop-target .g-dot{background:var(--green)!important;box-shadow:0 0 8px var(--green)}
.grp-item.drop-reject{border:2px dashed var(--red)!important;background:rgba(239,68,68,.1)!important}

/* DRAG styles on table rows */
.ch-dragging{opacity:.4}
.ch-drag-clone{position:fixed;pointer-events:none;z-index:9999;background:var(--bg3);border:1px solid var(--blue);border-radius:8px;padding:8px 14px;font-size:13px;color:var(--text);box-shadow:0 8px 24px rgba(0,0,0,.5);display:flex;align-items:center;gap:8px;max-width:300px}
.ch-drag-clone .dc-icon{font-size:16px}

/* DROP HINT banner */
.drop-hint{position:fixed;top:0;left:var(--sidebar-w);right:0;height:4px;background:linear-gradient(90deg,var(--blue),var(--green),var(--blue));z-index:100;opacity:0;transition:opacity .2s;pointer-events:none}
.drop-hint.active{opacity:1;animation:hintPulse 1s infinite}
@keyframes hintPulse{0%,100%{opacity:.7}50%{opacity:1}}

.sb-footer{padding:12px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:4px}
.sb-footer .sbtn{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;border:none;background:none;color:var(--text2);font-size:12px;cursor:pointer;transition:.15s;width:100%;text-align:left}
.sb-footer .sbtn:hover{background:var(--bg3);color:var(--text)}
.sb-footer .sbtn svg{width:16px;height:16px;flex-shrink:0}

/* TOP BAR */
.topbar{padding:16px 24px;border-bottom:1px solid var(--border);background:var(--bg2);display:flex;align-items:center;gap:16px;flex-shrink:0}
.topbar .mob-toggle{display:none;background:none;border:none;color:var(--text);font-size:20px;cursor:pointer;padding:4px}
.stats-row{display:flex;gap:12px;flex:1;flex-wrap:wrap}
.stat-card{display:flex;align-items:center;gap:10px;padding:8px 16px;background:var(--bg);border:1px solid var(--border);border-radius:10px;min-width:120px}
.stat-card .sc-icon{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
.stat-card .sc-icon.blue{background:var(--blue-bg);color:var(--blue)}
.stat-card .sc-icon.green{background:var(--green-bg);color:var(--green)}
.stat-card .sc-icon.yellow{background:var(--yellow-bg);color:var(--yellow)}
.stat-card .sc-icon.purple{background:var(--purple-bg);color:var(--purple)}
.stat-card .sc-info b{display:block;font-size:18px;font-weight:700;line-height:1.2}
.stat-card .sc-info small{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px}
.topbar-actions{display:flex;gap:8px;flex-shrink:0}

/* TOOLBAR */
.toolbar{padding:12px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;background:var(--bg2);flex-shrink:0;flex-wrap:wrap}
.toolbar .bulk-sel{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text2)}
.toolbar .bulk-sel label{cursor:pointer;display:flex;align-items:center;gap:4px}
.toolbar .search-main{position:relative;flex:1;max-width:400px}
.toolbar .search-main input{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:8px 36px 8px 36px;border-radius:8px;font-size:13px;outline:none;transition:.2s}
.toolbar .search-main input:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(59,130,246,.15)}
.toolbar .search-main .si{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--text3);font-size:13px;pointer-events:none}
.toolbar .search-main .sc{position:absolute;right:6px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text3);cursor:pointer;font-size:14px;padding:2px;display:none;border-radius:4px}
.toolbar .search-main input:not(:placeholder-shown)~.sc{display:block}
.toolbar .search-main .sc:hover{color:var(--text)}
.toolbar .result-count{font-size:12px;color:var(--text3);white-space:nowrap}
.drag-hint-bar{font-size:11px;color:var(--blue);display:flex;align-items:center;gap:4px;white-space:nowrap;padding:4px 10px;background:var(--blue-bg);border-radius:6px;border:1px solid rgba(59,130,246,.2)}
.drag-hint-bar svg{width:14px;height:14px}

/* BUTTONS */
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:8px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;font-weight:500;cursor:pointer;transition:.15s;white-space:nowrap}
.btn:hover{background:var(--bg4);border-color:var(--border2)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn svg{width:14px;height:14px}
.btn-blue{background:var(--blue);border-color:var(--blue2);color:#fff}
.btn-blue:hover{background:var(--blue2)}
.btn-green{background:var(--green);border-color:#16a34a;color:#fff}
.btn-green:hover{background:#16a34a}
.btn-green:disabled{background:var(--green);opacity:.5}
.btn-red{background:var(--red);border-color:#dc2626;color:#fff}
.btn-red:hover{background:#dc2626}
.btn-ghost{background:transparent;border-color:transparent}
.btn-ghost:hover{background:var(--bg3)}

/* TABLE */
.table-wrap{flex:1;overflow-y:auto;padding:0 24px 24px}
.table-wrap::-webkit-scrollbar{width:6px}
.table-wrap::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
.tbl{background:var(--bg2);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.tbl table{width:100%;border-collapse:collapse}
.tbl thead{position:sticky;top:0;z-index:3}
.tbl th{background:var(--bg3);padding:10px 14px;text-align:left;font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
.tbl td{padding:8px 14px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:middle}
.tbl tr:last-child td{border-bottom:none}
.tbl tr:hover td{background:rgba(59,130,246,.03)}
.tbl tr.changed td{background:var(--green-bg);border-bottom-color:rgba(34,197,94,.2)}
.tbl tr.changed:hover td{background:rgba(34,197,94,.15)}
.tbl tr[draggable=true]{cursor:grab}
.tbl tr[draggable=true]:active{cursor:grabbing}

.ch-row{display:flex;align-items:center;gap:10px}
.ch-logo{width:32px;height:32px;border-radius:6px;background:var(--bg);flex-shrink:0;display:flex;align-items:center;justify-content:center;overflow:hidden;border:1px solid var(--border)}
.ch-logo img{width:100%;height:100%;object-fit:contain}
.ch-logo .placeholder{font-size:14px;color:var(--text3)}
.ch-info .ch-name{font-weight:500;font-size:13px}
.ch-info .ch-meta{font-size:10px;color:var(--text3);margin-top:1px}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700;letter-spacing:.3px}
.b-tr{background:var(--blue-bg);color:var(--blue)}
.b-de{background:var(--red-bg);color:var(--red)}
.gsel{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:6px;font-size:12px;width:200px;outline:none;cursor:pointer;transition:.2s}
.gsel:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(59,130,246,.15)}
.tag{display:inline-flex;align-items:center;gap:3px;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600}
.tag-ovr{background:var(--yellow-bg);color:var(--yellow)}
.tag-chg{background:var(--green-bg);color:var(--green)}
.tag-cur{background:var(--bg3);color:var(--text2)}
.cb{width:16px;height:16px;accent-color:var(--blue);cursor:pointer}

/* TOAST */
.toast{position:fixed;bottom:24px;right:24px;background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:12px 20px;transform:translateY(100px);opacity:0;transition:.3s cubic-bezier(.4,0,.2,1);z-index:999;font-size:13px;display:flex;align-items:center;gap:8px;box-shadow:0 8px 32px rgba(0,0,0,.4)}
.toast.show{transform:translateY(0);opacity:1}
.toast.ok{border-color:var(--green)}.toast.ok .t-dot{background:var(--green)}
.toast.err{border-color:var(--red)}.toast.err .t-dot{background:var(--red)}
.toast .t-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}

#fileIn{display:none}
.empty{text-align:center;padding:60px 20px;color:var(--text3)}
.empty .em-icon{font-size:40px;margin-bottom:12px;opacity:.5}
.empty p{font-size:14px}

.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:90}
@media(max-width:768px){
  .sidebar{position:fixed;left:0;top:0;z-index:95;transform:translateX(-100%)}
  .sidebar.open{transform:translateX(0)}
  .overlay.show{display:block}
  .topbar .mob-toggle{display:block}
  .stats-row{gap:8px}
  .stat-card{min-width:80px;padding:6px 10px}
  .stat-card .sc-info b{font-size:15px}
  .gsel{width:140px;font-size:11px}
  .table-wrap{padding:0 12px 12px}
  .toolbar{padding:10px 12px}
  .tbl td,.tbl th{padding:6px 8px;font-size:11px}
  .ch-logo{width:28px;height:28px}
}

::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--border2)}

@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
.tbl tr{animation:fadeIn .15s ease-out}
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
</head>
<body>
<div class="overlay" id="overlay" onclick="toggleSidebar()"></div>
<div class="drop-hint" id="dropHint"></div>
<div class="app">

<!-- SIDEBAR -->
<aside class="sidebar" id="sidebar">
  <div class="sb-hdr">
    <h1><div class="icon">&#9656;</div> VxParser <span style="color:var(--blue)">Admin</span></h1>
    <p>Kanal Grup Yonetimi &bull; Drag &amp; Drop</p>
  </div>
  <div class="sb-search">
    <div class="search-box">
      <span class="s-icon">&#128269;</span>
      <input type="text" id="sideSearch" placeholder="Kanal ara..." oninput="onSideSearch(this.value)">
      <button class="s-clear" onclick="clearSideSearch()">&#10005;</button>
    </div>
  </div>
  <div class="sb-groups" id="sbGroups"></div>
  <div class="sb-footer">
    <button class="sbtn" onclick="doExport()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
      Disa Aktar JSON
    </button>
    <button class="sbtn" onclick="document.getElementById('fileIn').click()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>
      Ice Aktar JSON
    </button>
    <button class="sbtn" onclick="copyJson()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
      Kopyala
    </button>
    <button class="sbtn" style="color:var(--red)" onclick="doReset()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
      Sifirla
    </button>
  </div>
</aside>

<!-- MAIN -->
<div class="main">
  <div class="topbar">
    <button class="mob-toggle" onclick="toggleSidebar()">&#9776;</button>
    <div class="stats-row">
      <div class="stat-card"><div class="sc-icon blue">&#128250;</div><div class="sc-info"><b id="sTotal">-</b><small>Toplam</small></div></div>
      <div class="stat-card"><div class="sc-icon green">&#128193;</div><div class="sc-info"><b id="sGroups">-</b><small>Grup</small></div></div>
      <div class="stat-card"><div class="sc-icon yellow">&#9998;</div><div class="sc-info"><b id="sOverride">0</b><small>Override</small></div></div>
      <div class="stat-card"><div class="sc-icon purple">&#9999;</div><div class="sc-info"><b id="sChanged">0</b><small>Bekleyen</small></div></div>
    </div>
    <div class="topbar-actions">
      <button class="btn btn-green" id="saveBtn" onclick="save()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
        Kaydet
      </button>
    </div>
  </div>

  <div class="toolbar">
    <div class="bulk-sel">
      <label><input type="checkbox" class="cb" id="selectAll" onchange="toggleSelectAll()"> Tumunu Sec</label>
      <select class="gsel" id="bulkGroup" style="width:180px"><option value="">Toplu Tasi...</option></select>
      <button class="btn btn-blue" onclick="bulkMove()" id="bulkBtn" disabled>Uygula</button>
    </div>
    <div class="search-main">
      <span class="si">&#128269;</span>
      <input type="text" id="mainSearch" placeholder="Kanal adi ile ara..." oninput="onMainSearch(this.value)">
      <button class="sc" onclick="clearMainSearch()">&#10005;</button>
    </div>
    <div class="drag-hint-bar" id="dragHintBar">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 9l7-7 7 7M5 15l7 7 7-7"/></svg>
      Kanallari gruplara surukleyin
    </div>
    <span class="result-count" id="resultCount"></span>
  </div>

  <div class="table-wrap">
    <div class="tbl">
      <table>
        <thead>
          <tr>
            <th style="width:36px"><input type="checkbox" class="cb" id="selectAll2" onchange="toggleSelectAll()"></th>
            <th style="width:44px"></th>
            <th>Kanal</th>
            <th style="width:70px">Ulke</th>
            <th style="width:160px">Grup</th>
            <th style="width:200px">Yeni Grup</th>
            <th style="width:80px">Durum</th>
          </tr>
        </thead>
        <tbody id="list"></tbody>
      </table>
    </div>
  </div>
</div>

<input type="file" id="fileIn" accept=".json" onchange="doImport(event)">
</div>
<div class="toast" id="toast"><span class="t-dot"></span><span id="toastMsg"></span></div>

<script>
const TR_GRPS=["TR ULUSAL","TR SPOR","TR SINEMA","TR SINEMA VOD","TR DIZI","TR 7/24 DIZI","TR BELGESEL","TR COCUK","TR MUZIK","TR HABER","TR DINI","TR YEREL","TR RADYO","TR 4K","TR 8K","TR RAW"];
const DE_GRPS=["DE VOLLPROGRAMM","DE NACHRICHTEN","DE DOKU","DE KINDER","DE FILM","DE MUSIK","DE SPORT","DE SONSTIGE"];
const GRPS=[...TR_GRPS,...DE_GRPS];
const GRP_COLORS={"TR ULUSAL":"#3b82f6","TR SPOR":"#ef4444","TR SINEMA":"#a855f7","TR SINEMA VOD":"#8b5cf6","TR DIZI":"#ec4899","TR 7/24 DIZI":"#f472b6","TR BELGESEL":"#f59e0b","TR COCUK":"#22c55e","TR MUZIK":"#06b6d4","TR HABER":"#eab308","TR DINI":"#10b981","TR YEREL":"#6b7280","TR RADYO":"#14b8a6","TR 4K":"#f97316","TR 8K":"#fb923c","TR RAW":"#ef4444","DE VOLLPROGRAMM":"#3b82f6","DE NACHRICHTEN":"#ef4444","DE DOKU":"#f59e0b","DE KINDER":"#22c55e","DE FILM":"#a855f7","DE MUSIK":"#06b6d4","DE SPORT":"#ef4444","DE SONSTIGE":"#6b7280"};

let channels=[],overrides={},changes={},selected=new Set(),activeGroup='',searchQ='';
let dragChanName=null,dragClone=null,dragStarted=false;

/* Safe HTML escaping for text content */
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
/* Safe JS string literal escaping for inline handlers inside single-quoted attrs */
function escJS(s){return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\n/g,'\\n').replace(/\r/g,'\\r')}

async function load(){
  try{
    const r=await fetch('/api/admin/channels');channels=await r.json();
    const r2=await fetch('/api/admin/overrides');overrides=await r2.json();
    changes={};selected=new Set();
    buildSidebar();fillBulkGroup();render();
  }catch(e){toast('Veri yuklenemedi: '+e,'err')}
}

function buildSidebar(){
  const el=document.getElementById('sbGroups');
  const grpCounts={};channels.forEach(c=>{grpCounts[c.grp]=(grpCounts[c.grp]||0)+1});
  let h='';
  h+='<div class="grp-item '+(activeGroup===''?'active':'')+'" onclick="selectGroup(\'\')"><div class="g-dot" style="background:var(--text2)"></div><div class="g-name">Tumunu Goster</div><div class="g-cnt">'+channels.length+'</div></div>';

  h+='<div class="sb-section"><div class="sb-section-title" onclick="toggleSection(this)"><span class="arrow">&#9660;</span> Turk Kanallari ('+TR_GRPS.filter(g=>grpCounts[g]).length+')</div><div class="sb-section-body">';
  TR_GRPS.forEach(g=>{
    const cnt=grpCounts[g]||0;if(!cnt)return;
    const ovr=Object.keys(overrides).filter(k=>overrides[k]===g).length;
    h+='<div class="grp-item '+(activeGroup===g?'active':'')+'" data-grp="'+esc(g)+'" onclick="selectGroup(\''+escJS(g)+'\')"><div class="g-dot" style="background:'+(GRP_COLORS[g]||'var(--text3)')+'"></div><div class="g-name" title="'+esc(g)+'">'+esc(g.replace('TR ',''))+'</div><div class="g-cnt">'+cnt+(ovr?'<span style="color:var(--yellow);margin-left:3px">+'+ovr+'</span>':'')+'</div></div>';
  });
  h+='</div></div>';

  h+='<div class="sb-section"><div class="sb-section-title" onclick="toggleSection(this)"><span class="arrow">&#9660;</span> Alman Kanallari ('+DE_GRPS.filter(g=>grpCounts[g]).length+')</div><div class="sb-section-body">';
  DE_GRPS.forEach(g=>{
    const cnt=grpCounts[g]||0;if(!cnt)return;
    const ovr=Object.keys(overrides).filter(k=>overrides[k]===g).length;
    h+='<div class="grp-item '+(activeGroup===g?'active':'')+'" data-grp="'+esc(g)+'" onclick="selectGroup(\''+escJS(g)+'\')"><div class="g-dot" style="background:'+(GRP_COLORS[g]||'var(--text3)')+'"></div><div class="g-name" title="'+esc(g)+'">'+esc(g.replace('DE ',''))+'</div><div class="g-cnt">'+cnt+(ovr?'<span style="color:var(--yellow);margin-left:3px">+'+ovr+'</span>':'')+'</div></div>';
  });
  h+='</div></div>';
  el.innerHTML=h;

  /* Attach drag events to all group items */
  el.querySelectorAll('.grp-item[data-grp]').forEach(gi=>{
    gi.addEventListener('dragover',onGrpDragOver);
    gi.addEventListener('dragenter',onGrpDragEnter);
    gi.addEventListener('dragleave',onGrpDragLeave);
    gi.addEventListener('drop',onGrpDrop);
  });
}

function toggleSection(el){el.classList.toggle('collapsed');el.nextElementSibling.classList.toggle('collapsed')}
function selectGroup(g){activeGroup=g;searchQ='';document.getElementById('sideSearch').value='';document.getElementById('mainSearch').value='';selected=new Set();buildSidebar();render()}
function onSideSearch(v){searchQ=v.toUpperCase();activeGroup='';document.getElementById('mainSearch').value='';selected=new Set();buildSidebar();render()}
function onMainSearch(v){searchQ=v.toUpperCase();activeGroup='';selected=new Set();document.getElementById('sideSearch').value='';buildSidebar();render()}
function clearSideSearch(){document.getElementById('sideSearch').value='';searchQ='';buildSidebar();render()}
function clearMainSearch(){document.getElementById('mainSearch').value='';searchQ='';buildSidebar();render()}
function toggleSidebar(){document.getElementById('sidebar').classList.toggle('open');document.getElementById('overlay').classList.toggle('show')}

function fillBulkGroup(){
  const sel=document.getElementById('bulkGroup');
  sel.innerHTML='<option value="">Toplu Tasi...</option>';
  GRPS.forEach(g=>{sel.innerHTML+='<option value="'+g+'">'+g+'</option>'});
}

function getFiltered(){
  return channels.filter(c=>{
    if(activeGroup&&c.grp!==activeGroup)return false;
    if(searchQ&&!c.name.toUpperCase().includes(searchQ))return false;
    return true;
  });
}

function render(){
  const fl=getFiltered();
  const tb=document.getElementById('list');
  document.getElementById('resultCount').textContent=fl.length+' / '+channels.length+' kanal';

  if(!fl.length){tb.innerHTML='<tr><td colspan="7"><div class="empty"><div class="em-icon">&#128250;</div><p>Kanal bulunamadi</p></div></td></tr>';updStats();return}

  let h='';
  fl.forEach(c=>{
    const k=c.name,orig=c.grp;
    const isOvr=overrides.hasOwnProperty(k.toUpperCase());
    const isChg=changes.hasOwnProperty(k);
    const cur=isChg?changes[k]:orig;
    const rowCls=isChg?'changed':'';
    const isSel=selected.has(k);
    const logo=c.logo||'';
    const logoHtml=logo?'<img src="'+esc(logo)+'" onerror="this.parentElement.innerHTML=\'<span class=placeholder>&#9656;</span>\'" loading="lazy">':'<span class="placeholder">&#9656;</span>';

    let statusTag='';
    if(isOvr&&!isChg)statusTag='<span class="tag tag-ovr">&#9998; OVR</span>';
    else if(isChg)statusTag='<span class="tag tag-chg">&#10003; DEGISTI</span>';
    else statusTag='<span class="tag tag-cur">OTO</span>';

    h+='<tr class="'+rowCls+'" draggable="true" data-chname="'+esc(k)+'">';
    h+='<td><input type="checkbox" class="cb ch-cb" data-name="'+esc(k)+'" '+(isSel?'checked':'')+' onchange="toggleSel(this)"></td>';
    h+='<td><div class="ch-logo">'+logoHtml+'</div></td>';
    h+='<td><div class="ch-info"><div class="ch-name">'+esc(k)+'</div><div class="ch-meta">ID: '+c.id+'</div></div></td>';
    h+='<td><span class="badge '+(c.country==='TR'?'b-tr':'b-de')+'">'+c.country+'</span></td>';
    h+='<td style="color:var(--text2);font-size:12px">'+esc(orig)+'</td>';
    h+='<td><select class="gsel" data-chname="'+esc(k)+'">';
    GRPS.forEach(g=>{h+='<option value="'+g+'"'+(cur===g?' selected':'')+'>'+g+'</option>'});
    h+='</select></td>';
    h+='<td>'+statusTag+'</td>';
    h+='</tr>';
  });
  tb.innerHTML=h;
  updStats();

  /* Attach event listeners via delegation for select changes and drag */
  tb.querySelectorAll('.gsel').forEach(sel=>{
    sel.addEventListener('change',function(){chgGrp(this.dataset.chname,this.value)});
  });
  tb.querySelectorAll('tr[draggable]').forEach(tr=>{
    tr.addEventListener('dragstart',onRowDragStart);
    tr.addEventListener('dragend',onRowDragEnd);
  });
}

function toggleSel(cb){
  const name=cb.dataset.name;
  if(cb.checked)selected.add(name);else selected.delete(name);
  syncSelectAll();
}
function toggleSelectAll(){
  const fl=getFiltered();
  const checked=document.getElementById('selectAll').checked;
  selected.clear();
  if(checked)fl.forEach(c=>selected.add(c.name));
  document.getElementById('selectAll2').checked=checked;
  document.querySelectorAll('.ch-cb').forEach(cb=>{cb.checked=checked});
  updBulkBtn();
}
function syncSelectAll(){
  const fl=getFiltered();
  const allSel=fl.length>0&&fl.every(c=>selected.has(c.name));
  document.getElementById('selectAll').checked=allSel;
  document.getElementById('selectAll2').checked=allSel;
  updBulkBtn();
}
function updBulkBtn(){document.getElementById('bulkBtn').disabled=selected.size===0}

/* Fixed: change handler using data attributes + addEventListener instead of broken inline escJ */
function chgGrp(name,val){
  const c=channels.find(x=>x.name===name);if(!c)return;
  if(val===c.grp&&!overrides[name.toUpperCase()])delete changes[name];
  else changes[name]=val;
  render();
}

function bulkMove(){
  const grp=document.getElementById('bulkGroup').value;
  if(!grp||selected.size===0)return;
  selected.forEach(name=>{
    const c=channels.find(x=>x.name===name);
    if(c){
      if(grp===c.grp&&!overrides[name.toUpperCase()])delete changes[name];
      else changes[name]=grp;
    }
  });
  selected=new Set();
  document.getElementById('bulkGroup').value='';
  buildSidebar();render();
  toast(Object.keys(changes).length+' bekleyen degisiklik','ok');
}

function revert(name){delete changes[name];render()}

async function save(){
  const n=Object.keys(changes).length;
  if(!n){toast('Kaydedilecek degisiklik yok');return}
  try{
    const r=await fetch('/api/admin/overrides',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(changes)});
    if(r.ok){
      Object.entries(changes).forEach(([k,v])=>{overrides[k.toUpperCase()]=v});
      /* Update local channel list grp so sidebar counts reflect reality */
      Object.entries(changes).forEach(([k,v])=>{
        const c=channels.find(x=>x.name===k);
        if(c)c.grp=v;
      });
      changes={};selected=new Set();
      buildSidebar();render();
      toast(n+' kanal basariyla kaydedildi!','ok');
    }else toast('Sunucu hatasi!','err');
  }catch(e){toast('Hata: '+e,'err')}
}

async function doExport(){
  try{
    const r=await fetch('/api/admin/overrides/export');
    const blob=await r.blob();const a=document.createElement('a');
    a.href=URL.createObjectURL(blob);a.download='vxparser-overrides.json';a.click();
    toast('JSON dosyasi indirildi','ok');
  }catch(e){toast('Export hatasi','err')}
}

function doImport(ev){
  const f=ev.target.files[0];if(!f)return;
  const rd=new FileReader();
  rd.onload=async(e)=>{
    try{
      const d=JSON.parse(e.target.result);
      const r=await fetch('/api/admin/overrides/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
      if(r.ok){const j=await r.json();toast(j.imported+' override yuklendi','ok');load()}
      else toast('Import hatasi','err');
    }catch(ex){toast('JSON parse hatasi','err')}
  };rd.readAsText(f);ev.target.value='';
}

function copyJson(){
  const t=JSON.stringify(overrides,null,2);
  navigator.clipboard.writeText(t).then(()=>toast('Panoya kopyalandi!','ok')).catch(()=>toast('Kopyalama hatasi','err'));
}

async function doReset(){
  if(!confirm('Tum Manuel Atamalari Silmek Istedi\u011finize Emin Misiniz?'))return;
  try{
    await fetch('/api/admin/overrides',{method:'DELETE'});
    overrides={};changes={};selected=new Set();
    buildSidebar();render();
    toast('Tum atamalar sifirlandi','ok');
  }catch(e){toast('Hata','err')}
}

function updStats(){
  document.getElementById('sTotal').textContent=channels.length;
  const g=new Set(channels.map(c=>c.grp));
  document.getElementById('sGroups').textContent=g.size;
  document.getElementById('sOverride').textContent=Object.keys(overrides).length;
  const nc=Object.keys(changes).length;
  document.getElementById('sChanged').textContent=nc;
  const btn=document.getElementById('saveBtn');
  if(nc===0){btn.disabled=true;btn.style.opacity='.5'}
  else{btn.disabled=false;btn.style.opacity='1'}
}

function toast(msg,type){
  document.getElementById('toastMsg').textContent=msg;
  const t=document.getElementById('toast');
  t.className='toast show '+(type||'');
  setTimeout(()=>t.className='toast',3000);
}

/* ===== DRAG & DROP ===== */

function onRowDragStart(e){
  dragChanName=e.currentTarget.dataset.chname;
  dragStarted=false;
  e.dataTransfer.effectAllowed='move';
  e.dataTransfer.setData('text/plain',dragChanName);
  /* Custom drag image */
  const ch=channels.find(x=>x.name===dragChanName);
  if(ch){
    dragClone=document.createElement('div');
    dragClone.className='ch-drag-clone';
    dragClone.innerHTML='<span class="dc-icon">&#128250;</span><strong>'+esc(ch.name)+'</strong>';
    document.body.appendChild(dragClone);
    e.dataTransfer.setDragImage(dragClone,10,20);
  }
  e.currentTarget.classList.add('ch-dragging');
  document.getElementById('dropHint').classList.add('active');
}

function onRowDragEnd(e){
  e.currentTarget.classList.remove('ch-dragging');
  document.getElementById('dropHint').classList.remove('active');
  if(dragClone){dragClone.remove();dragClone=null}
  /* Clean up all drop-target classes */
  document.querySelectorAll('.grp-item.drop-target,.grp-item.drop-reject').forEach(el=>{
    el.classList.remove('drop-target','drop-reject');
  });
  dragChanName=null;
}

function onGrpDragOver(e){
  e.preventDefault();
  e.dataTransfer.dropEffect='move';
}

function onGrpDragEnter(e){
  e.preventDefault();
  const gi=e.currentTarget;
  if(!gi.dataset.grp)return;
  gi.classList.add('drop-target');
  gi.classList.remove('drop-reject');
}

function onGrpDragLeave(e){
  const gi=e.currentTarget;
  /* Only remove if actually leaving the element (not entering a child) */
  if(!gi.contains(e.relatedTarget)){
    gi.classList.remove('drop-target','drop-reject');
  }
}

function onGrpDrop(e){
  e.preventDefault();
  e.stopPropagation();
  const gi=e.currentTarget;
  const targetGroup=gi.dataset.grp;
  gi.classList.remove('drop-target','drop-reject');
  document.getElementById('dropHint').classList.remove('active');

  if(!dragChanName||!targetGroup)return;

  const c=channels.find(x=>x.name===dragChanName);
  if(!c)return;

  /* Apply the change */
  const curGrp=c.grp;
  if(targetGroup===curGrp&&!overrides[dragChanName.toUpperCase()]){
    toast(c.name+' zaten '+targetGroup+' grubunda','ok');
    return;
  }
  changes[dragChanName]=targetGroup;
  buildSidebar();
  render();
  toast(c.name+' → '+targetGroup+' (kaydetmek icin Kaydet basin)','ok');
}

/* ===== KEYBOARD SHORTCUTS ===== */

document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();save()}
  if(e.key==='Escape'){clearSideSearch();clearMainSearch();selected=new Set();syncSelectAll();render()}
  if(e.key==='/'){const ae=document.activeElement;if(ae.tagName!=='INPUT'&&ae.tagName!=='SELECT'&&ae.tagName!=='TEXTAREA'){e.preventDefault();document.getElementById('mainSearch').focus()}}
});

load();
</script>
</body>
</html>"""
