"""
video.py - VavooBey FastAPI application (OPTIMIZED v2)
Streaming endpoints + Admin Panel

OPTIMIZASYONLAR:
- Global httpx connection pooling (TCP/TLS reuse)
- Cache-Control: no-cache on M3U8 (donma onleme)
- Streaming transfer with aiter_bytes (8KB chunks)
- Client disconnect detection (bant genisligi tasarrufu)
- Retry logic with exponential backoff
- DNS prefetch + connection warm-up
"""
import asyncio
import os
import re
import secrets
import sqlite3
import time as time_module
import urllib.parse
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, PlainTextResponse, HTMLResponse, JSONResponse

import state

# ============================================================
# FASTAPI APP
# ============================================================
app = FastAPI(title="VavooBey", docs_url=None, redoc_url=None)

# Vavoo headers for streaming proxy
VAVOO_STREAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://vavoo.to/",
    "Origin": "https://vavoo.to",
    "Accept": "*/*",
    "Connection": "keep-alive",
}

# ============================================================
# OPTIMIZATION: Global Shared httpx Client (Connection Pooling)
# ============================================================
# EN BUYUK KAZANC: Her istekte yeni client yerine tek shared pool.
# DNS + TCP + TLS handshake tekrar yapilmaz -> ~200-500ms tasarruf per istek.

stream_client: Optional[httpx.AsyncClient] = None


async def get_stream_client() -> httpx.AsyncClient:
    """Get or create the global shared httpx client with connection pooling."""
    global stream_client
    if stream_client is None or stream_client.is_closed:
        stream_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,    # TCP/TLS baglanti: 5s
                read=30.0,      # Segment okuma: 30s (buyuk segmentler icin)
                write=10.0,     # Upload: 10s
                pool=5.0,       # Pool'dan baglanti alma: 5s
            ),
            limits=httpx.Limits(
                max_connections=500,            # Toplam maksimum baglanti
                max_keepalive_connections=100,  # Keep-alive baglanti sayisi
                keepalive_expiry=45,           # Idle keep-alive: 45sn
            ),
            http2=True,                        # HTTP/2 aktif (multiplexing)
            verify=False,
            follow_redirects=True,
            max_redirects=5,
        )
    return stream_client


async def warmup_connections(channel_count: int = 10):
    """
    DNS prefetch + connection warm-up.
    Baslangicta populer kanallar icin on baglanti olustur.
    Bu, ilk kanal acilisini ciddi surede hizlandirir.
    """
    try:
        client = await get_stream_client()
        conn = sqlite3.connect(state.DB_PATH, timeout=5)
        c = conn.cursor()
        c.execute("SELECT url, hls FROM channels WHERE url != '' OR hls != '' LIMIT ?", (channel_count,))
        rows = c.fetchall()
        conn.close()

        tasks = []
        for url, hls in rows:
            target = hls or url
            if target:
                # HEAD request - cok az veri transferi, ama DNS + TCP + TLS olusturur
                tasks.append(asyncio.create_task(
                    client.head(target, headers=VAVOO_STREAM_HEADERS, timeout=5.0)
                ))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if not isinstance(r, Exception))
            print(f"[OPT] Connection warmup: {ok}/{len(tasks)} basarili")
    except Exception as e:
        print(f"[OPT] Warmup hatasi (kritik degil): {e}")

# ============================================================
# SESSION-BASED AUTH (NO dev-mode - always require login)
# ============================================================
ADMIN_SESSIONS = {}  # {session_token: {"expires": timestamp, "username": str}}
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "vavuubey2024"


def get_admin_credentials():
    """Get admin credentials from env vars, fallback to defaults."""
    user = os.environ.get("ADMIN_USER", "")
    pwd = os.environ.get("ADMIN_PASS", "")
    if not user or not pwd:
        return DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASS
    return user, pwd


def verify_admin_credentials(username: str, password: str) -> bool:
    """Verify admin credentials."""
    env_user, env_pass = get_admin_credentials()
    return username == env_user and password == env_pass


async def get_admin_session(request: Request) -> Optional[str]:
    """Get or verify admin session from cookie."""
    session_token = request.cookies.get("vavuubey_session")
    if not session_token:
        return None
    if session_token in ADMIN_SESSIONS:
        session = ADMIN_SESSIONS[session_token]
        if session["expires"] > time_module.time():
            # Refresh session expiry
            session["expires"] = time_module.time() + 86400
            return session_token
        else:
            # Session expired, clean up
            del ADMIN_SESSIONS[session_token]
    return None


async def require_admin(request: Request) -> Optional[str]:
    """Dependency that requires admin session. ALWAYS enforces auth."""
    session = await get_admin_session(request)
    if session:
        return session
    # NO dev-mode fallback - always require authentication
    raise HTTPException(status_code=401, detail="Not authenticated")


# ============================================================
# OPTIMIZATION: Startup Event - Connection Warmup
# ============================================================
@app.on_event("startup")
async def on_startup():
    """Uygulama basladiginda connection warm-up yap."""
    # 3 saniye bekle - server.py'nin startup_sequence tamamlanmasi icin
    await asyncio.sleep(3)
    if state.DATA_READY:
        asyncio.create_task(warmup_connections(15))
        print("[OPT] Warmup baslatildi (DATA_READY=True)")


# ============================================================
# SECURITY HEADERS MIDDLEWARE
# ============================================================
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src * data:; style-src 'unsafe-inline' 'self'; script-src 'unsafe-inline' 'self'"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # Prevent caching of admin pages
    if request.url.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


