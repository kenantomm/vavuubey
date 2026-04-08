import os
import sqlite3
import logging
import threading

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse

DB_PATH = os.environ.get("DB_PATH", "/tmp/vxparser.db")
M3U_PATH = os.environ.get("M3U_PATH", "/tmp/playlist.m3u")
BASE_HOST = os.environ.get("BASE_HOST", "")

app = FastAPI(title="VxParser IPTV Proxy", version="3.0.0")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/")
async def root():
    import server
    return {
        "status": "ready" if server.DATA_READY else "loading",
        "error": server.STARTUP_ERROR,
        "load_time": round(server.LOAD_TIME, 1) if server.DATA_READY else None,
        "message": (
            "Hazir!"
            if server.DATA_READY
            else "Kanallar yukleniyor, 30-60sn bekle..."
        ),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# ============================================================
# CHANNEL RESOLVE - IPTV Oynatici icin (3 yontemli)
# ============================================================

@app.get("/channel/{sid}")
async def channel(sid: str):
    """
    Kanal ID'sine gore stream URL'ini coz ve yonlendir.
    YONTEM 1: HLS + Lokke Imzasi (en stabil)
    YONTEM 2: Standart Token (vavoo_auth parametresi)
    YONTEM 3: Dogrudan URL (son care)
    """
    import server

    resolved = server.resolve_channel(sid)
    if resolved:
        return RedirectResponse(url=resolved, status_code=302)

    raise HTTPException(status_code=503, detail="Yayin acilamadi.")


# ============================================================
# M3U PLAYLIST - HER KANAL PROXY URL ILE
# ============================================================

@app.get("/get.php")
async def get_playlist(
    username: str = Query("admin"),
    password: str = Query("admin"),
    type: str = Query("m3u_plus"),
    output: str = Query("m3u_plus"),
):
    """
    M3U playlist endpoint.
    Her kanal /channel/{lid} proxy URL'i ile sunulur.
    IPTV oynatici kanali actiginda resolve calisir.
    """
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT c.lid, c.name, c.url, c.hls, c.logo, "
        "COALESCE(cat.name, 'Sonstige') as group_name "
        "FROM channels c "
        "LEFT JOIN categories cat ON c.cid = cat.cid "
        "ORDER BY COALESCE(cat.sort_order, 9999), c.name"
    )
    channels = c.fetchall()
    conn.close()

    host = BASE_HOST
    lines = ['#EXTM3U url-tvg="" deinterlace="1"']
    for ch in channels:
        lid = ch["lid"]
        logo = ch["logo"] or ""
        group = ch["group_name"]
        name = ch["name"]

        # HER ZAMAN proxy URL - resolve endpoint'e gider
        stream_url = f"{host}/channel/{lid}"

        lines.append(
            f'#EXTINF:-1 tvg-id="{lid}" tvg-logo="{logo}" '
            f'group-title="{group}",{name}'
        )
        lines.append(stream_url)

    return PlainTextResponse(
        content="\n".join(lines), media_type="audio/x-mpegurl"
    )


# ============================================================
# XTREAM CODES JSON API
# ============================================================

@app.get("/player_api.php")
async def player_api(
    username: str = Query("admin"),
    password: str = Query("admin"),
    action: str = Query(None),
):
    conn = get_db()

    if action == "get_live_categories":
        c = conn.cursor()
        c.execute(
            "SELECT cid as category_id, name as category_name "
            "FROM categories ORDER BY sort_order"
        )
        cats = [dict(r) for r in c.fetchall()]
        conn.close()
        return cats

    elif action == "get_live_streams":
        host = BASE_HOST
        c = conn.cursor()
        c.execute(
            "SELECT c.lid as stream_id, c.name as name, c.logo as stream_icon, "
            "c.cid as category_id, COALESCE(cat.name, 'Sonstige') as category_name "
            "FROM channels c "
            "LEFT JOIN categories cat ON c.cid = cat.cid "
            "ORDER BY COALESCE(cat.sort_order, 9999), c.name"
        )
        streams = []
        for r in c.fetchall():
            row = dict(r)
            # Xtream formatinda stream URL
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
            "user_info": {
                "username": username,
                "status": "Active",
                "exp_date": "2099-01-01",
                "max_connections": 1,
            },
            "server_info": {"port": os.environ.get("PORT", "10000")},
            "available_channels": total,
            "available_categories": cats,
        }


# ============================================================
# M3U DOSYA INDIRME
# ============================================================

@app.get("/playlist.m3u")
async def download_playlist():
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT c.name, c.lid, c.logo, "
        "COALESCE(cat.name, 'Sonstige') as group_name "
        "FROM channels c "
        "LEFT JOIN categories cat ON c.cid = cat.cid "
        "ORDER BY COALESCE(cat.sort_order, 9999), c.name"
    )
    channels = c.fetchall()
    conn.close()

    host = BASE_HOST
    lines = ["#EXTM3U"]
    for ch in channels:
        logo = ch["logo"] or ""
        group = ch["group_name"]
        name = ch["name"]
        lid = ch["lid"]
        stream_url = f"{host}/channel/{lid}"

        lines.append(
            f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{name}'
        )
        lines.append(stream_url)

    return PlainTextResponse(
        content="\n".join(lines),
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": "attachment; filename=playlist.m3u"},
    )


# ============================================================
# RELOAD
# ============================================================

@app.get("/reload")
async def reload_channels():
    import server

    server.DATA_READY = False
    server.STARTUP_ERROR = None

    def do_reload():
        try:
            server.init_db()
            server.fetch_vavoo_channels()
            server.fetch_hls_links()
            server.remap_groups()
            server.generate_m3u()
            server.DATA_READY = True
        except Exception as e:
            server.STARTUP_ERROR = str(e)

    threading.Thread(target=do_reload, daemon=True).start()
    return {"status": "reloading", "message": "Yukleniyor... 30-60sn bekle"}


# ============================================================
# STATS
# ============================================================

@app.get("/stats")
async def stats():
    import server

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM channels")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM categories")
    cats = c.fetchone()[0]
    c.execute(
        "SELECT COALESCE(cat.name, 'Eslesmeyen') as g, COUNT(*) as cnt "
        "FROM channels c "
        "LEFT JOIN categories cat ON c.cid = cat.cid "
        "GROUP BY c.cid "
        "ORDER BY MIN(cat.sort_order)"
    )
    groups = [{"group": r[0], "count": r[1]} for r in c.fetchall()]

    c.execute(
        "SELECT COUNT(*) FROM channels WHERE hls != '' AND hls IS NOT NULL"
    )
    hls_count = c.fetchone()[0]

    # Token durumu
    vavoo_token = bool(server._vavoo_sig)
    lokke_token = bool(server._watched_sig)

    conn.close()
    return {
        "total_channels": total,
        "total_categories": cats,
        "hls_channels": hls_count,
        "vavoo_token": vavoo_token,
        "lokke_token": lokke_token,
        "groups": groups,
        "data_ready": server.DATA_READY,
    }
