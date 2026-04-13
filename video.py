"""
video.py - FastAPI uygulama.
server.py'yi IMPORT ETMEZ - circular import onlemek icin.
Hem resolve hem token fonksiyonlari state.py'den gelir.

v7.4.0 - Grup yonetimi, kanal siralama, grup ekleme/silme
"""
import os
import sqlite3
import threading
import re

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

import state

app = FastAPI(title="VxParser IPTV Proxy", version="7.4.0")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "admin123")

CHANNEL_ORDER = "COALESCE(cat.sort_order, 9999), c.sort_order, c.name"


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


@app.get("/ping")
@app.get("/pong")
async def ping_pong():
    return {"status": "pong", "ready": state.DATA_READY}


# ============================================================
# API STATUS (Xtream uyumlu)
# ============================================================

@app.get("/api/status")
async def api_status():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM channels")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM categories")
    cats = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM channels WHERE hls != '' AND hls IS NOT NULL")
    hls_count = c.fetchone()[0]
    conn.close()
    return {
        "status": "ready" if state.DATA_READY else "loading",
        "data_ready": state.DATA_READY,
        "error": state.STARTUP_ERROR,
        "load_time": round(state.LOAD_TIME, 1) if state.DATA_READY else None,
        "available_channels": total,
        "available_categories": cats,
        "hls_channels": hls_count,
        "vavoo_token": bool(state._vavoo_sig),
        "lokke_token": bool(state._watched_sig),
        "resolve_cache": state.get_resolve_cache_info(),
        "startup_logs": state.STARTUP_LOGS,
    }


@app.get("/debug")
async def debug():
    return {
        "data_ready": state.DATA_READY,
        "error": state.STARTUP_ERROR,
        "vavoo_token": bool(state._vavoo_sig),
        "lokke_token": bool(state._watched_sig),
        "resolve_cache": state.get_resolve_cache_info(),
        "startup_logs": state.STARTUP_LOGS,
        "db_path": state.DB_PATH,
    }


# ============================================================
# CHANNEL TEST (debug - redirect yapmaz)
# ============================================================

@app.get("/test/{sid}")
async def test_channel(sid: str):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT lid, name, url, hls, grp FROM channels WHERE lid=?", (sid,))
    ch = c.fetchone()
    conn.close()
    if not ch:
        return {"error": f"Kanal {sid} bulunamadi"}
    resolved_url, method = state.resolve_channel(sid)
    return {
        "lid": ch["lid"], "name": ch["name"], "url": ch["url"],
        "hls": ch["hls"], "grp": ch["grp"],
        "resolve_method": method, "resolved_url": resolved_url,
    }


# ============================================================
# CHANNEL RESOLVE
# ============================================================

@app.get("/channel/{sid}")
async def play_channel(sid: str):
    url, method = state.resolve_channel(sid)
    if url:
        return RedirectResponse(url=url, status_code=302)
    raise HTTPException(status_code=503, detail=method)


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
        f"ORDER BY {CHANNEL_ORDER}"
    )
    channels = c.fetchall()
    conn.close()
    lines = [f'#EXTM3U url-tvg="{host}/epg.xml" deinterlace="1"']
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
# EPG (XMLTV)
# ============================================================

@app.get("/epg.xml")
async def epg_xml():
    xml_content = state.get_epg_data()
    if xml_content:
        return Response(content=xml_content, media_type="application/xml")
    return Response(content="<?xml version='1.0'?><tv><error>EPG not ready</error></tv>", media_type="application/xml")


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
            f"FROM channels c LEFT JOIN categories cat ON c.cid = cat.cid ORDER BY {CHANNEL_ORDER}"
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
            "server_info": {"port": str(state.PORT), "url": f"{host}/epg.xml"},
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
    state.clear_resolve_cache()

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
        "SELECT cat.cid, COALESCE(cat.name, 'Eslesmeyen') as g, COUNT(*) as cnt "
        "FROM channels c LEFT JOIN categories cat ON c.cid = cat.cid "
        "GROUP BY c.cid ORDER BY MIN(cat.sort_order)"
    )
    groups = [{"cid": r[0], "group": r[1], "count": r[2]} for r in c.fetchall()]
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


# ============================================================
# ADMIN API - GRUP YONETIMI
# ============================================================

@app.get("/api/admin/groups")
async def admin_groups_list():
    """Tum gruplari siralama ile listele."""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT cat.cid, cat.name, cat.sort_order, "
        "(SELECT COUNT(*) FROM channels WHERE channels.cid = cat.cid) as ch_count "
        "FROM categories cat ORDER BY cat.sort_order"
    )
    groups = []
    for r in c.fetchall():
        groups.append({
            "cid": r["cid"],
            "name": r["name"],
            "sort_order": r["sort_order"],
            "count": r["ch_count"],
        })
    conn.close()
    return {"groups": groups}