# ============================================================
# DATABASE HELPER
# ============================================================
def get_db():
    """Get a database connection with row_factory."""
    conn = sqlite3.connect(state.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ============================================================
# XML HELPERS (for EPG)
# ============================================================
def escape_xml(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")


def format_time(ts):
    return time_module.strftime("%Y%m%d%H%M%S +0000", time_module.gmtime(ts))


# ============================================================
# M3U BUILDER
# ============================================================
def build_m3u(host: str) -> str:
    """Build M3U playlist from DB with proper group ordering."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT ch.lid, ch.name, ch.url, ch.hls, ch.logo, ch.grp, ch.sort_order
        FROM channels ch
        LEFT JOIN categories cat ON ch.cid = cat.cid
        ORDER BY COALESCE(cat.sort_order, 9999), ch.sort_order, ch.name
    """)
    channels = [dict(r) for r in c.fetchall()]
    conn.close()

    lines = ['#EXTM3U url-tvg="" deinterlace="1"']
    for ch in channels:
        logo = ch.get("logo", "")
        logo_param = f' tvg-logo="{host}/logo/{ch["lid"]}"' if logo else ""
        grp = ch.get("grp") or ""
        lines.append(f'#EXTINF:-1 group-title="{grp}"{logo_param},{ch["name"]}')
        lines.append(f'{host}/channel/{ch["lid"]}')
    return "\n".join(lines)


# ============================================================
# HLS MANIFEST REWRITER
# ============================================================
def rewrite_m3u8(body: str, host: str, lid: int, resolved_url: str) -> str:
    """Rewrite HLS manifest URLs to go through our proxy."""
    base_url = resolved_url.rsplit("/", 1)[0] if "/" in resolved_url else resolved_url
    lines = body.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            result.append(line)
        elif stripped.startswith("http://") or stripped.startswith("https://"):
            encoded = urllib.parse.quote(stripped, safe='')
            result.append(f"{host}/channel/{lid}/stream?url={encoded}")
        else:
            full = base_url + "/" + stripped
            encoded = urllib.parse.quote(full, safe='')
            result.append(f"{host}/channel/{lid}/stream?url={encoded}")
    return "\n".join(result)


def rewrite_m3u8_relative(body: str, lid: int, base_url: str) -> str:
    """Rewrite HLS manifest URLs for sub-stream proxy (relative paths)."""
    lines = body.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            result.append(line)
        elif stripped.startswith("http://") or stripped.startswith("https://"):
            encoded = urllib.parse.quote(stripped, safe='')
            result.append(f"/channel/{lid}/stream?url={encoded}")
        else:
            full = base_url + "/" + stripped
            encoded = urllib.parse.quote(full, safe='')
            result.append(f"/channel/{lid}/stream?url={encoded}")
    return "\n".join(result)


# ============================================================
# HEALTH / STATUS ENDPOINTS (no auth, always available)
# ============================================================
@app.get("/ping")
async def ping():
    """Health check endpoint. Always returns 200."""
    return JSONResponse(content={
        "status": "ok",
        "data_ready": state.DATA_READY,
        "startup_done": state.STARTUP_DONE,
        "startup_error": state.STARTUP_ERROR,
        "uptime": state.get_uptime(),
    }, status_code=200)


@app.get("/api/status")
async def api_status():
    """Detailed status endpoint."""
    channel_count = state.count_db_channels()
    return JSONResponse(content={
        "status": "ok",
        "data_ready": state.DATA_READY,
        "startup_done": state.STARTUP_DONE,
        "startup_error": state.STARTUP_ERROR,
        "uptime": state.get_uptime(),
        "load_time": round(state.LOAD_TIME, 2),
        "channel_count": channel_count,
        "last_refresh": state.LAST_REFRESH,
        "logs": state.STARTUP_LOGS[-50:],
    }, status_code=200)


# ============================================================
# STREAMING ENDPOINTS
# ============================================================
@app.get("/")
async def index():
    if not state.DATA_READY:
        return PlainTextResponse("VavooBey is starting up... /ping for health check", status_code=503)
    return PlainTextResponse("VavooBey is running. /admin for management.", media_type="text/plain")


@app.get("/robots.txt")
async def robots_txt():
    return PlainTextResponse("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/get.php")
async def get_m3u(request: Request):
    if not state.DATA_READY:
        return PlainTextResponse("#EXTM3U\n# VavooBey is starting...", status_code=503)
    host = str(request.base_url).rstrip("/")
    m3u = build_m3u(host)
    return PlainTextResponse(m3u, media_type="application/x-mpegurl")


@app.get("/player_api.php")
async def player_api(request: Request, action: str = Query("")):
    if not state.DATA_READY:
        return PlainTextResponse("VavooBey is starting up...", status_code=503)
    host = str(request.base_url).rstrip("/")
    if action == "get_live_streams":
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT ch.lid, ch.name, ch.grp, ch.logo, ch.sort_order
            FROM channels ch
            LEFT JOIN categories cat ON ch.cid = cat.cid
            ORDER BY COALESCE(cat.sort_order, 9999), ch.sort_order, ch.name
        """)
        streams = []
        for r in c.fetchall():
            streams.append({
                "num": r["lid"],
                "name": r["name"],
                "group": r["grp"] or "",
                "logo": r["logo"] or "",
                "stream_type": "live",
                "stream_id": r["lid"],
            })
        conn.close()
        return {"streams": streams}
    elif action == "get_live_categories":
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT cid, name, sort_order FROM categories ORDER BY sort_order")
        cats = []
        for r in c.fetchall():
            cats.append({"category_id": r["cid"], "category_name": r["name"], "parent_id": 0})
        conn.close()
        return {"categories": cats}
    elif action == "":
        return PlainTextResponse(build_m3u(host), media_type="application/x-mpegurl")
    return PlainTextResponse("Unknown action", status_code=400, media_type="text/plain")


@app.get("/channel/{lid}")
async def channel_stream(lid: int, request: Request):
    """Proxy channel stream with HLS rewriting.
    OPTIMIZED: Global client, retry logic, Cache-Control, streaming transfer."""
    if not state.DATA_READY:
        return PlainTextResponse("VavooBey is starting up...", status_code=503)

    resolved_url, log_msg = await state.resolve_channel(lid)
    if not resolved_url:
        return PlainTextResponse(f"Channel {lid} not resolvable: {log_msg}", status_code=404)

    host = str(request.base_url).rstrip("/")

    # OPTIMIZATION: Retry logic (2 deneme, exponential backoff)
    last_error = None
    for attempt in range(2):
        try:
            client = await get_stream_client()
            r = await client.get(resolved_url, headers=VAVOO_STREAM_HEADERS)

            content_type = r.headers.get("content-type", "")

            if "mpegURL" in content_type or "x-mpegurl" in content_type or "m3u8" in content_type or ".m3u8" in resolved_url:
                body = r.text
                body = rewrite_m3u8(body, host, lid, resolved_url)
                # OPTIMIZATION: Cache-Control: no-cache ZORUNLU!
                # M3U8 playlist asla cache'lenmemeli - donma nedeni #1
                return PlainTextResponse(
                    body,
                    media_type="application/vnd.apple.mpegurl",
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                    }
                )
            else:
                content = r.content
                ct = content_type or "video/mp4"
                return StreamingResponse(
                    iter([content]),
                    media_type=ct,
                    headers={"Content-Length": str(len(content))}
                )
        except httpx.TimeoutException as e:
            last_error = e
            if attempt == 0:
                await asyncio.sleep(0.5)  # 500ms bekle, tekrar dene
        except Exception as e:
            last_error = e
            break

    return PlainTextResponse(f"Stream error: {last_error}", status_code=502)


@app.get("/channel/{lid}/stream")
async def channel_sub_stream(lid: int, url: str = Query(""), request: Request = None):
    """Proxy sub-stream (HLS segments, playlists).
    OPTIMIZED: Global client, streaming transfer, disconnect detection, retry."""
    if not url:
        return PlainTextResponse("No URL provided", status_code=400, media_type="text/plain")

    # OPTIMIZATION: Retry logic (2 deneme)
    last_error = None
    for attempt in range(2):
        try:
            client = await get_stream_client()

            content_type_guess = ""
            if ".m3u8" in url:
                content_type_guess = "mpegURL"

            # OPTIMIZATION: Streaming transfer (aiter_bytes ile parca parca gonder)
            # Segment boyutu bilinmedigi icin Content-Length yok, chunked transfer.
            if not content_type_guess:
                # Segment (.ts, .mp4) - STREAMING transfer
                async def stream_segment():
                    try:
                        async with client.stream("GET", url, headers=VAVOO_STREAM_HEADERS) as resp:
                            resp.raise_for_status()
                            async for chunk in resp.aiter_bytes(chunk_size=8192):
                                # OPTIMIZATION: Client hala bagli mi kontrol et
                                if request and await request.is_disconnected():
                                    break
                                yield chunk
                    except Exception:
                        pass

                ct = "video/MP2T"
                return StreamingResponse(
                    stream_segment(),
                    media_type=ct,
                    headers={
                        "Cache-Control": "public, max-age=3600",  # Segment cache'lenebilir
                        "Transfer-Encoding": "chunked",
                    }
                )
            else:
                # M3U8 playlist - normal fetch + rewrite
                r = await client.get(url, headers=VAVOO_STREAM_HEADERS)
                content_type = r.headers.get("content-type", "")

                if "mpegURL" in content_type or "x-mpegurl" in content_type or ".m3u8" in url:
                    body = r.text
                    base_url = url.rsplit("/", 1)[0] if "/" in url else url
                    body = rewrite_m3u8_relative(body, lid, base_url)
                    # OPTIMIZATION: Cache-Control: no-cache ZORUNLU!
                    return PlainTextResponse(
                        body,
                        media_type="application/vnd.apple.mpegurl",
                        headers={
                            "Cache-Control": "no-cache, no-store, must-revalidate",
                            "Pragma": "no-cache",
                            "Expires": "0",
                        }
                    )
                else:
                    # M3U8 degil ama streaming ile gonder
                    async def stream_content():
                        try:
                            async with client.stream("GET", url, headers=VAVOO_STREAM_HEADERS) as resp:
                                resp.raise_for_status()
                                async for chunk in resp.aiter_bytes(chunk_size=8192):
                                    if request and await request.is_disconnected():
                                        break
                                    yield chunk
                        except Exception:
                            pass

                    ct = content_type or "video/MP2T"
                    return StreamingResponse(
                        stream_content(),
                        media_type=ct,
                        headers={"Cache-Control": "public, max-age=3600"}
                    )
        except httpx.TimeoutException as e:
            last_error = e
            if attempt == 0:
                await asyncio.sleep(0.5)
        except Exception as e:
            last_error = e
            break

    return PlainTextResponse(f"Stream error: {last_error}", status_code=502)


# ============================================================
# EPG ENDPOINT (XMLTV format)
# ============================================================
@app.get("/epg.xml")
async def get_epg():
    """Generate EPG in XMLTV format with channel info and current programme."""
    if not state.DATA_READY:
        return PlainTextResponse("<!-- EPG not ready -->", status_code=503)

    conn = get_db()
    c = conn.cursor()
    # Get channels grouped by categories
    c.execute("""
        SELECT ch.lid, ch.name, ch.grp, ch.logo, ch.cid,
               COALESCE(cat.sort_order, 9999) as cat_order
        FROM channels ch
        LEFT JOIN categories cat ON ch.cid = cat.cid
        ORDER BY cat_order, ch.sort_order, ch.name
    """)
    channels = c.fetchall()
    conn.close()

    if not channels:
        return PlainTextResponse('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="VavooBey"/>\n', media_type="application/xml")

    now = int(time_module.time())
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<tv generator-info-name="VavooBey" source-info-name="VavooBey IPTV">')

    # Channel definitions
    for ch in channels:
        ch_id = str(ch["lid"])
        lines.append(f'  <channel id="{ch_id}">')
        lines.append(f'    <display-name lang="tr">{escape_xml(ch["name"])}</display-name>')
        if ch["grp"]:
            lines.append(f'    <display-name lang="tr">{escape_xml(ch["grp"])}</display-name>')
        if ch["logo"]:
            lines.append(f'    <icon src="{escape_xml(ch["logo"])}"/>')
        lines.append(f'  </channel>')

    # Programme entries - 24h "Yayinda" for each channel
    for ch in channels:
        ch_id = str(ch["lid"])
        start = format_time(now - 3600)
        stop = format_time(now + 82800)
        lines.append(f'  <programme start="{start}" stop="{stop}" channel="{ch_id}">')
        lines.append(f'    <title lang="tr">Canli Yayin</title>')
        lines.append(f'    <desc lang="tr">{escape_xml(ch["name"])} - Canli Yayin</desc>')
        lines.append(f'  </programme>')

    lines.append('</tv>')
    return PlainTextResponse("\n".join(lines), media_type="application/xml")


# ============================================================
# LOGO PROXY ENDPOINT
# ============================================================
@app.get("/logo/{lid}")
async def proxy_logo(lid: int):
    """Proxy channel logos. Fetches from Vavoo or DB and caches."""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT logo FROM channels WHERE lid=?", (lid,))
        row = c.fetchone()
        conn.close()

        if not row or not row["logo"]:
            return PlainTextResponse("Not found", status_code=404)

        logo_url = row["logo"]

        # Validate URL scheme
        if not logo_url.startswith("http://") and not logo_url.startswith("https://"):
            return PlainTextResponse("Invalid logo URL", status_code=400)

        client = await get_stream_client()
        r = await client.get(logo_url, headers=VAVOO_STREAM_HEADERS)
        if r.status_code != 200:
            return PlainTextResponse("Logo fetch failed", status_code=502)
        ct = r.headers.get("content-type", "image/png")
        if "image" not in ct and "octet-stream" not in ct:
            ct = "image/png"
        # Logo 24 saat cache'lenebilir
        return Response(content=r.content, media_type=ct, headers={
            "Cache-Control": "public, max-age=86400",
        })
    except httpx.TimeoutException:
        return PlainTextResponse("Logo timeout", status_code=504)
    except Exception:
        return PlainTextResponse("Logo fetch failed", status_code=502)


# ============================================================
# ADMIN PANEL HTML
# ============================================================
ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<title>VavooBey Admin</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f1117;--sidebar:#1a1d27;--card:#1e2233;--primary:#4f8cff;
  --success:#22c55e;--warning:#f59e0b;--danger:#ef4444;
  --text:#e2e8f0;--muted:#94a3b8;--border:#2d3348;
  --hover:#252a3a;--input-bg:#151822;
}
html,body{height:100%;font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);overflow:hidden}
.app{display:flex;height:100vh}

/* Login overlay */
.login-overlay{position:fixed;inset:0;background:var(--bg);z-index:500;display:flex;align-items:center;justify-content:center}
.login-overlay.hidden{display:none}
.login-box{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:40px;width:380px;max-width:90vw;text-align:center}
.login-box h2{font-size:22px;font-weight:700;margin-bottom:6px}
.login-box .subtitle{font-size:13px;color:var(--muted);margin-bottom:28px}
.login-box input{width:100%;background:var(--input-bg);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:8px;font-size:14px;outline:none;transition:border-color .2s;margin-bottom:12px}
.login-box input:focus{border-color:var(--primary)}
.login-box button{width:100%;background:var(--primary);color:#fff;border:none;padding:11px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .2s;margin-top:4px}
.login-box button:hover{opacity:.85}
.login-box button:disabled{opacity:.5;cursor:not-allowed}
.login-error{color:var(--danger);font-size:13px;margin-top:10px;min-height:20px}

/* Sidebar */
.sidebar{width:280px;min-width:280px;background:var(--sidebar);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.sidebar-header{padding:20px 16px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.sidebar-header h1{font-size:18px;font-weight:700;display:flex;align-items:center;gap:8px}
.sidebar-header h1 .dot{width:8px;height:8px;border-radius:50%;background:var(--success);display:inline-block}
.sidebar-header .subtitle{font-size:11px;color:var(--muted);margin-top:4px}
.logout-btn{background:transparent;border:1px solid var(--border);color:var(--muted);padding:4px 10px;border-radius:6px;font-size:11px;cursor:pointer;transition:all .15s}
.logout-btn:hover{color:var(--danger);border-color:var(--danger)}
.group-list{flex:1;overflow-y:auto;padding:8px}
.group-list::-webkit-scrollbar{width:4px}
.group-list::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
.group-item{display:flex;align-items:center;padding:9px 12px;border-radius:8px;cursor:pointer;transition:background .15s;gap:8px;margin-bottom:2px;position:relative;user-select:none}
.group-item:hover{background:var(--hover)}
.group-item.active{background:var(--primary);color:#fff}
.group-item.active .g-count{background:rgba(255,255,255,.2);color:#fff}
.group-item .g-icon{font-size:14px;opacity:.7;width:20px;text-align:center;flex-shrink:0}
.group-item .g-name{flex:1;font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.group-item .g-count{font-size:11px;background:var(--border);color:var(--muted);padding:1px 7px;border-radius:10px;flex-shrink:0}
.group-item .g-actions{display:none;gap:2px;flex-shrink:0}
.group-item:hover .g-actions{display:flex}
.g-action-btn{background:none;border:none;color:var(--muted);cursor:pointer;padding:2px 5px;border-radius:4px;font-size:12px;transition:all .15s}
.g-action-btn:hover{color:var(--text);background:rgba(255,255,255,.1)}
.g-action-btn.delete:hover{color:var(--danger)}
.sidebar-footer{padding:12px;border-top:1px solid var(--border)}
.add-group-form{display:flex;gap:6px}
.add-group-form input{flex:1;background:var(--input-bg);border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:6px;font-size:12px;outline:none;transition:border-color .2s}
.add-group-form input:focus{border-color:var(--primary)}
.add-group-form button{background:var(--primary);color:#fff;border:none;padding:7px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;transition:opacity .2s;white-space:nowrap}
.add-group-form button:hover{opacity:.85}

/* Rename input */
.rename-input{background:var(--input-bg);border:1px solid var(--primary);color:var(--text);padding:2px 6px;border-radius:4px;font-size:13px;outline:none;width:100%}

/* Main content */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.toolbar{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-shrink:0;flex-wrap:wrap}
.toolbar .title-area{flex:1;min-width:200px}
.toolbar .title-area h2{font-size:16px;font-weight:600}
.toolbar .title-area .channel-count{font-size:12px;color:var(--muted);margin-top:2px}
.search-box{position:relative;width:260px}
.search-box input{width:100%;background:var(--input-bg);border:1px solid var(--border);color:var(--text);padding:8px 12px 8px 34px;border-radius:8px;font-size:13px;outline:none;transition:border-color .2s}
.search-box input:focus{border-color:var(--primary)}
.search-box svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--muted)}
.bulk-bar{display:none;align-items:center;gap:10px;padding:8px 20px;background:rgba(79,140,255,.08);border-bottom:1px solid rgba(79,140,255,.2);flex-shrink:0}
.bulk-bar.visible{display:flex}
.bulk-bar .sel-count{font-size:13px;color:var(--primary);font-weight:600;white-space:nowrap}
.bulk-bar select{background:var(--input-bg);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:6px;font-size:12px;outline:none;cursor:pointer}
.bulk-bar .btn{padding:6px 14px;border-radius:6px;border:1px solid var(--border);background:var(--card);color:var(--text);font-size:12px;cursor:pointer;transition:all .15s;font-weight:500}
.bulk-bar .btn:hover{background:var(--hover)}
.bulk-bar .btn.btn-primary{background:var(--primary);border-color:var(--primary);color:#fff}
.bulk-bar .btn.btn-danger{color:var(--danger);border-color:rgba(239,68,68,.3)}
.bulk-bar .btn.btn-danger:hover{background:rgba(239,68,68,.1)}

/* Table */
.table-wrap{flex:1;overflow-y:auto;padding:0}
.table-wrap::-webkit-scrollbar{width:6px}
.table-wrap::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
table{width:100%;border-collapse:collapse}
thead{position:sticky;top:0;z-index:10}
thead th{background:var(--sidebar);padding:10px 14px;text-align:left;font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);white-space:nowrap}
thead th:first-child{width:40px;text-align:center}
thead th:nth-child(3){width:50px}
tbody tr{border-bottom:1px solid var(--border);transition:background .1s;cursor:default}
tbody tr:hover{background:var(--hover)}
tbody tr.dragging{opacity:.4}
tbody tr.drag-over{border-top:2px solid var(--primary)}
tbody td{padding:8px 14px;font-size:13px;vertical-align:middle}
tbody td:first-child{text-align:center}
.ch-check{width:16px;height:16px;accent-color:var(--primary);cursor:pointer}
.ch-logo{width:36px;height:36px;border-radius:6px;object-fit:contain;background:var(--input-bg);border:1px solid var(--border)}
.ch-name{font-weight:500;max-width:300px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.grp-select{background:var(--input-bg);border:1px solid var(--border);color:var(--text);padding:5px 8px;border-radius:6px;font-size:12px;outline:none;cursor:pointer;min-width:160px;transition:border-color .2s}
.grp-select:focus{border-color:var(--primary)}
.order-btns{display:flex;gap:4px}
.order-btn{background:var(--card);border:1px solid var(--border);color:var(--muted);width:24px;height:24px;border-radius:4px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:10px;transition:all .15s}
.order-btn:hover{color:var(--text);border-color:var(--primary);background:var(--hover)}
.order-btn:disabled{opacity:.3;cursor:not-allowed}

.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px 20px;color:var(--muted)}
.empty-state svg{margin-bottom:12px;opacity:.5}
.empty-state p{font-size:14px}

/* Loading overlay */
.loading-overlay{position:fixed;inset:0;background:rgba(15,17,23,.6);display:none;align-items:center;justify-content:center;z-index:100}
.loading-overlay.active{display:flex}
.spinner{width:32px;height:32px;border:3px solid var(--border);border-top-color:var(--primary);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* Toast */
.toast-container{position:fixed;bottom:20px;right:20px;z-index:200;display:flex;flex-direction:column-reverse;gap:8px}
.toast{padding:12px 18px;border-radius:8px;font-size:13px;font-weight:500;color:#fff;animation:slideIn .3s ease;box-shadow:0 4px 20px rgba(0,0,0,.4);max-width:360px;word-break:break-word}
.toast.success{background:#16a34a}
.toast.error{background:#dc2626}
.toast.info{background:#2563eb}
.toast.removing{animation:slideOut .3s ease forwards}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
@keyframes slideOut{from{transform:translateX(0);opacity:1}to{transform:translateX(100%);opacity:0}}

/* Responsive */
@media(max-width:768px){
  .sidebar{width:220px;min-width:220px}
  .toolbar{padding:12px 14px}
  .search-box{width:180px}
}
@media(max-width:600px){
  .sidebar{display:none}
}
</style>
</head>
<body>

<!-- Login Overlay (ALWAYS shown first, hidden after successful auth) -->
<div class="login-overlay" id="loginOverlay">
  <div class="login-box">
    <h2>VavooBey</h2>
    <div class="subtitle">IPTV Kanal Yoneticisi</div>
    <input type="text" id="loginUser" placeholder="Kullanici Adi" autocomplete="username">
    <input type="password" id="loginPass" placeholder="Sifre" autocomplete="current-password">
    <button id="loginBtn" onclick="doLogin()">Giris Yap</button>
    <div class="login-error" id="loginError"></div>
  </div>
</div>

<div class="app" id="mainApp" style="display:none">
  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-header">
      <div>
        <h1><span class="dot" id="statusDot"></span> VavooBey</h1>
        <div class="subtitle">IPTV Kanal Yoneticisi</div>
      </div>
      <button class="logout-btn" onclick="doLogout()">Cikis</button>
    </div>
    <div class="group-list" id="groupList"></div>
    <div class="sidebar-footer">
      <div class="add-group-form">
        <input type="text" id="newGroupName" placeholder="Yeni grup adi..." maxlength="50">
        <button onclick="createGroup()">+ Ekle</button>
      </div>
    </div>
  </aside>

  <!-- Main -->
  <div class="main">
    <div class="toolbar">
      <div class="title-area">
        <h2 id="currentTitle">Tum Kanallar</h2>
        <div class="channel-count" id="channelCount">0 kanal</div>
      </div>
      <div class="search-box">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input type="text" id="searchInput" placeholder="Kanal ara..." oninput="debounceSearch()">
      </div>
    </div>
    <div class="bulk-bar" id="bulkBar">
      <span class="sel-count" id="selCount">0 secili</span>
      <select id="bulkGroupSelect"><option value="">Gruba tasi...</option></select>
      <button class="btn btn-primary" onclick="bulkAssign()">Ata</button>
      <button class="btn btn-danger" onclick="bulkUngroup()">Gruptan Cikar</button>
      <button class="btn" onclick="clearSelection()">Iptal</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th><input type="checkbox" class="ch-check" id="selectAll" onchange="toggleSelectAll(this)"></th>
            <th>Kanal</th>
            <th></th>
            <th>Grup</th>
            <th style="width:70px">Sira</th>
          </tr>
        </thead>
        <tbody id="channelBody"></tbody>
      </table>
      <div class="empty-state" id="emptyState" style="display:none">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/></svg>
        <p>Kanal bulunamadi</p>
      </div>
    </div>
  </div>
</div>

<div class="loading-overlay" id="loadingOverlay"><div class="spinner"></div></div>
<div class="toast-container" id="toastContainer"></div>

<script>
let groups = [];
let channels = [];
let selectedGroup = null;
let selectedLids = new Set();
let searchTimer = null;
let allGroupsCache = [];
let isRendering = false;
let isLoggedIn = false;
let authToken = null;

// ====== UTILITY ======
function escHtml(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}
function escAttr(s) {
  return (s || "").replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/'/g,"&#39;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// ====== AUTH (ALWAYS enforced) ======
async function checkAuth() {
  try {
    const r = await fetch("/admin/api/auth-check");
    if (r.ok) {
      const data = await r.json();
      if (data.authenticated) {
        isLoggedIn = true;
        authToken = data.token || null;
        document.getElementById("loginOverlay").classList.add("hidden");
        document.getElementById("mainApp").style.display = "flex";
        return true;
      }
    }
  } catch(e) {}
  // Not authenticated - ALWAYS show login
  isLoggedIn = false;
  document.getElementById("loginOverlay").classList.remove("hidden");
  document.getElementById("mainApp").style.display = "none";
  return false;
}

async function doLogin() {
  const user = document.getElementById("loginUser").value.trim();
  const pass = document.getElementById("loginPass").value;
  const errEl = document.getElementById("loginError");
  const btn = document.getElementById("loginBtn");
  if (!user || !pass) { errEl.textContent = "Kullanici adi ve sifre gerekli"; return; }
  errEl.textContent = "";
  btn.disabled = true;
  btn.textContent = "Giris yapiliyor...";
  try {
    const r = await fetch("/admin/api/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username: user, password: pass})
    });
    if (r.ok) {
      isLoggedIn = true;
      document.getElementById("loginOverlay").classList.add("hidden");
      document.getElementById("mainApp").style.display = "flex";
      await init();
    } else {
      const data = await r.json().catch(() => ({}));
      errEl.textContent = data.detail || "Giris basarisiz";
    }
  } catch(e) {
    errEl.textContent = "Baglanti hatasi";
  } finally {
    btn.disabled = false;
    btn.textContent = "Giris Yap";
  }
}

async function doLogout() {
  // 1. Call server logout endpoint
  try {
    await fetch("/admin/api/logout", {method: "POST", credentials: "include"});
  } catch(e) {}
  // 2. Reset local state
  isLoggedIn = false;
  authToken = null;
  // 3. Clear all local data
  groups = [];
  channels = [];
  selectedGroup = null;
  selectedLids.clear();
  // 4. Show login, hide app
  document.getElementById("loginOverlay").classList.remove("hidden");
  document.getElementById("mainApp").style.display = "none";
  // 5. Clear form fields
  document.getElementById("loginUser").value = "";
  document.getElementById("loginPass").value = "";
  document.getElementById("loginError").textContent = "";
}

// Enter key on login form
document.getElementById("loginPass").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
document.getElementById("loginUser").addEventListener("keydown", e => { if (e.key === "Enter") document.getElementById("loginPass").focus(); });

// ====== API HELPERS ======
async function api(method, url, body) {
  const opts = { method, headers: {"Content-Type": "application/json"}, credentials: "include" };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(url, opts);
  if (r.status === 401) {
    // Session expired - force re-login
    isLoggedIn = false;
    authToken = null;
    document.getElementById("loginOverlay").classList.remove("hidden");
    document.getElementById("mainApp").style.display = "none";
    throw new Error("Oturum suresi doldu, tekrar giris yapin");
  }
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || "Request failed");
  }
  return r.json();
}

// ====== TOAST ======
function toast(msg, type = "info") {
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  document.getElementById("toastContainer").appendChild(el);
  setTimeout(() => {
    el.classList.add("removing");
    setTimeout(() => el.remove(), 300);
  }, 3000);
}

// ====== LOADING ======
let loadingCount = 0;
function showLoading() {
  loadingCount++;
  document.getElementById("loadingOverlay").classList.add("active");
}
function hideLoading() {
  loadingCount--;
  if (loadingCount <= 0) { loadingCount = 0; document.getElementById("loadingOverlay").classList.remove("active"); }
}

// ====== GROUPS ======
async function loadGroups() {
  groups = await api("GET", "/admin/api/groups");
  allGroupsCache = groups;
  renderSidebar();
  populateGroupDropdowns();
}

function renderSidebar() {
  const list = document.getElementById("groupList");
  let html = '<div class="group-item ' + (selectedGroup === null ? 'active' : '') + '" onclick="selectGroup(null)"><span class="g-icon">&#9776;</span><span class="g-name">Tum Kanallar</span><span class="g-count">' + groups.reduce((s,g) => s + g.count, 0) + '</span></div>';
  groups.forEach((g, idx) => {
    html += '<div class="group-item ' + (selectedGroup === g.cid ? 'active' : '') + '" onclick="selectGroup(' + g.cid + ')" data-cid="' + g.cid + '"><span class="g-icon">&#127909;</span><span class="g-name" ondblclick="startRename(event, ' + g.cid + ')" title="' + escAttr(g.name) + '">' + escHtml(g.name) + '</span><span class="g-count">' + g.count + '</span><span class="g-actions"><button class="g-action-btn" onclick="event.stopPropagation();reorderGroup(' + g.cid + ',\'up\')" title="Yukari">&#9650;</button><button class="g-action-btn" onclick="event.stopPropagation();reorderGroup(' + g.cid + ',\'down\')" title="Asagi">&#9660;</button><button class="g-action-btn" onclick="event.stopPropagation();startRename(event,' + g.cid + ')" title="Yeniden Adlandir">&#9998;</button><button class="g-action-btn delete" onclick="event.stopPropagation();deleteGroup(' + g.cid + ',\'' + escAttr(g.name).replace(/'/g, "\\'") + '\')" title="Sil">&#128465;</button></span></div>';
  });
  list.innerHTML = html;
}

function populateGroupDropdowns() {
  let opts = '<option value="">Gruba tasi...</option>';
  opts += '<option value="0">Grupsuz</option>';
  groups.forEach(g => { opts += '<option value="' + g.cid + '">' + escHtml(g.name) + '</option>'; });
  document.getElementById("bulkGroupSelect").innerHTML = opts;
}

async function selectGroup(cid) {
  selectedGroup = cid;
  selectedLids.clear();
  updateBulkBar();
  document.getElementById("selectAll").checked = false;
  renderSidebar();
  document.getElementById("currentTitle").textContent = cid === null ? "Tum Kanallar" : (groups.find(x => x.cid === cid) || {}).name || "Grup";
  await loadChannels();
}

async function createGroup() {
  const input = document.getElementById("newGroupName");
  const name = input.value.trim();
  if (!name) { toast("Grup adi girin", "error"); return; }
  showLoading();
  try {
    await api("POST", "/admin/api/groups/create", { name });
    input.value = "";
    toast('"' + name + '" grubu olusturuldu', "success");
    await loadGroups();
    await loadChannels();
  } catch (e) { toast(e.message, "error"); }
  finally { hideLoading(); }
}

function startRename(e, cid) {
  e.stopPropagation();
  const item = e.target.closest(".group-item") || e.target.closest('[data-cid]');
  if (!item) return;
  const nameEl = item.querySelector(".g-name");
  const oldName = nameEl.textContent;
  nameEl.innerHTML = '<input class="rename-input" type="text" value="' + escAttr(oldName) + '" maxlength="50">';
  const inp = nameEl.querySelector("input");
  inp.focus();
  inp.select();
  const finish = async () => {
    const newName = inp.value.trim();
    if (newName && newName !== oldName) {
      showLoading();
      try {
        await api("POST", "/admin/api/groups/rename", { cid, name: newName });
        toast('"' + newName + '" olarak yeniden adlandirildi', "success");
        await loadGroups();
        await loadChannels();
      } catch (err) { toast(err.message, "error"); }
      finally { hideLoading(); }
    } else { renderSidebar(); }
  };
  inp.addEventListener("blur", finish);
  inp.addEventListener("keydown", ev => { if (ev.key === "Enter") inp.blur(); if (ev.key === "Escape") { inp.value = oldName; inp.blur(); } });
}

async function deleteGroup(cid, name) {
  if (!confirm('"' + name + '" grubunu silmek istediginize emin misiniz?\nKanallar Grupsuz\'a tasinacaktir.')) return;
  showLoading();
  try {
    await api("POST", "/admin/api/groups/delete", { cid });
    toast('"' + name + '" grubu silindi', "success");
    if (selectedGroup === cid) selectedGroup = null;
    await loadGroups();
    await loadChannels();
  } catch (e) { toast(e.message, "error"); }
  finally { hideLoading(); }
}

async function reorderGroup(cid, direction) {
  showLoading();
  try {
    const result = await api("POST", "/admin/api/groups/reorder", { cid, direction });
    if (result.swapped) {
      toast("Grup siralama degistirildi", "success");
    }
    await loadGroups();
  } catch (e) { toast(e.message, "error"); }
  finally { hideLoading(); }
}

// ====== CHANNELS ======
async function loadChannels() {
  const search = document.getElementById("searchInput").value.trim();
  let url = "/admin/api/channels?";
  if (selectedGroup !== null) url += "group_id=" + selectedGroup + "&";
  if (search) url += "search=" + encodeURIComponent(search);
  channels = await api("GET", url);
  renderChannels();
}

function renderChannels() {
  isRendering = true;
  const body = document.getElementById("channelBody");
  const empty = document.getElementById("emptyState");
  const countEl = document.getElementById("channelCount");
  countEl.textContent = channels.length + " kanal";
  if (channels.length === 0) {
    body.innerHTML = "";
    empty.style.display = "flex";
    isRendering = false;
    return;
  }
  empty.style.display = "none";
  let html = "";
  channels.forEach((ch, idx) => {
    const checked = selectedLids.has(ch.lid) ? "checked" : "";
    const logoSrc = ch.logo ? '/logo/' + ch.lid : '';
    const logo = ch.logo ? '<img class="ch-logo" src="' + logoSrc + '" alt="" onerror="this.style.display=\'none\'">' : '<div class="ch-logo" style="display:flex;align-items:center;justify-content:center;font-size:14px;color:var(--muted)">&#127909;</div>';
    let grpOpts = '<option value="0"' + (ch.cid == 0 ? ' selected' : '') + '>Grupsuz</option>';
    groups.forEach(g => {
      grpOpts += '<option value="' + g.cid + '"' + (ch.cid === g.cid ? ' selected' : '') + '>' + escHtml(g.name) + '</option>';
    });
    html += '<tr draggable="true" data-lid="' + ch.lid + '" ondragstart="onDragStart(event)" ondragover="onDragOver(event)" ondragleave="onDragLeave(event)" ondrop="onDrop(event)" ondragend="onDragEnd(event)"><td><input type="checkbox" class="ch-check" ' + checked + ' onchange="toggleChannel(' + ch.lid + ', this.checked)"></td><td><span class="ch-name" title="' + escAttr(ch.name) + '">' + escHtml(ch.name) + '</span></td><td>' + logo + '</td><td><select class="grp-select" data-original="' + ch.cid + '" onchange="changeGroup(' + ch.lid + ', this.value, this)">' + grpOpts + '</select></td><td><div class="order-btns"><button class="order-btn" onclick="reorderChannel(' + ch.lid + ',\'up\',' + idx + ')"' + (idx === 0 ? ' disabled' : '') + '>&#9650;</button><button class="order-btn" onclick="reorderChannel(' + ch.lid + ',\'down\',' + idx + ')"' + (idx === channels.length - 1 ? ' disabled' : '') + '>&#9660;</button></div></td></tr>';
  });
  body.innerHTML = html;
  setTimeout(() => { isRendering = false; }, 50);
}

// ====== DRAG & DROP ======
let dragSrcLid = null;
function onDragStart(e) {
  dragSrcLid = parseInt(e.currentTarget.dataset.lid);
  e.currentTarget.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
}
function onDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  e.currentTarget.classList.add("drag-over");
}
function onDragLeave(e) { e.currentTarget.classList.remove("drag-over"); }
function onDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove("drag-over");
  const targetLid = parseInt(e.currentTarget.dataset.lid);
  if (dragSrcLid && targetLid && dragSrcLid !== targetLid) {
    moveChannel(dragSrcLid, targetLid);
  }
}
function onDragEnd(e) {
  e.currentTarget.classList.remove("dragging");
  document.querySelectorAll(".drag-over").forEach(el => el.classList.remove("drag-over"));
}

async function moveChannel(srcLid, targetLid) {
  try {
    await api("POST", "/admin/api/channels/move", { lid: srcLid, target_lid: targetLid });
    toast("Kanal tasindi", "success");
    await loadChannels();
  } catch (e) { toast(e.message, "error"); }
}

// ====== CHANNEL OPERATIONS ======
function toggleChannel(lid, checked) {
  if (checked) selectedLids.add(lid); else selectedLids.delete(lid);
  updateBulkBar();
}

function toggleSelectAll(el) {
  const boxes = document.querySelectorAll("#channelBody .ch-check");
  if (el.checked) {
    channels.forEach(ch => selectedLids.add(ch.lid));
    boxes.forEach(b => b.checked = true);
  } else {
    selectedLids.clear();
    boxes.forEach(b => b.checked = false);
  }
  updateBulkBar();
}

function updateBulkBar() {
  const bar = document.getElementById("bulkBar");
  const countEl = document.getElementById("selCount");
  const n = selectedLids.size;
  if (n > 0) {
    bar.classList.add("visible");
    countEl.textContent = n + " secili";
  } else {
    bar.classList.remove("visible");
  }
}

async function bulkAssign() {
  const cid = parseInt(document.getElementById("bulkGroupSelect").value);
  if (!cid && cid !== 0) { toast("Grup secin", "error"); return; }
  const lids = Array.from(selectedLids);
  showLoading();
  try {
    const result = await api("POST", "/admin/api/channels/assign", { lids, cid });
    toast(result.updated + " kanal atandi", "success");
    selectedLids.clear();
    updateBulkBar();
    document.getElementById("selectAll").checked = false;
    await loadGroups();
    await loadChannels();
  } catch (e) { toast(e.message, "error"); }
  finally { hideLoading(); }
}

async function bulkUngroup() {
  const lids = Array.from(selectedLids);
  showLoading();
  try {
    const result = await api("POST", "/admin/api/channels/ungroup", { lids });
    toast(result.ungrouped + " kanal gruptan cikarildi", "success");
    selectedLids.clear();
    updateBulkBar();
    document.getElementById("selectAll").checked = false;
    await loadGroups();
    await loadChannels();
  } catch (e) { toast(e.message, "error"); }
  finally { hideLoading(); }
}

function clearSelection() {
  selectedLids.clear();
  updateBulkBar();
  document.getElementById("selectAll").checked = false;
  renderChannels();
}

async function changeGroup(lid, newCid, selectEl) {
  const origCid = parseInt(selectEl.dataset.original);
  if (parseInt(newCid) === origCid) return;
  showLoading();
  try {
    await api("POST", "/admin/api/channels/assign", { lids: [lid], cid: parseInt(newCid) });
    selectEl.dataset.original = newCid;
    toast("Kanal grup degistirildi", "success");
    await loadGroups();
    await loadChannels();
  } catch (e) {
    toast(e.message, "error");
    selectEl.value = origCid;
  }
  finally { hideLoading(); }
}

async function reorderChannel(lid, direction, idx) {
  try {
    const result = await api("POST", "/admin/api/channels/reorder", { lid, direction });
    if (result.swapped) {
      toast("Kanal siralamasi degistirildi", "success");
      await loadChannels();
    }
  } catch (e) { toast(e.message, "error"); }
}

function debounceSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => { await loadChannels(); }, 300);
}

// ====== REFRESH ======
async function doRefresh() {
  showLoading();
  try {
    const result = await api("POST", "/admin/api/refresh");
    toast(result.message || "Refresh tamamlandi", result.success ? "success" : "error");
    await loadGroups();
    await loadChannels();
  } catch (e) { toast(e.message, "error"); }
  finally { hideLoading(); }
}

// ====== INIT ======
async function init() {
  try {
    await loadGroups();
    await loadChannels();
  } catch (e) {
    toast("Veri yukleme hatasi: " + e.message, "error");
  }
}

// Boot: always check auth first
checkAuth();
</script>
'''


# ============================================================
# ADMIN AUTH ENDPOINTS
# ============================================================
@app.get("/admin/api/auth-check")
async def auth_check(request: Request):
    """Check if user is authenticated by verifying session cookie."""
    session = await get_admin_session(request)
    return {"authenticated": session is not None, "requires_login": session is None}


@app.post("/admin/api/login")
async def admin_login(request: Request):
    """Login endpoint - validates credentials, creates session cookie."""
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")

    if not username or not password:
        raise HTTPException(status_code=400, detail="Kullanici adi ve sifre gerekli")

    if not verify_admin_credentials(username, password):
        raise HTTPException(status_code=403, detail="Gecersiz kimlik bilgileri")

    # Create session
    token = secrets.token_urlsafe(32)
    ADMIN_SESSIONS[token] = {"expires": time_module.time() + 86400, "username": username}

    # Clean up expired sessions
    now = time_module.time()
    expired = [k for k, v in ADMIN_SESSIONS.items() if v["expires"] <= now]
    for k in expired:
        del ADMIN_SESSIONS[k]

    response = JSONResponse({"success": True, "message": "Giris basarili"})
    response.set_cookie(
        key="vavuubey_session",
        value=token,
        httponly=True,
        max_age=86400,
        samesite="strict",
        secure=False,  # Set True if using HTTPS
        path="/"
    )
    return response


@app.post("/admin/api/logout")
async def admin_logout(request: Request):
    """Logout endpoint - removes session from server AND client."""
    # Remove session from server
    session_token = request.cookies.get("vavuubey_session")
    if session_token and session_token in ADMIN_SESSIONS:
        del ADMIN_SESSIONS[session_token]

    # Clear cookie
    response = JSONResponse({"success": True})
    response.delete_cookie(
        key="vavuubey_session",
        path="/",
        samesite="strict"
    )
    return response


# ============================================================
# ADMIN PAGE
# ============================================================
@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Serve admin panel HTML. Auth is enforced client-side AND server-side."""
    return HTMLResponse(ADMIN_HTML)


# ============================================================
# ADMIN API ENDPOINTS (require session auth)
# ============================================================
@app.get("/admin/api/groups")
async def api_get_groups(auth: Optional[str] = Depends(require_admin)):
    """List all groups with channel counts."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT cid, name, sort_order FROM categories ORDER BY sort_order, name")
    groups = []
    for r in c.fetchall():
        cid = r["cid"]
        c2 = conn.cursor()
        c2.execute("SELECT COUNT(*) as cnt FROM channels WHERE cid=?", (cid,))
        cnt = c2.fetchone()["cnt"]
        groups.append({"cid": cid, "name": r["name"], "sort_order": r["sort_order"], "count": cnt})
    c.execute("SELECT COUNT(*) as cnt FROM channels WHERE cid=0")
    ungrouped = c.fetchone()["cnt"]
    if ungrouped > 0:
        groups.append({"cid": 0, "name": "Grupsuz", "sort_order": 99999, "count": ungrouped})
    conn.close()
    return groups


@app.post("/admin/api/groups/create")
async def api_create_group(request: Request, auth: Optional[str] = Depends(require_admin)):
    """Create a new group."""
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name required")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="Name too long")

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT cid FROM categories WHERE name=?", (name,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="Group already exists")

    c.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 as nxt FROM categories")
    nxt = c.fetchone()["nxt"]
    c.execute("INSERT INTO categories(name, sort_order) VALUES(?, ?)", (name, nxt))
    cid = c.lastrowid
    conn.commit()
    conn.close()
    return {"success": True, "cid": cid, "name": name}


@app.post("/admin/api/groups/rename")
async def api_rename_group(request: Request, auth: Optional[str] = Depends(require_admin)):
    """Rename a group."""
    body = await request.json()
    cid = body.get("cid")
    name = body.get("name", "").strip()
    if not cid or not name:
        raise HTTPException(status_code=400, detail="cid and name required")

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT cid FROM categories WHERE cid=?", (cid,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Group not found")

    c.execute("UPDATE categories SET name=? WHERE cid=?", (name, cid))
    c.execute("UPDATE channels SET grp=? WHERE cid=?", (name, cid))
    conn.commit()
    conn.close()
    return {"success": True}


@app.post("/admin/api/groups/delete")
async def api_delete_group(request: Request, auth: Optional[str] = Depends(require_admin)):
    """Delete a group. Channels become ungrouped."""
    body = await request.json()
    cid = body.get("cid")
    if not cid:
        raise HTTPException(status_code=400, detail="cid required")

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT cid FROM categories WHERE cid=?", (cid,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Group not found")

    c.execute("UPDATE channels SET cid=0, grp='' WHERE cid=?", (cid,))
    c.execute("DELETE FROM categories WHERE cid=?", (cid,))
    conn.commit()
    conn.close()
    return {"success": True}


@app.post("/admin/api/groups/reorder")
async def api_reorder_groups(request: Request, auth: Optional[str] = Depends(require_admin)):
    """Reorder groups - swap with neighbor (up/down)."""
    body = await request.json()
    conn = get_db()
    c = conn.cursor()

    if "cid" in body and "direction" in body:
        cid = body.get("cid")
        direction = body.get("direction")
        if not cid or direction not in ("up", "down"):
            conn.close()
            raise HTTPException(status_code=400, detail="cid and direction required")

        c.execute("SELECT cid, sort_order FROM categories WHERE cid=?", (cid,))
        row = c.fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="Group not found")

        current_order = row["sort_order"]

        if direction == "up":
            c.execute("SELECT cid, sort_order FROM categories WHERE sort_order < ? ORDER BY sort_order DESC LIMIT 1", (current_order,))
        else:
            c.execute("SELECT cid, sort_order FROM categories WHERE sort_order > ? ORDER BY sort_order ASC LIMIT 1", (current_order,))

        neighbor = c.fetchone()
        if not neighbor:
            conn.close()
            return {"success": True, "swapped": False}

        try:
            c.execute("BEGIN")
            c.execute("UPDATE categories SET sort_order=? WHERE cid=?", (neighbor["sort_order"], cid))
            c.execute("UPDATE categories SET sort_order=? WHERE cid=?", (current_order, neighbor["cid"]))
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            raise HTTPException(status_code=500, detail=f"DB error: {e}")

        conn.close()
        return {"success": True, "swapped": True}

    conn.close()
    raise HTTPException(status_code=400, detail="cid and direction required")


@app.get("/admin/api/channels")
async def api_get_channels(
    request: Request,
    group_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    auth: Optional[str] = Depends(require_admin)
):
    """List channels with optional group filter and search."""
    conn = get_db()
    c = conn.cursor()

    if group_id is not None and group_id > 0:
        if search:
            c.execute("""SELECT lid, name, grp, cid, logo, url, hls, sort_order FROM channels WHERE cid=? AND (name LIKE ? OR grp LIKE ?) ORDER BY sort_order, name""", (group_id, f"%{search}%", f"%{search}%"))
        else:
            c.execute("""SELECT lid, name, grp, cid, logo, url, hls, sort_order FROM channels WHERE cid=? ORDER BY sort_order, name""", (group_id,))
    elif group_id == 0:
        if search:
            c.execute("""SELECT lid, name, grp, cid, logo, url, hls, sort_order FROM channels WHERE cid=0 AND (name LIKE ? OR grp LIKE ?) ORDER BY sort_order, name""", (f"%{search}%", f"%{search}%"))
        else:
            c.execute("""SELECT lid, name, grp, cid, logo, url, hls, sort_order FROM channels WHERE cid=0 ORDER BY sort_order, name""")
    else:
        if search:
            c.execute("""SELECT ch.lid, ch.name, ch.grp, ch.cid, ch.logo, ch.url, ch.hls, ch.sort_order FROM channels ch LEFT JOIN categories cat ON ch.cid = cat.cid WHERE (ch.name LIKE ? OR ch.grp LIKE ?) ORDER BY COALESCE(cat.sort_order, 9999), ch.sort_order, ch.name""", (f"%{search}%", f"%{search}%"))
        else:
            c.execute("""SELECT ch.lid, ch.name, ch.grp, ch.cid, ch.logo, ch.url, ch.hls, ch.sort_order FROM channels ch LEFT JOIN categories cat ON ch.cid = cat.cid ORDER BY COALESCE(cat.sort_order, 9999), ch.sort_order, ch.name""")

    result = []
    for r in c.fetchall():
        result.append({
            "lid": r["lid"], "name": r["name"], "grp": r["grp"] or "",
            "cid": r["cid"] or 0, "logo": r["logo"] or "",
            "url": r["url"] or "", "hls": r["hls"] or "",
            "sort_order": r["sort_order"],
        })
    conn.close()
    return result


@app.post("/admin/api/channels/assign")
async def api_assign_channels(request: Request, auth: Optional[str] = Depends(require_admin)):
    """Batch assign channels to a group."""
    body = await request.json()
    lids = body.get("lids", [])
    cid = body.get("cid")

    if not lids or not isinstance(lids, list) or len(lids) == 0:
        raise HTTPException(status_code=400, detail="lids array required")
    if cid is None:
        raise HTTPException(status_code=400, detail="cid required")

    if cid == 0:
        target_grp = ""
    else:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT name FROM categories WHERE cid=?", (cid,))
        row = c.fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Group not found")
        target_grp = row["name"]

    conn = get_db()
    c = conn.cursor()

    try:
        c.execute("BEGIN")
        for lid in lids:
            c.execute("UPDATE channels SET cid=?, grp=? WHERE lid=?", (cid, target_grp, lid))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    c.execute("SELECT COUNT(*) as cnt FROM channels WHERE cid=? AND lid IN ({})".format(
        ",".join("?" * len(lids))), [cid] + list(lids))
    saved_count = c.fetchone()["cnt"]
    conn.close()

    return {"success": True, "updated": saved_count}


@app.post("/admin/api/channels/reorder")
async def api_reorder_channel(request: Request, auth: Optional[str] = Depends(require_admin)):
    """Reorder a channel within its group (swap with neighbor)."""
    body = await request.json()
    lid = body.get("lid")
    direction = body.get("direction", "up")

    if not lid or direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="lid and direction required")

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT lid, cid, sort_order, name FROM channels WHERE lid=?", (lid,))
    ch = c.fetchone()
    if not ch:
        conn.close()
        raise HTTPException(status_code=404, detail="Channel not found")

    ch_cid = ch["cid"]
    ch_sort = ch["sort_order"]
    ch_name = ch["name"]

    if direction == "up":
        c.execute("""SELECT lid, sort_order FROM channels WHERE cid=? AND lid != ? AND (sort_order < ? OR (sort_order = ? AND name < ?)) ORDER BY sort_order DESC, name DESC LIMIT 1""", (ch_cid, lid, ch_sort, ch_sort, ch_name))
    else:
        c.execute("""SELECT lid, sort_order FROM channels WHERE cid=? AND lid != ? AND (sort_order > ? OR (sort_order = ? AND name > ?)) ORDER BY sort_order ASC, name ASC LIMIT 1""", (ch_cid, lid, ch_sort, ch_sort, ch_name))

    neighbor = c.fetchone()
    if not neighbor:
        conn.close()
        return {"success": True, "swapped": False}

    try:
        c.execute("BEGIN")
        c.execute("UPDATE channels SET sort_order=? WHERE lid=?", (neighbor["sort_order"], lid))
        c.execute("UPDATE channels SET sort_order=? WHERE lid=?", (ch_sort, neighbor["lid"]))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    conn.close()
    return {"success": True, "swapped": True, "with_lid": neighbor["lid"]}


@app.post("/admin/api/channels/move")
async def api_move_channel(request: Request, auth: Optional[str] = Depends(require_admin)):
    """Move a channel to a specific position (drag & drop)."""
    body = await request.json()
    lid = body.get("lid")
    target_lid = body.get("target_lid")

    if not lid or not target_lid:
        raise HTTPException(status_code=400, detail="lid and target_lid required")

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT lid, cid, sort_order FROM channels WHERE lid=?", (lid,))
    src = c.fetchone()
    if not src:
        conn.close()
        raise HTTPException(status_code=404, detail="Source channel not found")

    c.execute("SELECT lid, cid, sort_order FROM channels WHERE lid=?", (target_lid,))
    tgt = c.fetchone()
    if not tgt:
        conn.close()
        raise HTTPException(status_code=404, detail="Target channel not found")

    src_cid = src["cid"]
    tgt_cid = tgt["cid"]

    if src_cid != tgt_cid:
        try:
            c.execute("BEGIN")
            c.execute("SELECT name FROM categories WHERE cid=?", (tgt_cid,))
            cat_row = c.fetchone()
            target_grp = cat_row["name"] if cat_row else ""
            target_order = tgt["sort_order"]
            c.execute("UPDATE channels SET sort_order=sort_order+1 WHERE cid=? AND sort_order>=?", (tgt_cid, target_order))
            c.execute("UPDATE channels SET cid=?, grp=?, sort_order=? WHERE lid=?", (tgt_cid, target_grp, target_order, lid))
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            raise HTTPException(status_code=500, detail=f"DB error: {e}")
        conn.close()
        return {"success": True, "new_group": tgt_cid}
    else:
        try:
            c.execute("BEGIN")
            c.execute("UPDATE channels SET sort_order=? WHERE lid=?", (tgt["sort_order"], lid))
            c.execute("UPDATE channels SET sort_order=? WHERE lid=?", (src["sort_order"], target_lid))
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            raise HTTPException(status_code=500, detail=f"DB error: {e}")
        conn.close()
        return {"success": True, "swapped": True}


@app.post("/admin/api/channels/ungroup")
async def api_ungroup_channels(request: Request, auth: Optional[str] = Depends(require_admin)):
    """Remove channels from their group."""
    body = await request.json()
    lids = body.get("lids", [])
    if not lids or not isinstance(lids, list) or len(lids) == 0:
        raise HTTPException(status_code=400, detail="lids array required")

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("BEGIN")
        for lid in lids:
            c.execute("UPDATE channels SET cid=0, grp='' WHERE lid=?", (lid,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    c.execute("SELECT COUNT(*) as cnt FROM channels WHERE cid=0 AND lid IN ({})".format(
        ",".join("?" * len(lids))), list(lids))
    saved_count = c.fetchone()["cnt"]
    conn.close()
    return {"success": True, "ungrouped": saved_count}


@app.post("/admin/api/channels/normalize")
async def api_normalize_sort(auth: Optional[str] = Depends(require_admin)):
    """Normalize sort_orders."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT lid, cid FROM channels ORDER BY cid, sort_order, name")
    rows = c.fetchall()
    current_cid = None
    order = 0
    c.execute("BEGIN")
    for row in rows:
        if row["cid"] != current_cid:
            current_cid = row["cid"]
            order = 1
        c.execute("UPDATE channels SET sort_order=? WHERE lid=?", (order, row["lid"]))
        order += 1
    conn.commit()
    conn.close()
    return {"success": True, "normalized": len(rows)}


