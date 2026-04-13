"""
video.py - FastAPI uygulama.
server.py'yi IMPORT ETMEZ - circular import onlemek icin.
Hem resolve hem token fonksiyonlari state.py'den gelir.
"""
import os
import sqlite3
import threading
import re

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

import state

app = FastAPI(title="VxParser IPTV Proxy", version="7.3.0")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "admin123")


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
    """UptimeRobot / cron keep-alive endpoint. Render uyumasin!"""
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


# ============================================================
# DEBUG
# ============================================================

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
    """Kanal bilgisi ve resolve sonucunu goster (redirect yapmaz)"""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT lid, name, url, hls, grp FROM channels WHERE lid=?", (sid,))
    ch = c.fetchone()
    conn.close()

    if not ch:
        return {"error": f"Kanal {sid} bulunamadi"}

    # Resolve test
    resolved_url, method = state.resolve_channel(sid)

    return {
        "lid": ch["lid"],
        "name": ch["name"],
        "url": ch["url"],
        "hls": ch["hls"],
        "grp": ch["grp"],
        "resolve_method": method,
        "resolved_url": resolved_url,
    }


# ============================================================
# CHANNEL RESOLVE - state.resolve_channel kullanir
# ============================================================

@app.get("/channel/{sid}")
async def play_channel(sid: str):
    """
    Kanal resolve ve redirect.
    state.resolve_channel() kullanir - server.py'ye gerek YOK.
    """
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
        "ORDER BY COALESCE(cat.sort_order, 9999), c.name"
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
    state.clear_resolve_cache()  # Cache'i temizle

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

# ============================================================
# ADMIN PANEL (Tek sayfa HTML)
# ============================================================

ADMIN_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VxParser Admin</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e0e0e0; min-height: 100vh; }

