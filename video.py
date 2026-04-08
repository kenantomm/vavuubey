@'
import os, sqlite3, httpx, logging, threading
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

DB_PATH = os.environ.get("DB_PATH", "/tmp/vxparser.db")
M3U_PATH = os.environ.get("M3U_PATH", "/tmp/playlist.m3u")
app = FastAPI(title="VxParser IPTV Proxy", version="2.0.0")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/")
async def root():
    import server
    return {"status": "ready" if server.DATA_READY else "loading",
            "message": "Hazir!" if server.DATA_READY else "Kanallar yukleniyor, 30-60sn bekle..."}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/get.php")
async def get_playlist(username: str = Query("admin"), password: str = Query("admin"), type: str = Query("m3u_plus"), output: str = Query("m3u_plus")):
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT c.lid,c.name,c.url,c.logo,COALESCE(cat.name,'Sonstige') as group_name
        FROM channels c LEFT JOIN categories cat ON c.cid=cat.cid
        ORDER BY COALESCE(cat.sort_order,9999),c.name""")
    channels = c.fetchall()
    conn.close()
    lines = ['#EXTM3U url-tvg="" deinterlace="1"']
    for ch in channels:
        lines.append(f'#EXTINF:-1 tvg-id="{ch["lid"]}" tvg-logo="{ch["logo"] or ""}" group-title="{ch["group_name"]}",{ch["name"]}')
        lines.append(ch["url"])
    return PlainTextResponse(content="\n".join(lines), media_type="audio/x-mpegurl")

@app.get("/player_api.php")
async def player_api(username: str = Query("admin"), password: str = Query("admin"), action: str = Query(None)):
    conn = get_db()
    if action == "get_live_categories":
        c = conn.cursor()
        c.execute("SELECT cid as category_id, name as category_name FROM categories ORDER BY sort_order")
        cats = [dict(r) for r in c.fetchall()]
        conn.close()
        return cats
    elif action == "get_live_streams":
        c = conn.cursor()
        c.execute("""SELECT c.lid as stream_id,c.name as name,c.logo as stream_icon,c.cid as category_id,
            COALESCE(cat.name,'Sonstige') as category_name FROM channels c
            LEFT JOIN categories cat ON c.cid=cat.cid ORDER BY COALESCE(cat.sort_order,9999),c.name""")
        streams = [dict(r) for r in c.fetchall()]
        conn.close()
        return streams
    else:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM channels")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM categories")
        cats = c.fetchone()[0]
        conn.close()
        return {"user_info":{"username":username,"status":"Active","exp_date":"2099-01-01","max_connections":1},
                "server_info":{"port":os.environ.get("PORT","10000")},"available_channels":total,"available_categories":cats}

@app.get("/playlist.m3u")
async def download_playlist():
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT c.name,c.url,c.logo,COALESCE(cat.name,'Sonstige') as group_name
        FROM channels c LEFT JOIN categories cat ON c.cid=cat.cid ORDER BY COALESCE(cat.sort_order,9999),c.name""")
    lines = ["#EXTM3U"]
    for ch in c.fetchall():
        lines.append(f'#EXTINF:-1 tvg-logo="{ch["logo"] or ""}" group-title="{ch["group_name"]}",{ch["name"]}')
        lines.append(ch["url"])
    conn.close()
    return PlainTextResponse(content="\n".join(lines), media_type="audio/x-mpegurl",
        headers={"Content-Disposition":"attachment; filename=playlist.m3u"})

@app.get("/reload")
async def reload_channels():
    import server
    server.DATA_READY = False
    server.STARTUP_ERROR = None
    def do_reload():
        try:
            server.init_db(); server.fetch_vavoo_channels(); server.filter_tr_de()
            server.remap_groups(); server.generate_m3u(); server.DATA_READY = True
        except Exception as e:
            server.STARTUP_ERROR = str(e)
    threading.Thread(target=do_reload, daemon=True).start()
    return {"status":"reloading","message":"Yukleniyor... 30-60sn bekle"}

@app.get("/stats")
async def stats():
    import server
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM channels")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM categories")
    cats = c.fetchone()[0]
    c.execute("""SELECT COALESCE(cat.name,'Eslesmeyen') as g, COUNT(*) as cnt
        FROM channels c LEFT JOIN categories cat ON c.cid=cat.cid GROUP BY c.cid ORDER BY MIN(cat.sort_order)""")
    groups = [{"group":r[0],"count":r[1]} for r in c.fetchall()]
    conn.close()
    return {"total_channels":total,"total_categories":cats,"groups":groups,"data_ready":server.DATA_READY}
'@ | Out-File -Encoding utf8 video.py