@app.post("/api/admin/groups")
async def admin_group_create(request: Request):
    """Yeni grup olustur."""
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return {"ok": False, "error": "Grup adi bos olamaz"}
    conn = get_db()
    c = conn.cursor()
    # Mevcut max sort_order bul
    c.execute("SELECT MAX(sort_order) FROM categories")
    max_so = c.fetchone()[0] or 0
    c.execute("INSERT INTO categories(name, sort_order) VALUES(?, ?)", (name, max_so + 1))
    conn.commit()
    cid = c.lastrowid
    conn.close()
    return {"ok": True, "cid": cid, "name": name}


@app.delete("/api/admin/groups/{cid}")
async def admin_group_delete(cid: int):
    """Grup sil - kanallar SONSTIGE'e tasinir."""
    conn = get_db()
    c = conn.cursor()
    # Varsayilan grup bul (SONSTIGE veya en son grup)
    c.execute("SELECT cid FROM categories WHERE name='DE SONSTIGE' LIMIT 1")
    default = c.fetchone()
    default_cid = default[0] if default else 0

    # Kanallari varsayilana tasini
    c.execute("UPDATE channels SET cid=?, grp='DE SONSTIGE' WHERE cid=?", (default_cid, cid))
    moved = c.rowcount
    # Grubu sil
    c.execute("DELETE FROM categories WHERE cid=?", (cid,))
    conn.commit()
    conn.close()
    return {"ok": True, "moved": moved}


@app.put("/api/admin/groups/{cid}/move")
async def admin_group_move(cid: int, request: Request):
    """Grup siralamasini degistir (up/down)."""
    body = await request.json()
    direction = body.get("direction", "up")  # "up" or "down"

    conn = get_db()
    c = conn.cursor()

    # Mevcut grup bilgisi
    c.execute("SELECT cid, sort_order FROM categories WHERE cid=?", (cid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "Grup bulunamadi"}

    current_so = row["sort_order"]

    if direction == "up":
        # Ustteki grupla takas
        c.execute("SELECT cid, sort_order FROM categories WHERE sort_order < ? ORDER BY sort_order DESC LIMIT 1", (current_so,))
    else:
        # Alttaki grupla takas
        c.execute("SELECT cid, sort_order FROM categories WHERE sort_order > ? ORDER BY sort_order ASC LIMIT 1", (current_so,))

    swap_row = c.fetchone()
    if not swap_row:
        conn.close()
        return {"ok": False, "error": "Zaten en ustte/altta"}

    # Takas yap
    c.execute("UPDATE categories SET sort_order=? WHERE cid=?", (swap_row["sort_order"], cid))
    c.execute("UPDATE categories SET sort_order=? WHERE cid=?", (current_so, swap_row["cid"]))
    conn.commit()
    conn.close()
    return {"ok": True}


# ============================================================
# ADMIN API - KANAL YONETIMI
# ============================================================

@app.get("/api/admin/channels")
async def admin_channels():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT c.lid, c.name, c.url, c.hls, c.logo, c.cid, c.sort_order, "
        "COALESCE(cat.name, 'Sonstige') as grp "
        f"FROM channels c LEFT JOIN categories cat ON c.cid = cat.cid ORDER BY {CHANNEL_ORDER}"
    )
    channels = []
    for r in c.fetchall():
        channels.append({
            "lid": r["lid"],
            "name": r["name"],
            "grp": r["grp"],
            "cid": r["cid"],
            "sort_order": r["sort_order"],
            "logo": r["logo"] or "",
            "url": r["url"] or "",
            "has_hls": bool(r["hls"]),
        })
    conn.close()
    return {"channels": channels, "total": len(channels)}