@app.post("/admin/api/refresh")
async def api_refresh_channels(auth: Optional[str] = Depends(require_admin)):
    """Manually trigger a re-fetch of channels from Vavoo API."""
    if not state.STARTUP_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A refresh is already in progress")

    try:
        state.slog("=== Manual refresh triggered via admin ===")
        state.STARTUP_DONE = False
        state.STARTUP_ERROR = None

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Import server functions
            import server
            server.init_db()

            # Get signatures
            lokke = state.get_watchedsig()
            vavoo = state.get_auth_signature(force=True)

            # Fetch channels
            channels = await server.async_fetch_vavoo_channels()

            fetch_ok = False
            if channels:
                import sqlite3 as sqlite3_mod
                conn = sqlite3_mod.connect(state.DB_PATH)
                c = conn.cursor()
                c.execute("DELETE FROM channels")
                for ch in channels:
                    country = state.detect_country(ch)
                    if country not in ("TR", "DE", "BOTH"):
                        continue
                    name = ch.get("name", "Unknown")
                    url = ch.get("url", "")
                    logo = ch.get("logo", "")
                    group = ch.get("group", "")
                    grp = state.remap_group(name, group)
                    ch_id = 0
                    m = re.search(r'/play\d+/(\d+)\.m3u8', url)
                    if m:
                        ch_id = int(m.group(1))
                    if ch_id == 0:
                        ch_id = abs(hash(name)) % 9999999
                    final_country = country if country != "BOTH" else "TR"
                    clean = state.clean_name(name)
                    c.execute("INSERT OR REPLACE INTO channels(lid,name,grp,cid,logo,url,hls,sort_order,country,clean_name) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (ch_id, name, grp, 0, logo, url, "", 9999, final_country, clean))
                conn.commit()
                conn.close()
                fetch_ok = True

            # Fetch HLS links
            if fetch_ok:
                await server.async_fetch_hls_links()

            # Remap groups
            if fetch_ok:
                server.remap_groups()

        finally:
            loop.close()

        state.LAST_REFRESH = time_module.time()
        channel_count = state.count_db_channels()

        return JSONResponse(content={
            "success": True,
            "message": f"Refresh complete. {channel_count} channels.",
            "channel_count": channel_count,
        })
    except Exception as e:
        state.STARTUP_ERROR = str(e)
        raise HTTPException(status_code=500, detail=f"Refresh error: {e}")
    finally:
        state.STARTUP_LOCK.release()
        state.STARTUP_DONE = True