/* NAVBAR */
.navbar { background: linear-gradient(135deg, #1a1d2e 0%, #252840 100%); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #2a2d45; position: sticky; top: 0; z-index: 100; }
.navbar h1 { font-size: 20px; font-weight: 700; color: #8b5cf6; }
.navbar .nav-links { display: flex; gap: 8px; }
.nav-btn { padding: 8px 16px; border-radius: 8px; border: 1px solid #2a2d45; background: transparent; color: #a0a0b0; cursor: pointer; font-size: 13px; font-weight: 500; transition: all 0.2s; }
.nav-btn:hover, .nav-btn.active { background: #8b5cf6; color: white; border-color: #8b5cf6; }

/* LOGIN */
.login-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
.login-box { background: #1a1d2e; padding: 40px; border-radius: 16px; border: 1px solid #2a2d45; width: 360px; text-align: center; }
.login-box h2 { color: #8b5cf6; margin-bottom: 24px; font-size: 24px; }
.login-box input { width: 100%; padding: 12px 16px; border-radius: 8px; border: 1px solid #2a2d45; background: #0f1117; color: #e0e0e0; font-size: 14px; margin-bottom: 16px; }
.login-box button { width: 100%; padding: 12px; border-radius: 8px; border: none; background: #8b5cf6; color: white; font-size: 14px; font-weight: 600; cursor: pointer; }
.login-box button:hover { background: #7c3aed; }
.login-error { color: #ef4444; font-size: 13px; margin-top: 12px; }

/* TABS */
.tab-content { display: none; padding: 24px; max-width: 1400px; margin: 0 auto; }
.tab-content.active { display: block; }

/* CARDS */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
.card { background: #1a1d2e; border: 1px solid #2a2d45; border-radius: 12px; padding: 20px; }
.card-label { font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
.card-value { font-size: 28px; font-weight: 700; }
.card-value.green { color: #10b981; }
.card-value.red { color: #ef4444; }
.card-value.yellow { color: #f59e0b; }
.card-value.blue { color: #3b82f6; }
.card-value.purple { color: #8b5cf6; }

/* STATUS BAR */
.status-bar { background: #1a1d2e; border: 1px solid #2a2d45; border-radius: 12px; padding: 16px 20px; margin-bottom: 24px; display: flex; align-items: center; gap: 12px; }
.status-dot { width: 12px; height: 12px; border-radius: 50%; }
.status-dot.ok { background: #10b981; box-shadow: 0 0 8px #10b981; }
.status-dot.loading { background: #f59e0b; animation: pulse 1.5s infinite; }
.status-dot.error { background: #ef4444; box-shadow: 0 0 8px #ef4444; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

/* LOG BOX */
.log-box { background: #0f1117; border: 1px solid #2a2d45; border-radius: 12px; padding: 16px; max-height: 350px; overflow-y: auto; font-family: 'JetBrains Mono', monospace; font-size: 12px; line-height: 1.8; }
.log-box .log-line { color: #9ca3af; }
.log-box .log-line.ok { color: #10b981; }
.log-box .log-line.err { color: #ef4444; }
.log-box .log-line.warn { color: #f59e0b; }

/* TABLE */
.table-wrap { background: #1a1d2e; border: 1px solid #2a2d45; border-radius: 12px; overflow: hidden; }
.table-toolbar { padding: 16px 20px; display: flex; gap: 12px; align-items: center; border-bottom: 1px solid #2a2d45; flex-wrap: wrap; }
.table-toolbar input, .table-toolbar select { padding: 8px 12px; border-radius: 8px; border: 1px solid #2a2d45; background: #0f1117; color: #e0e0e0; font-size: 13px; }
.table-toolbar input { flex: 1; min-width: 200px; }
.ch-table { width: 100%; border-collapse: collapse; }
.ch-table th { background: #252840; padding: 12px 16px; text-align: left; font-size: 12px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.5px; position: sticky; top: 0; }
.ch-table td { padding: 10px 16px; border-bottom: 1px solid #1e2133; font-size: 13px; }
.ch-table tr:hover td { background: #252840; }
.ch-table .badge { display: inline-block; padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; }
.badge.hls { background: #10b98120; color: #10b981; }
.badge.auth { background: #3b82f620; color: #3b82f6; }
.badge.direct { background: #f59e0b20; color: #f59e0b; }
.badge.none { background: #ef444420; color: #ef4444; }
.btn-sm { padding: 5px 10px; border-radius: 6px; border: 1px solid #2a2d45; background: transparent; color: #a0a0b0; cursor: pointer; font-size: 12px; transition: all 0.2s; }
.btn-sm:hover { background: #8b5cf6; color: white; border-color: #8b5cf6; }
.btn-sm.test-ok { background: #10b98120; color: #10b981; border-color: #10b981; }
.btn-sm.test-fail { background: #ef444420; color: #ef4444; border-color: #ef4444; }
.logo-img { width: 32px; height: 32px; border-radius: 6px; object-fit: contain; background: #0f1117; }

/* GROUPS */
.groups-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px; }
.group-card { background: #1a1d2e; border: 1px solid #2a2d45; border-radius: 10px; padding: 16px; display: flex; justify-content: space-between; align-items: center; }
.group-name { font-weight: 600; font-size: 14px; }
.group-count { font-size: 24px; font-weight: 700; color: #8b5cf6; }

/* TEST MODAL */
.modal-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); z-index: 200; align-items: center; justify-content: center; }
.modal-overlay.show { display: flex; }
.modal { background: #1a1d2e; border: 1px solid #2a2d45; border-radius: 16px; padding: 24px; width: 600px; max-width: 90vw; max-height: 80vh; overflow-y: auto; }
.modal h3 { color: #8b5cf6; margin-bottom: 16px; }
.modal pre { background: #0f1117; padding: 12px; border-radius: 8px; font-size: 12px; overflow-x: auto; color: #9ca3af; word-break: break-all; }
.modal .close-btn { float: right; background: none; border: none; color: #6b7280; font-size: 20px; cursor: pointer; }
.modal .close-btn:hover { color: white; }
.modal .test-btn { padding: 8px 20px; border-radius: 8px; border: none; background: #8b5cf6; color: white; cursor: pointer; font-weight: 600; margin-top: 12px; }
.modal .test-btn:hover { background: #7c3aed; }

/* LINKS */
.link-section { margin-top: 24px; }
.link-box { background: #1a1d2e; border: 1px solid #2a2d45; border-radius: 12px; padding: 20px; margin-bottom: 12px; }
.link-box label { font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; }
.link-box .link-row { display: flex; gap: 8px; margin-top: 8px; align-items: center; }
.link-box input { flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid #2a2d45; background: #0f1117; color: #e0e0e0; font-size: 13px; font-family: 'JetBrains Mono', monospace; }
.copy-btn { padding: 10px 16px; border-radius: 8px; border: none; background: #8b5cf6; color: white; cursor: pointer; font-weight: 600; white-space: nowrap; }
.copy-btn:hover { background: #7c3aed; }

/* SCROLLBAR */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: #0f1117; }
::-webkit-scrollbar-thumb { background: #2a2d45; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #8b5cf6; }

@media (max-width: 768px) {
  .cards { grid-template-columns: repeat(2, 1fr); }
  .navbar { flex-direction: column; gap: 12px; }
  .table-toolbar { flex-direction: column; }
  .table-toolbar input { min-width: 100%; }
}
</style>
</head>
<body>

<!-- LOGIN -->
<div id="loginPage" class="login-wrap">
  <div class="login-box">
    <h2>VxParser</h2>
    <p style="color:#6b7280; margin-bottom:20px;">Admin Panel</p>
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
      <button class="nav-btn active" onclick="switchTab('dashboard')">Dashboard</button>
      <button class="nav-btn" onclick="switchTab('channels')">Kanallar</button>
      <button class="nav-btn" onclick="switchTab('groups')">Gruplar</button>
      <button class="nav-btn" onclick="switchTab('links')">Linkler</button>
      <button class="nav-btn" onclick="switchTab('logs')">Loglar</button>
      <button class="nav-btn" onclick="doReload()">Reload</button>
      <button class="nav-btn" onclick="doCacheClear()" style="border-color:#f59e0b;color:#f59e0b">Cache Temizle</button>
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
        <span id="chCount" style="color:#6b7280;font-size:13px"></span>
      </div>
      <div style="max-height:60vh;overflow-y:auto">
        <table class="ch-table">
          <thead><tr><th>#</th><th></th><th>Kanal</th><th>Grup</th><th>Tip</th><th>Islem</th></tr></thead>
          <tbody id="chBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- GROUPS -->
  <div id="tab-groups" class="tab-content">
    <div class="groups-grid" id="groupsGrid"></div>
  </div>

  <!-- LINKS -->
  <div id="tab-links" class="tab-content">
    <div class="link-section">
      <div class="link-box">
        <label>M3U Playlist</label>
        <div class="link-row">
          <input type="text" id="linkM3U" readonly>
          <button class="copy-btn" onclick="copyLink('linkM3U')">Kopyala</button>
        </div>
      </div>
      <div class="link-box">
        <label>Xtream Codes API</label>
        <div class="link-row">
          <input type="text" id="linkXtream" readonly>
          <button class="copy-btn" onclick="copyLink('linkXtream')">Kopyala</button>
        </div>
      </div>
      <div class="link-box">
        <label>EPG (XMLTV)</label>
        <div class="link-row">
          <input type="text" id="linkEPG" readonly>
          <button class="copy-btn" onclick="copyLink('linkEPG')">Kopyala</button>
        </div>
      </div>
    </div>
  </div>

  <!-- LOGS -->
  <div id="tab-logs" class="tab-content">
    <div style="margin-bottom:12px;display:flex;gap:8px">
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

<script>
let allChannels = [];
let currentTestLid = null;
const H = window.location.origin;

// AUTH
function doLogin() {
  const p = document.getElementById('passInput').value;
  fetch('/api/admin/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password:p})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){document.getElementById('loginPage').style.display='none';document.getElementById('appPage').style.display='block';loadAll();}
    else{document.getElementById('loginErr').textContent='Yanlis sifre!';}
  });
}
function doLogout(){document.getElementById('appPage').style.display='none';document.getElementById('loginPage').style.display='flex';document.getElementById('passInput').value='';}

// TABS
function switchTab(name){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  event.target.classList.add('active');
  if(name==='channels') loadChannels();
  if(name==='groups') loadGroups();
  if(name==='links') loadLinks();
  if(name==='logs') loadLogs();
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
    const dot=document.getElementById('statusDot');
    const txt=document.getElementById('statusText');
    if(d.data_ready){dot.className='status-dot ok';txt.textContent='Hazir! '+d.available_channels+' kanal yuklu';}
    else if(d.error){dot.className='status-dot error';txt.textContent='HATA: '+d.error;}
    else{dot.className='status-dot loading';txt.textContent='Kanallar yukleniyor...';}
  });
}

// CHANNELS
function loadChannels(){
  fetch('/api/admin/channels').then(r=>r.json()).then(d=>{
    allChannels=d.channels||[];
    // group filter
    const sel=document.getElementById('chGroup');
    const groups=[...new Set(allChannels.map(c=>c.grp).filter(Boolean))].sort();
    sel.innerHTML='<option value="">Tum Gruplar</option>'+groups.map(g=>'<option>'+g+'</option>').join('');
    filterChannels();
  });
}
function filterChannels(){
  const q=document.getElementById('chSearch').value.toLowerCase();
  const g=document.getElementById('chGroup').value;
  const t=document.getElementById('chType').value;
  let list=allChannels;
  if(q) list=list.filter(c=>c.name.toLowerCase().includes(q));
  if(g) list=list.filter(c=>c.grp===g);
  if(t==='hls') list=list.filter(c=>c.has_hls);
  else if(t==='url') list=list.filter(c=>c.url);
  else if(t==='nohls') list=list.filter(c=>!c.has_hls);
  document.getElementById('chCount').textContent=list.length+' kanal';
  const tb=document.getElementById('chBody');
  tb.innerHTML=list.slice(0,500).map(c=>{
    const badge=c.has_hls?'<span class="badge hls">HLS</span>':(c.url?'<span class="badge auth">URL</span>':'<span class="badge none">YOK</span>');
    const logo=c.logo?'<img class="logo-img" src="'+c.logo+'">':'<div class="logo-img"></div>';
    return `<tr><td>${c.lid}</td><td>${logo}</td><td>${c.name}</td><td>${c.grp}</td><td>${badge}</td><td><button class="btn-sm" onclick="openTest(${c.lid},this.dataset.n)" data-n="${c.name.replace(/"/g,'&quot;')}">Test</button></td></tr>`;
  }).join('');
}

// GROUPS
function loadGroups(){
  fetch('/stats').then(r=>r.json()).then(d=>{
    document.getElementById('groupsGrid').innerHTML=(d.groups||[]).map(g=>
      '<div class="group-card"><div><div class="group-name">'+g.group+'</div></div><div class="group-count">'+g.count+'</div></div>'
    ).join('');
  });
}

// LINKS
function loadLinks(){
  document.getElementById('linkM3U').value=H+'/get.php?username=admin&password=admin&type=m3u_plus';
  document.getElementById('linkXtream').value=H+'/player_api.php?username=admin&password=admin';
  document.getElementById('linkEPG').value=H+'/epg.xml';
}
function copyLink(id){
  navigator.clipboard.writeText(document.getElementById(id).value).then(()=>{
    const b=event.target;b.textContent='Kopyalandi!';setTimeout(()=>b.textContent='Kopyala',1500);
  });
}

// LOGS
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

// RELOAD
function doReload(){
  if(!confirm('Kanallari yeniden yuklemek istiyor musun?'))return;
  fetch('/reload').then(r=>r.json()).then(d=>{
    alert(d.message||'Reload basladi');
    setTimeout(loadAll, 5000);
    setTimeout(loadAll, 15000);
    setTimeout(loadAll, 30000);
  });
}
function doCacheClear(){
  fetch('/api/admin/cache/clear',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok){alert('Resolve cache temizlendi!');loadAll();}
    else{alert('Hata: '+d.message);}
  });
}

// TEST MODAL
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
      '<p><b>ID:</b> '+d.lid+'</p>'+
      '<p><b>URL:</b> '+(d.url?'Var':'YOK')+'</p>'+
      '<p><b>HLS:</b> '+(d.hls?'Var':'YOK')+'</p>'+
      '<p><b>Grup:</b> '+d.grp+'</p>';
  });
}
function runResolveTest(){
  const lid=currentTestLid;
  const btn=document.getElementById('modalTestBtn');
  btn.textContent='Test ediliyor...';btn.disabled=true;
  fetch('/api/admin/resolve/'+lid).then(r=>r.json()).then(d=>{
    btn.textContent='Resolve Test';btn.disabled=false;
    const pre=document.getElementById('modalResult');
    pre.style.display='block';
    pre.textContent=JSON.stringify(d,null,2);
    if(d.resolved_url){btn.textContent='Basarili!';btn.style.background='#10b981';}
    else{btn.textContent='Basarisiz';btn.style.background='#ef4444';}
    setTimeout(()=>{btn.style.background='';},3000);
  }).catch(()=>{btn.textContent='Hata';btn.disabled=false;});
}
function closeModal(){document.getElementById('testModal').classList.remove('show');}

// AUTO REFRESH
setInterval(loadAll, 30000);
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


@app.get("/api/admin/channels")
async def admin_channels():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT c.lid, c.name, c.url, c.hls, c.logo, "
        "COALESCE(cat.name, 'Sonstige') as grp "
        "FROM channels c LEFT JOIN categories cat ON c.cid = cat.cid "
        "ORDER BY COALESCE(cat.sort_order, 9999), c.name"
    )
    channels = []
    for r in c.fetchall():
        channels.append({
            "lid": r["lid"],
            "name": r["name"],
            "grp": r["grp"],
            "logo": r["logo"] or "",
            "url": r["url"] or "",
            "has_hls": bool(r["hls"]),
        })
    conn.close()
    return {"channels": channels, "total": len(channels)}


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
    """Resolve cache'i temizle - CDN URL'leri yeniden cozulur."""
    state.clear_resolve_cache()
    return {"ok": True, "message": "Resolve cache temizlendi"}


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
