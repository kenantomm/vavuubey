"""
video.py - FastAPI uygulama
Her endpoint state modulunu kullanir (server.py ile ayni state).
"""
import os
import sqlite3
import threading

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse

import state

app = FastAPI(title="VxParser IPTV Proxy", version="6.0.0")


def get_db():
    conn = sqlite3.connect(state.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_base_host(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host", "localhost:10000")
    return f"{proto}://{host}"


# ============================================================
# DURUM
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "ready" if state.DATA_READY else "loading",
        "error": state.STARTUP_ERROR,
        "load_time": round(state.LOAD_TIME, 1) if state.DATA_READY else None,
        "message": "Hazir!" if state.DATA_READY else "Kanallar yukleniyor, 30-60sn bekle...",
        "logs_count": len(state.STARTUP_LOGS),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# ============================================================
# DEBUG - Adim adim loglar
# ============================================================

@app.get("/debug")
async def debug():
    return {
        "data_ready": state.DATA_READY,
        "error": state.STARTUP_ERROR,
        "vavoo_token": bool(state._vavoo_sig),
        "lokke_token": bool(state._watched_sig),
        "startup_logs": state.STARTUP_LOGS,
        "db_path": state.DB_PATH,
    }


# ============================================================
# CHANNEL RESOLVE
# ============================================================

@app.get("/channel/{sid}")
async def channel(sid: str):
    # server modulunu import et (main thread olarak)
    import server
    resolved = server.resolve_channel(sid)
    if resolved:
        return RedirectResponse(url=resolved, status_code=302)
    raise HTTPException(status_code=503, detail="Yayin acilamadi.")


# ============================================================
# M3U PLAYLIST
# ============================================================

@app.get("/get.php")
async def get_playlist(
    request: Request,
    username: str = Query("admin"),
    password: str = Query("admin"),
    type: str = Query("m3u_plus"),
    output: str = Query("m3u_plus"),
):
    host = get_base_host(request)

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT c.lid, c.name, c.url, c.hls, c.logo, "
        "COALESCE(cat.name, 'Sonstige') as group_name "
        "FROM channels c LEFT JOIN categories cat ON c.cid = cat.cid "
        "ORDER BY COALESCE(cat.sort_order, 9999), c.name"
    )
    channels = c.fetchall()
    conn.close()

    lines = ['#EXTM3U url-tvg="" deinterlace="1"']
    for ch in channels:
        lid = ch["lid"]
        logo = ch["logo"] or ""
        group = ch["group_name"]
        name = ch["name"]
        stream_url = f"{host}/channel/{lid}"
        lines.append(f'#EXTINF:-1 tvg-id="{lid}" tvg-logo="{logo}" group-title="{group}",{name}')
        lines.append(stream_url)

    return PlainTextResponse(content="\n".join(lines), media_type="audio/x-mpegurl")


# ============================================================
# XTREAM CODES JSON API
# ============================================================

@app.get("/player_api.php")
async def player_api(
    request: Request,
    username: str = Query("admin"),
    password: str = Query("admin"),
    action: str = Query(None),
):
    host = get_base_host(request)
    conn = get_db()

    if action == "get_live_categories":
        c = conn.cursor()
        c.execute("SELECT cid as category_id, name as category_name FROM categories ORDER BY sort_order")
        cats = [dict(r) for r in c.fetchall()]
        conn.close()
        return cats

    elif action == "get_live_streams":
        c = conn.cursor()
        c.execute(
            "SELECT c.lid as stream_id, c.name as name, c.logo as stream_icon, "
            "c.cid as category_id, COALESCE(cat.name, 'Sonstige') as category_name "
            "FROM channels c LEFT JOIN categories cat ON c.cid = cat.cid "
            "ORDER BY COALESCE(cat.sort_order, 9999), c.name"
        )
        streams = []
        for r in c.fetchall():
            row = dict(r)
            row["stream_url"] = f"{host}/channel/{row['stream_id']}"
            streams.append(row)
        conn.close()
        return streams

    else:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM channels")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM categories")
        cats = c.fetchone()[0]
        conn.close()
        return {
            "user_info": {"username": username, "status": "Active", "exp_date": "2099-01-01", "max_connections": 1},
            "server_info": {"port": str(state.PORT)},
            "available_channels": total,
            "available_categories": cats,
        }


# ============================================================
# RELOAD
# ============================================================

@app.get("/reload")
async def reload_channels():
    state.DATA_READY = False
    state.STARTUP_ERROR = None
    state.STARTUP_LOGS.clear()

    def do_reload():
        import server
        try:
            server.init_db()
            server.fetch_vavoo_channels()
            server.fetch_hls_links()
            server.remap_groups()
            state.DATA_READY = True
        except Exception as e:
            state.STARTUP_ERROR = str(e)

    threading.Thread(target=do_reload, daemon=True).start()
    return {"status": "reloading", "message": "Yukleniyor... 30-60sn bekle"}


# ============================================================
# STATS
# ============================================================

@app.get("/stats")
async def stats():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM channels")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM categories")
    cats = c.fetchone()[0]
    c.execute(
        "SELECT COALESCE(cat.name, 'Eslesmeyen') as g, COUNT(*) as cnt "
        "FROM channels c LEFT JOIN categories cat ON c.cid = cat.cid "
        "GROUP BY c.cid ORDER BY MIN(cat.sort_order)"
    )
    groups = [{"group": r[0], "count": r[1]} for r in c.fetchall()]
    c.execute("SELECT COUNT(*) FROM channels WHERE hls != '' AND hls IS NOT NULL")
    hls_count = c.fetchone()[0]
    conn.close()

    return {
        "total_channels": total,
        "total_categories": cats,
        "hls_channels": hls_count,
        "vavoo_token": bool(state._vavoo_sig),
        "lokke_token": bool(state._watched_sig),
        "error": state.STARTUP_ERROR,
        "groups": groups,
        "data_ready": state.DATA_READY,
    }