@app.put("/api/admin/channels/{lid}/move")
async def admin_channel_move(lid: int, request: Request):
    """Kanali ayni grupta yukari/asagi tasir."""
    body = await request.json()
    direction = body.get("direction", "up")

    conn = get_db()
    c = conn.cursor()

    # Mevcut kanal
    c.execute("SELECT lid, cid, sort_order FROM channels WHERE lid=?", (lid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "Kanal bulunamadi"}

    current_so = row["sort_order"]
    ch_cid = row["cid"]

    if direction == "up":
        # Ayni grupta sort_order < olan en yakin kanal
        c.execute(
            "SELECT lid, sort_order FROM channels WHERE cid=? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1",
            (ch_cid, current_so)
        )
    else:
        c.execute(
            "SELECT lid, sort_order FROM channels WHERE cid=? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1",
            (ch_cid, current_so)
        )

    swap_row = c.fetchone()
    if not swap_row:
        conn.close()
        return {"ok": False, "error": "Zaten en ustte/altta"}

    # Takas
    c.execute("UPDATE channels SET sort_order=? WHERE lid=?", (swap_row["sort_order"], lid))
    c.execute("UPDATE channels SET sort_order=? WHERE lid=?", (current_so, swap_row["lid"]))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/admin/channels/{lid}/group")
async def admin_channel_assign_group(lid: int, request: Request):
    """Kanali farkli bir gruba tasir."""
    body = await request.json()
    new_cid = body.get("cid", 0)

    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Yeni grup adini bul
    c.execute("SELECT name, sort_order FROM categories WHERE cid=?", (new_cid,))
    cat = c.fetchone()
    if not cat:
        conn.close()
        return {"ok": False, "error": "Grup bulunamadi"}

    # Kanalin mevcut gruptaki pozisyonunu bul
    c.execute("SELECT MAX(sort_order) FROM channels WHERE cid=?", (new_cid,))
    max_so = c.fetchone()[0] or 0

    # Kanali yeni gruba tasiri
    c.execute("UPDATE channels SET cid=?, grp=?, sort_order=? WHERE lid=?",
              (new_cid, cat["name"], max_so + 1, lid))
    conn.commit()
    conn.close()
    return {"ok": True, "new_group": cat["name"]}


# ============================================================
# ADMIN API - RESOLVE & CACHE
# ============================================================

@app.get("/api/admin/resolve/{sid}")
async def admin_resolve(sid: str):
    url, method = state.resolve_channel(sid)
    return {
        "channel_id": sid,
        "resolve_method": method,
        "resolved_url": url,
        "success": bool(url),
        "resolve_cache": state.get_resolve_cache_info(),
    }


@app.post("/api/admin/cache/clear")
async def admin_cache_clear():
    state.clear_resolve_cache()
    return {"ok": True, "message": "Resolve cache temizlendi"}


# ============================================================
# ADMIN PANEL (Tek sayfa HTML)
# ============================================================

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VxParser Admin</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e0e0e0;min-height:100vh}

/* NAVBAR */
.navbar{background:linear-gradient(135deg,#1a1d2e,#252840);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #2a2d45;position:sticky;top:0;z-index:100}
.navbar h1{font-size:18px;font-weight:700;color:#8b5cf6}
.nav-links{display:flex;gap:6px;flex-wrap:wrap}
.nav-btn{padding:6px 12px;border-radius:7px;border:1px solid #2a2d45;background:transparent;color:#a0a0b0;cursor:pointer;font-size:12px;font-weight:500;transition:all .2s;white-space:nowrap}
.nav-btn:hover,.nav-btn.active{background:#8b5cf6;color:#fff;border-color:#8b5cf6}

/* LOGIN */
.login-wrap{display:flex;align-items:center;justify-content:center;min-height:100vh}
.login-box{background:#1a1d2e;padding:40px;border-radius:16px;border:1px solid #2a2d45;width:360px;text-align:center}
.login-box h2{color:#8b5cf6;margin-bottom:20px;font-size:22px}
.login-box input{width:100%;padding:12px;border-radius:8px;border:1px solid #2a2d45;background:#0f1117;color:#e0e0e0;font-size:14px;margin-bottom:14px}
.login-box button{width:100%;padding:12px;border-radius:8px;border:none;background:#8b5cf6;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
.login-box button:hover{background:#7c3aed}
.login-error{color:#ef4444;font-size:13px;margin-top:10px}

/* TABS */
.tab-content{display:none;padding:20px;max-width:1400px;margin:0 auto}
.tab-content.active{display:block}

/* CARDS */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:20px}
.card{background:#1a1d2e;border:1px solid #2a2d45;border-radius:10px;padding:16px}
.card-label{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.card-value{font-size:24px;font-weight:700}
.card-value.green{color:#10b981}.card-value.red{color:#ef4444}
.card-value.yellow{color:#f59e0b}.card-value.blue{color:#3b82f6}.card-value.purple{color:#8b5cf6}

/* STATUS BAR */
.status-bar{background:#1a1d2e;border:1px solid #2a2d45;border-radius:10px;padding:14px 18px;margin-bottom:20px;display:flex;align-items:center;gap:10px}
.status-dot{width:10px;height:10px;border-radius:50%}
.status-dot.ok{background:#10b981;box-shadow:0 0 8px #10b981}
.status-dot.loading{background:#f59e0b;animation:pulse 1.5s infinite}
.status-dot.error{background:#ef4444;box-shadow:0 0 8px #ef4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* LOG BOX */
.log-box{background:#0f1117;border:1px solid #2a2d45;border-radius:10px;padding:14px;max-height:350px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.7}
.log-line{color:#9ca3af}.log-line.ok{color:#10b981}.log-line.err{color:#ef4444}.log-line.warn{color:#f59e0b}

/* TABLE */
.table-wrap{background:#1a1d2e;border:1px solid #2a2d45;border-radius:10px;overflow:hidden}
.table-toolbar{padding:12px 16px;display:flex;gap:10px;align-items:center;border-bottom:1px solid #2a2d45;flex-wrap:wrap}
.table-toolbar input,.table-toolbar select{padding:7px 10px;border-radius:7px;border:1px solid #2a2d45;background:#0f1117;color:#e0e0e0;font-size:12px}
.table-toolbar input{flex:1;min-width:180px}
.ch-table{width:100%;border-collapse:collapse}
.ch-table th{background:#252840;padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px;position:sticky;top:0;z-index:5}
.ch-table td{padding:8px 12px;border-bottom:1px solid #1e2133;font-size:12px}
.ch-table tr:hover td{background:#252840}
.ch-table .badge{display:inline-block;padding:2px 7px;border-radius:5px;font-size:10px;font-weight:600}
.badge.hls{background:#10b98120;color:#10b981}.badge.auth{background:#3b82f620;color:#3b82f6}
.badge.direct{background:#f59e0b20;color:#f59e0b}.badge.none{background:#ef444420;color:#ef4444}
.btn-sm{padding:4px 8px;border-radius:5px;border:1px solid #2a2d45;background:transparent;color:#a0a0b0;cursor:pointer;font-size:11px;transition:all .2s}
.btn-sm:hover{background:#8b5cf6;color:#fff;border-color:#8b5cf6}
.logo-img{width:28px;height:28px;border-radius:5px;object-fit:contain;background:#0f1117}

/* GROUP TABLE */
.grp-table{width:100%;border-collapse:collapse}
.grp-table th{background:#252840;padding:10px 14px;text-align:left;font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px}
.grp-table td{padding:10px 14px;border-bottom:1px solid #1e2133;font-size:13px}
.grp-table tr:hover td{background:#252840}
.grp-name{font-weight:600;color:#e0e0e0}
.grp-count{font-weight:700;color:#8b5cf6;font-size:18px;min-width:40px;text-align:center}

/* GRUP ADD BAR */
.grp-add-bar{background:#1a1d2e;border:1px solid #2a2d45;border-radius:10px;padding:14px 16px;margin-bottom:16px;display:flex;gap:10px;align-items:center}
.grp-add-bar input{flex:1;padding:9px 14px;border-radius:7px;border:1px solid #2a2d45;background:#0f1117;color:#e0e0e0;font-size:13px}
.grp-add-bar button{padding:9px 20px;border-radius:7px;border:none;background:#10b981;color:#fff;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.grp-add-bar button:hover{background:#059669}

/* ARROW BUTTONS */
.arrow-btn{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;border:1px solid #2a2d45;background:transparent;color:#a0a0b0;cursor:pointer;font-size:14px;transition:all .15s;padding:0;line-height:1}
.arrow-btn:hover{background:#8b5cf6;color:#fff;border-color:#8b5cf6}
.arrow-btn:disabled{opacity:.3;cursor:default;background:transparent;color:#4a4a5a;border-color:#1e2133}

/* GROUP SELECT */
.grp-select{padding:4px 6px;border-radius:5px;border:1px solid #2a2d45;background:#0f1117;color:#e0e0e0;font-size:11px;max-width:140px;cursor:pointer}
.grp-select:focus{outline:none;border-color:#8b5cf6}

/* TEST MODAL */
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);z-index:200;align-items:center;justify-content:center}
.modal-overlay.show{display:flex}
.modal{background:#1a1d2e;border:1px solid #2a2d45;border-radius:14px;padding:22px;width:580px;max-width:92vw;max-height:80vh;overflow-y:auto}
.modal h3{color:#8b5cf6;margin-bottom:14px}
.modal pre{background:#0f1117;padding:10px;border-radius:7px;font-size:11px;overflow-x:auto;color:#9ca3af;word-break:break-all}
.modal .close-btn{float:right;background:none;border:none;color:#6b7280;font-size:18px;cursor:pointer}
.modal .close-btn:hover{color:#fff}
.modal .test-btn{padding:8px 18px;border-radius:7px;border:none;background:#8b5cf6;color:#fff;cursor:pointer;font-weight:600;margin-top:10px}
.modal .test-btn:hover{background:#7c3aed}

/* LINKS */
.link-box{background:#1a1d2e;border:1px solid #2a2d45;border-radius:10px;padding:18px;margin-bottom:10px}
.link-box label{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px}
.link-box .link-row{display:flex;gap:8px;margin-top:7px;align-items:center}
.link-box input{flex:1;padding:9px 12px;border-radius:7px;border:1px solid #2a2d45;background:#0f1117;color:#e0e0e0;font-size:12px;font-family:'JetBrains Mono',monospace}
.copy-btn{padding:9px 14px;border-radius:7px;border:none;background:#8b5cf6;color:#fff;cursor:pointer;font-weight:600;white-space:nowrap;font-size:12px}
.copy-btn:hover{background:#7c3aed}

/* TOAST */
.toast{position:fixed;bottom:24px;right:24px;background:#1a1d2e;border:1px solid #2a2d45;border-radius:10px;padding:14px 20px;color:#e0e0e0;font-size:13px;z-index:999;opacity:0;transform:translateY(20px);transition:all .3s}
.toast.show{opacity:1;transform:translateY(0)}
.toast.ok{border-color:#10b981;color:#10b981}
.toast.err{border-color:#ef4444;color:#ef4444}

/* SCROLLBAR */
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:#0f1117}::-webkit-scrollbar-thumb{background:#2a2d45;border-radius:3px}::-webkit-scrollbar-thumb:hover{background:#8b5cf6}

@media(max-width:768px){
  .cards{grid-template-columns:repeat(2,1fr)}
  .navbar{flex-direction:column;gap:10px}
  .table-toolbar{flex-direction:column}
  .table-toolbar input{min-width:100%}
  .grp-add-bar{flex-direction:column}
}
</style>
</head>
<body>

<!-- LOGIN -->
<div id="loginPage" class="login-wrap">
  <div class="login-box">
    <h2>VxParser</h2>
    <p style="color:#6b7280;margin-bottom:18px">Admin Panel</p>
    <input type="password" id="passInput" placeholder="Sifre" onkeydown="if(event.key==='Enter')doLogin()">
    <button onclick="doLogin()">Giris</button>
    <div id="loginErr" class="login-error"></div>
  </div>
</div>

<!-- APP -->
<div id="appPage" style="display:none">
  <nav class="navbar">
    <h1>VxParser Admin</h1>
    <div class="nav-links">
      <button class="nav-btn active" onclick="switchTab('dashboard',this)">Dashboard</button>
      <button class="nav-btn" onclick="switchTab('channels',this)">Kanallar</button>
      <button class="nav-btn" onclick="switchTab('groups',this)">Gruplar</button>
      <button class="nav-btn" onclick="switchTab('links',this)">Linkler</button>
      <button class="nav-btn" onclick="switchTab('logs',this)">Loglar</button>
      <button class="nav-btn" onclick="doReload()">Reload</button>
      <button class="nav-btn" onclick="doCacheClear()" style="border-color:#f59e0b;color:#f59e0b">Cache</button>
      <button class="nav-btn" onclick="doLogout()" style="border-color:#ef4444;color:#ef4444">Cikis</button>
    </div>
  </nav>

  <!-- DASHBOARD -->
  <div id="tab-dashboard" class="tab-content active">
    <div class="status-bar">
      <div id="statusDot" class="status-dot loading"></div>
      <span id="statusText">Yukleniyor...</span>
    </div>
    <div class="cards">
      <div class="card"><div class="card-label">Toplam Kanal</div><div class="card-value blue" id="statTotal">-</div></div>
      <div class="card"><div class="card-label">Kategoriler</div><div class="card-value purple" id="statCats">-</div></div>
      <div class="card"><div class="card-label">HLS Kanal</div><div class="card-value green" id="statHLS">-</div></div>
      <div class="card"><div class="card-label">Vavoo Token</div><div class="card-value" id="statVavoo">-</div></div>
      <div class="card"><div class="card-label">Lokke Token</div><div class="card-value" id="statLokke">-</div></div>
      <div class="card"><div class="card-label">Resolve Cache</div><div class="card-value yellow" id="statCache">-</div></div>
      <div class="card"><div class="card-label">Yukleme Suresi</div><div class="card-value" id="statTime">-</div></div>
    </div>
  </div>

  <!-- CHANNELS -->
  <div id="tab-channels" class="tab-content">
    <div class="table-wrap">
      <div class="table-toolbar">
        <input type="text" id="chSearch" placeholder="Kanal ara..." oninput="filterChannels()">
        <select id="chGroup" onchange="filterChannels()"><option value="">Tum Gruplar</option></select>
        <select id="chType" onchange="filterChannels()">
          <option value="">Tum Tipler</option>
          <option value="hls">HLS Var</option>
          <option value="url">URL Var</option>
          <option value="nohls">HLS Yok</option>
        </select>
        <span id="chCount" style="color:#6b7280;font-size:12px"></span>
      </div>
      <div style="max-height:62vh;overflow-y:auto">
        <table class="ch-table">
          <thead><tr><th style="width:36px">#</th><th style="width:36px"></th><th>Kanal</th><th>Grup</th><th style="width:50px">Tip</th><th style="width:130px">Islem</th></tr></thead>
          <tbody id="chBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- GROUPS -->
  <div id="tab-groups" class="tab-content">
    <div class="grp-add-bar">
      <input type="text" id="newGrpName" placeholder="Yeni grup adi..." onkeydown="if(event.key==='Enter')addGroup()">
      <button onclick="addGroup()">+ Grup Ekle</button>
    </div>
    <div class="table-wrap">
      <div style="max-height:65vh;overflow-y:auto">
        <table class="grp-table">
          <thead><tr><th style="width:90px">Siralama</th><th>Grup Adi</th><th style="width:80px;text-align:center">Kanal</th><th style="width:70px">Sil</th></tr></thead>
          <tbody id="grpBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- LINKS -->
  <div id="tab-links" class="tab-content">
    <div class="link-box">
      <label>M3U Playlist</label>
      <div class="link-row"><input type="text" id="linkM3U" readonly><button class="copy-btn" onclick="copyLink('linkM3U')">Kopyala</button></div>
    </div>
    <div class="link-box">
      <label>Xtream Codes API</label>
      <div class="link-row"><input type="text" id="linkXtream" readonly><button class="copy-btn" onclick="copyLink('linkXtream')">Kopyala</button></div>
    </div>
    <div class="link-box">
      <label>EPG (XMLTV)</label>
      <div class="link-row"><input type="text" id="linkEPG" readonly><button class="copy-btn" onclick="copyLink('linkEPG')">Kopyala</button></div>
    </div>
  </div>

  <!-- LOGS -->
  <div id="tab-logs" class="tab-content">
    <div style="margin-bottom:10px;display:flex;gap:8px">
      <button class="btn-sm" onclick="loadLogs()">Yenile</button>
      <button class="btn-sm" onclick="clearLogs()">Temizle</button>
    </div>
    <div class="log-box" id="logBox"></div>
  </div>
</div>

<!-- TEST MODAL -->
<div id="testModal" class="modal-overlay">
  <div class="modal">
    <button class="close-btn" onclick="closeModal()">&times;</button>
    <h3 id="modalTitle">Kanal Test</h3>
    <div id="modalBody"></div>
    <button class="test-btn" id="modalTestBtn" onclick="runResolveTest()">Resolve Test</button>
    <pre id="modalResult" style="display:none"></pre>
  </div>
</div>

<!-- TOAST -->
<div id="toast" class="toast"></div>

<script>
let allChannels=[];
let allGroups=[];
let currentTestLid=null;
const H=window.location.origin;

function toast(msg,ok){
  const t=document.getElementById('toast');
  t.textContent=msg;t.className='toast show '+(ok?'ok':'err');
  setTimeout(()=>t.className='toast',2500);
}

// AUTH
function doLogin(){
  const p=document.getElementById('passInput').value;
  fetch('/api/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){document.getElementById('loginPage').style.display='none';document.getElementById('appPage').style.display='block';loadAll();}
    else{document.getElementById('loginErr').textContent='Yanlis sifre!';}
  });
}
function doLogout(){document.getElementById('appPage').style.display='none';document.getElementById('loginPage').style.display='flex';document.getElementById('passInput').value='';}

// TABS
function switchTab(name,el){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.nav-links .nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  if(el)el.classList.add('active');
  if(name==='channels')loadChannels();
  if(name==='groups')loadGroups();
  if(name==='links')loadLinks();
  if(name==='logs')loadLogs();
}

// LOAD ALL
function loadAll(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    document.getElementById('statTotal').textContent=d.available_channels||0;
    document.getElementById('statCats').textContent=d.available_categories||0;
    document.getElementById('statHLS').textContent=d.hls_channels||0;
    document.getElementById('statVavoo').textContent=d.vavoo_token?'OK':'YOK';
    document.getElementById('statVavoo').className='card-value '+(d.vavoo_token?'green':'red');
    document.getElementById('statLokke').textContent=d.lokke_token?'OK':'YOK';
    document.getElementById('statLokke').className='card-value '+(d.lokke_token?'green':'red');
    document.getElementById('statCache').textContent=(d.resolve_cache?.active||0)+'/'+(d.resolve_cache?.total_cached||0);
    document.getElementById('statTime').textContent=(d.load_time||0)+'s';
    const dot=document.getElementById('statusDot'),txt=document.getElementById('statusText');
    if(d.data_ready){dot.className='status-dot ok';txt.textContent='Hazir! '+d.available_channels+' kanal yuklu';}
    else if(d.error){dot.className='status-dot error';txt.textContent='HATA: '+d.error;}
    else{dot.className='status-dot loading';txt.textContent='Kanallar yukleniyor...';}
  });
}

// ============ CHANNELS ============
function loadChannels(){
  Promise.all([
    fetch('/api/admin/channels').then(r=>r.json()),
    fetch('/api/admin/groups').then(r=>r.json())
  ]).then(([chd, grd])=>{
    allChannels=chd.channels||[];
    allGroups=grd.groups||[];
    // group filter dropdown
    const sel=document.getElementById('chGroup');
    sel.innerHTML='<option value="">Tum Gruplar</option>'+allGroups.map(g=>'<option value="'+g.cid+'">'+g.name+'</option>').join('');
    filterChannels();
  });
}
function filterChannels(){
  const q=document.getElementById('chSearch').value.toLowerCase();
  const g=document.getElementById('chGroup').value;
  const t=document.getElementById('chType').value;
  let list=allChannels;
  if(q)list=list.filter(c=>c.name.toLowerCase().includes(q));
  if(g)list=list.filter(c=>c.cid==g);
  if(t==='hls')list=list.filter(c=>c.has_hls);
  else if(t==='url')list=list.filter(c=>c.url);
  else if(t==='nohls')list=list.filter(c=>!c.has_hls);
  document.getElementById('chCount').textContent=list.length+' kanal';
  const tb=document.getElementById('chBody');
  // grup dropdown options
  const grpOpts=allGroups.map(g=>'<option value="'+g.cid+'">'+g.name+'</option>').join('');
  tb.innerHTML=list.slice(0,500).map((c,i)=>{
    const badge=c.has_hls?'<span class="badge hls">HLS</span>':(c.url?'<span class="badge auth">URL</span>':'<span class="badge none">YOK</span>');
    const logo=c.logo?'<img class="logo-img" src="'+c.logo+'" loading="lazy">':'<div class="logo-img"></div>';
    const isFirst=i===0;
    const isLast=i===list.slice(0,500).length-1;
    // group select
    const sel='<select class="grp-select" onchange="moveChGroup('+c.lid+',this.value)" title="Grup degistir"><option value="">-- Grup --</option>'+grpOpts+'</select>';
    return '<tr>'+
      '<td>'+c.lid+'</td>'+
      '<td>'+logo+'</td>'+
      '<td><b>'+c.name+'</b></td>'+
      '<td>'+sel+'</td>'+
      '<td>'+badge+'</td>'+
      '<td>'+
        '<button class="arrow-btn" onclick="moveCh('+c.lid+','+c.cid+',\'up\')"'+(isFirst?' disabled':'')+' title="Yukari">&#9650;</button>'+
        '<button class="arrow-btn" onclick="moveCh('+c.lid+','+c.cid+',\'down\')"'+(isLast?' disabled':'')+' title="Asagi">&#9660;</button>'+
        ' <button class="btn-sm" onclick="openTest('+c.lid+',\''+c.name.replace(/'/g,'\\&#39;')+'\')">Test</button>'+
      '</td></tr>';
  }).join('');
  // secili grubu set et
  if(g)document.getElementById('chGroup').value=g;
}
function moveCh(lid,cid,dir){
  fetch('/api/admin/channels/'+lid+'/move',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({direction:dir})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){loadChannels();}
    else toast(d.error||'Hata',false);
  }).catch(()=>toast('Baglanti hatasi',false));
}
function moveChGroup(lid,newCid){
  if(!newCid)return;
  fetch('/api/admin/channels/'+lid+'/group',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({cid:newCid})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){toast('Grup degistirildi: '+d.new_group,true);loadChannels();}
    else toast(d.error||'Hata',false);
  }).catch(()=>toast('Baglanti hatasi',false));
}

// ============ GROUPS ============
function loadGroups(){
  fetch('/api/admin/groups').then(r=>r.json()).then(d=>{
    allGroups=d.groups||[];
    const tb=document.getElementById('grpBody');
    const len=allGroups.length;
    tb.innerHTML=allGroups.map((g,i)=>{
      return '<tr>'+
        '<td>'+
          '<button class="arrow-btn" onclick="moveGrp('+g.cid+',\'up\')"'+(i===0?' disabled':'')+' title="Yukari">&#9650;</button> '+
          '<button class="arrow-btn" onclick="moveGrp('+g.cid+',\'down\')"'+(i===len-1?' disabled':'')+' title="Asagi">&#9660;</button>'+
        '</td>'+
        '<td class="grp-name">'+g.name+'</td>'+
        '<td style="text-align:center"><span class="grp-count">'+g.count+'</span></td>'+
        '<td><button class="btn-sm" style="border-color:#ef4444;color:#ef4444" onclick="delGrp('+g.cid+',\''+g.name.replace(/'/g,'\\&#39;')+'\')">Sil</button></td>'+
      '</tr>';
    }).join('');
  });
}
function addGroup(){
  const inp=document.getElementById('newGrpName');
  const name=inp.value.trim();
  if(!name)return;
  fetch('/api/admin/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){inp.value='';toast('Grup eklendi: '+d.name,true);loadGroups();}
    else toast(d.error||'Hata',false);
  }).catch(()=>toast('Baglanti hatasi',false));
}
function delGrp(cid,name){
  if(!confirm('Bu grubu silmek istiyor musun? Kanallar "DE SONSTIGE" grubuna tasincak.'))return;
  fetch('/api/admin/groups/'+cid,{method:'DELETE'})
  .then(r=>r.json()).then(d=>{
    if(d.ok){toast(name+' silindi ('+d.moved+' kanal tasindi)',true);loadGroups();}
    else toast(d.error||'Hata',false);
  }).catch(()=>toast('Baglanti hatasi',false));
}
function moveGrp(cid,dir){
  fetch('/api/admin/groups/'+cid+'/move',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({direction:dir})})
  .then(r=>r.json()).then(d=>{
    if(d.ok)loadGroups();
    else toast(d.error||'Hata',false);
  }).catch(()=>toast('Baglanti hatasi',false));
}

// ============ LINKS ============
function loadLinks(){
  document.getElementById('linkM3U').value=H+'/get.php?username=admin&password=admin&type=m3u_plus';
  document.getElementById('linkXtream').value=H+'/player_api.php?username=admin&password=admin';
  document.getElementById('linkEPG').value=H+'/epg.xml';
}
function copyLink(id){
  navigator.clipboard.writeText(document.getElementById(id).value).then(()=>{
    const b=event.target;b.textContent='Kopyalandi!';setTimeout(()=>b.textContent='Kopyala',1200);
  });
}

// ============ LOGS ============
function loadLogs(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    const box=document.getElementById('logBox');
    box.innerHTML=(d.startup_logs||[]).map(l=>{
      let cls='log-line';
      if(l.includes('OK')||l.includes('TAMAM')||l.includes('alindi'))cls+=' ok';
      else if(l.includes('HATA')||l.includes('BASARISIZ'))cls+=' err';
      else if(l.includes('cekiliyor')||l.includes('aliniyor'))cls+=' warn';
      return '<div class="'+cls+'">'+l+'</div>';
    }).join('');
  });
}
function clearLogs(){document.getElementById('logBox').innerHTML='';}

// ============ RELOAD & CACHE ============
function doReload(){
  if(!confirm('Kanallari yeniden yuklemek istiyor musun?'))return;
  fetch('/reload').then(r=>r.json()).then(d=>{
    toast(d.message||'Reload basladi',true);
    setTimeout(loadAll,5000);setTimeout(loadAll,15000);setTimeout(loadAll,30000);
  });
}
function doCacheClear(){
  fetch('/api/admin/cache/clear',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok){toast('Resolve cache temizlendi!',true);loadAll();}
    else toast(d.message||'Hata',false);
  });
}

// ============ TEST MODAL ============
function openTest(lid,name){
  currentTestLid=lid;
  document.getElementById('modalTitle').textContent='Test: '+name+' (#'+lid+')';
  document.getElementById('modalBody').innerHTML='<p style="color:#6b7280">Kanal bilgisi yukleniyor...</p>';
  document.getElementById('modalResult').style.display='none';
  document.getElementById('modalTestBtn').style.display='inline-block';
  document.getElementById('testModal').classList.add('show');
  fetch('/test/'+lid).then(r=>r.json()).then(d=>{
    if(d.error){document.getElementById('modalBody').innerHTML='<p style="color:#ef4444">'+d.error+'</p>';return;}
    document.getElementById('modalBody').innerHTML=
      '<p><b>ID:</b> '+d.lid+'</p><p><b>URL:</b> '+(d.url?'Var':'YOK')+'</p>'+
      '<p><b>HLS:</b> '+(d.hls?'Var':'YOK')+'</p><p><b>Grup:</b> '+d.grp+'</p>';
  });
}
function runResolveTest(){
  const lid=currentTestLid;
  const btn=document.getElementById('modalTestBtn');
  btn.textContent='Test ediliyor...';btn.disabled=true;
  fetch('/api/admin/resolve/'+lid).then(r=>r.json()).then(d=>{
    btn.textContent='Resolve Test';btn.disabled=false;
    const pre=document.getElementById('modalResult');
    pre.style.display='block';pre.textContent=JSON.stringify(d,null,2);
    if(d.resolved_url){btn.textContent='Basarili!';btn.style.background='#10b981';}
    else{btn.textContent='Basarisiz';btn.style.background='#ef4444';}
    setTimeout(()=>btn.style.background='',3000);
  }).catch(()=>{btn.textContent='Hata';btn.disabled=false;});
}
function closeModal(){document.getElementById('testModal').classList.remove('show');}

// AUTO REFRESH
setInterval(loadAll,30000);
</script>
</body>
</html>"""


@app.get("/admin")
async def admin_page():
    return Response(content=ADMIN_HTML, media_type="text/html")


@app.post("/api/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    if body.get("password") == ADMIN_PASSWORD:
        return {"ok": True}
    return {"ok": False, "error": "Wrong password"}
