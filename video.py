"""
video.py - VxParser Routes
All FastAPI endpoints, HLS proxy, Admin Panel
"""
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse, HTMLResponse
import state
import httpx
import time
import os
import json
import secrets

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

def add_log(msg):
    state.add_log(msg)

# ===== HLS PROXY =====
async def proxy_stream(url, ch_id):
    """Fetch and proxy a stream URL, rewriting HLS segments through self"""
    try:
        async with httpx.AsyncClient(timeout=20, verify=False, follow_redirects=True) as client:
            r = await client.get(url, headers=VAVOO_HEADERS)
            if r.status_code != 200:
                return JSONResponse({"error": f"upstream {r.status_code}", "url": url[:100]}, status_code=502)

            text = r.text
            base = url.rsplit("/", 1)[0] + "/"

            def rewrite(line):
                line = line.strip()
                if not line or line.startswith("#"):
                    return line
                if line.startswith("http"):
                    return f"/channel/{ch_id}/stream?url={line}"
                abs_url = base + line if not line.startswith("/") else f"https://vavoo.to{line}"
                return f"/channel/{ch_id}/stream?url={abs_url}"

            rewritten = "\n".join(rewrite(l) for l in text.split("\n"))
            return PlainTextResponse(rewritten, media_type="application/vnd.apple.mpegurl")
    except httpx.TimeoutException:
        return JSONResponse({"error": "timeout"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ===== M3U BUILD =====
def build_m3u(host):
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

# ===== PUBLIC ROUTES =====

@app.get("/")
async def root(request: Request):
    host = detect_host(request)
    return PlainTextResponse(
        f"VxParser Online\n\n"
        f"M3U: {host}/get.php?username=admin&password=admin&type=m3u_plus\n"
        f"Status: {host}/api/status\n"
        f"Admin: {host}/admin\n"
    )

@app.get("/ping")
@app.get("/pong")
@app.get("/health")
async def health():
    return PlainTextResponse("pong")

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
        return JSONResponse([{"num": i+1, "name": ch["name"], "stream_type": "live", "stream_id": ch["id"], "stream_icon": ch.get("logo", "")} for i, ch in enumerate(state.get_all_channels(ordered=True))])
    return PlainTextResponse(build_m3u(host), media_type="audio/x-mpegurl")

@app.get("/channel/{ch_id}")
async def channel_stream(ch_id: int):
    """Resolve channel and proxy stream - Y3-Direct first"""
    ch = state.get_channel(ch_id)
    if not ch:
        return JSONResponse({"error": "Not found"}, status_code=404)

    # Check resolve cache
    cache_key = str(ch_id)
    if cache_key in state.RESOLVE_CACHE:
        cached = state.RESOLVE_CACHE[cache_key]
        if cached.get("expires", 0) > time.time():
            return await proxy_stream(cached["url"], ch_id)

    # Resolve channel (Y3-Direct first)
    result = await state.resolve_channel(ch_id)
    if result.get("success"):
        stream_url = result["url"]
        state.RESOLVE_CACHE[cache_key] = {"url": stream_url, "expires": time.time() + 300}
        return await proxy_stream(stream_url, ch_id)

    return JSONResponse({"error": result.get("error", "Could not resolve")}, status_code=502)

@app.get("/channel/{ch_id}/stream")
async def channel_substream(ch_id: int, url: str):
    """Proxy sub-segments for HLS"""
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
                    abs_url = base + line if not line.startswith("/") else f"https://vavoo.to{line}"
                    return f"/channel/{ch_id}/stream?url={abs_url}"
                rewritten = "\n".join(rewrite(l) for l in text.split("\n"))
                return PlainTextResponse(rewritten, media_type="application/vnd.apple.mpegurl")
            return StreamingResponse(iter([r.content]), media_type=content_type or "video/MP2T", headers={"Content-Length": str(len(r.content))})
    except httpx.TimeoutException:
        return JSONResponse({"error": "timeout"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ===== API ROUTES =====

@app.get("/api/status")
async def api_status(request: Request):
    host = detect_host(request)
    import sqlite3
    info = {
        "data_ready": state.DATA_READY,
        "host": host,
        "lokke_sig": bool(state.WATCHED_SIG),
        "vavoo_token": bool(state.VAVOO_TOKEN),
        "vavoo_cooldown": bool(time.time() < state.VAVOO_TOKEN_COOLDOWN),
        "live2_domain": state.LIVE2_DOMAIN,
        "catalog_domain": state.CATALOG_DOMAIN,
        "resolve_cache": len(state.RESOLVE_CACHE),
        "logs": state.STARTUP_LOGS[-30:]
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
        c.execute("SELECT DISTINCT grp FROM channels ORDER BY grp")
        info["groups"] = [r[0] for r in c.fetchall()]
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

@app.get("/test/{ch_id}")
async def test_channel(ch_id: int):
    ch = state.get_channel(ch_id)
    if not ch:
        return JSONResponse({"error": "Not found"}, status_code=404)
    ov = state.get_override(ch_id)
    result = {
        "channel_id": ch_id,
        "name": ch.get("name", ""),
        "url": ch.get("url", ""),
        "hls": ch.get("hls", ""),
        "grp": ch.get("grp", ""),
        "country": ch.get("country", ""),
        "logo": ch.get("logo", ""),
        "has_override": bool(ov),
        "override_url": ov.get("url", "") if ov else ""
    }
    resolve = await state.resolve_channel(ch_id)
    result["resolve_method"] = resolve.get("method", "NONE")
    result["resolved_url"] = resolve.get("url", "")
    result["success"] = resolve.get("success", False)
    return JSONResponse(result)

@app.get("/reload")
async def reload():
    """Reload channels"""
    if not state.DATA_READY:
        state.DATA_READY = False
        import asyncio
        asyncio.create_task(state.startup_sequence())
        return JSONResponse({"status": "reloading"})
    return JSONResponse({"status": "already_ready"})

# ===== ADMIN API =====

@app.post("/api/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    pwd = body.get("password", "")
    if pwd == state.ADMIN_PASS:
        token = secrets.token_hex(32)
        state.ADMIN_SESSIONS[token] = time.time() + 86400
        return JSONResponse({"token": token, "success": True})
    return JSONResponse({"success": False, "error": "Sifre hatali"}, status_code=403)

def check_admin(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        if token in state.ADMIN_SESSIONS and state.ADMIN_SESSIONS[token] > time.time():
            return True
    return False

@app.get("/api/admin/channels")
async def admin_channels(request: Request, search: str = "", grp: str = ""):
    if not check_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    channels = state.get_all_channels(ordered=True)
    if search:
        channels = [c for c in channels if search.lower() in c["name"].lower()]
    if grp:
        channels = [c for c in channels if c["grp"] == grp]
    return JSONResponse({"channels": channels, "total": len(channels)})

@app.get("/api/admin/groups")
async def admin_groups(request: Request):
    if not check_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import sqlite3
    conn = sqlite3.connect(state.DB_PATH)
    c = conn.cursor()
    c.execute("SELECT grp, COUNT(*) as cnt FROM channels GROUP BY grp ORDER BY MIN(name)")
    groups = [{"name": r[0], "count": r[1]} for r in c.fetchall()]
    conn.close()
    return JSONResponse({"groups": groups})

@app.post("/api/admin/override")
async def admin_set_override(request: Request):
    if not check_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    ch_id = body.get("channel_id")
    url = body.get("url", "")
    enabled = body.get("enabled", True)
    if not ch_id:
        return JSONResponse({"error": "channel_id required"}, status_code=400)
    state.set_override(ch_id, url, 1 if enabled else 0)
    return JSONResponse({"success": True})

@app.get("/api/admin/overrides")
async def admin_get_overrides(request: Request):
    if not check_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    overrides = state.get_all_overrides()
    return JSONResponse({"overrides": overrides})

# ===== ADMIN PANEL HTML =====

ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VxParser Admin</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0f1117;color:#e1e4e8}
.login-wrap{display:flex;align-items:center;justify-content:center;min-height:100vh;background:#0f1117}
.login-box{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:40px;width:360px;text-align:center}
.login-box h1{color:#58a6ff;margin-bottom:8px;font-size:24px}
.login-box p{color:#8b949e;margin-bottom:24px;font-size:14px}
.login-box input{width:100%;padding:10px 14px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e1e4e8;font-size:14px;outline:none;margin-bottom:16px}
.login-box input:focus{border-color:#58a6ff}
.login-box button{width:100%;padding:10px;background:#238636;border:none;border-radius:6px;color:#fff;font-size:14px;cursor:pointer;font-weight:600}
.login-box button:hover{background:#2ea043}
.login-box .err{color:#f85149;font-size:13px;margin-top:12px;display:none}
.app{display:none}
.topbar{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;align-items:center;gap:16px}
.topbar h1{color:#58a6ff;font-size:18px}
.topbar .badge{background:#238636;color:#fff;padding:2px 10px;border-radius:10px;font-size:12px}
.topbar .right{margin-left:auto;display:flex;gap:10px;align-items:center}
.tabs{display:flex;background:#0d1117;border-bottom:1px solid #30363d;overflow-x:auto}
.tab{padding:10px 20px;cursor:pointer;color:#8b949e;font-size:13px;font-weight:600;border-bottom:2px solid transparent;white-space:nowrap}
.tab:hover{color:#e1e4e8}
.tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
.content{padding:20px}
.panel{display:none}
.panel.active{display:block}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:12px}
.card h3{color:#e1e4e8;font-size:14px;margin-bottom:8px}
.card .val{color:#58a6ff;font-size:20px;font-weight:700}
.card .sub{color:#8b949e;font-size:12px;margin-top:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-bottom:20px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#0d1117;color:#8b949e;text-align:left;padding:8px 12px;border-bottom:1px solid #30363d;position:sticky;top:0}
td{padding:8px 12px;border-bottom:1px solid #21262d;color:#c9d1d9}
tr:hover{background:#161b22}
.badge-ok{color:#3fb950;font-weight:600}
.badge-err{color:#f85149;font-weight:600}
.badge-warn{color:#d29922;font-weight:600}
.btn{padding:6px 14px;border-radius:6px;border:1px solid #30363d;background:#21262d;color:#e1e4e8;cursor:pointer;font-size:12px}
.btn:hover{background:#30363d}
.btn-primary{background:#238636;border-color:#238636;color:#fff}
.btn-primary:hover{background:#2ea043}
.btn-danger{background:#da3633;border-color:#da3633;color:#fff}
.btn-sm{padding:4px 10px;font-size:11px}
input[type="text"],input[type="search"],select{padding:8px 12px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e1e4e8;font-size:13px;outline:none}
input:focus,select:focus{border-color:#58a6ff}
.toolbar{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
.toolbar input{flex:1;min-width:200px}
.logbox{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:12px;font-family:monospace;font-size:12px;max-height:400px;overflow-y:auto;color:#8b949e;line-height:1.6}
.logbox div{padding:1px 0}
.empty{text-align:center;color:#8b949e;padding:40px;font-size:14px}
.modal-bg{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);display:none;align-items:center;justify-content:center;z-index:100}
.modal-bg.show{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:400px;max-width:90vw}
.modal h3{color:#e1e4e8;margin-bottom:16px}
.modal input,.modal textarea{width:100%;padding:8px 12px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e1e4e8;font-size:13px;outline:none;margin-bottom:12px}
.modal textarea{height:80px;resize:vertical;font-family:monospace}
.modal .btns{display:flex;gap:8px;justify-content:flex-end}
</style>
</head>
<body>

<div class="login-wrap" id="loginWrap">
  <div class="login-box">
    <h1>VxParser</h1>
    <p>Admin Panel</p>
    <input type="password" id="pwdInput" placeholder="Sifre" autocomplete="off">
    <button onclick="doLogin()">Giris</button>
    <div class="err" id="loginErr"></div>
  </div>
</div>

<div class="app" id="appWrap">
  <div class="topbar">
    <h1>VxParser</h1>
    <span class="badge" id="statusBadge">...</span>
    <div class="right">
      <button class="btn" onclick="refreshStatus()">Yenile</button>
      <button class="btn btn-danger" onclick="doLogout()">Cikis</button>
    </div>
  </div>
  <div class="tabs">
    <div class="tab active" onclick="showTab('dashboard')">Dashboard</div>
    <div class="tab" onclick="showTab('channels')">Kanallar</div>
    <div class="tab" onclick="showTab('groups')">Gruplar</div>
    <div class="tab" onclick="showTab('overrides')">Linkler</div>
    <div class="tab" onclick="showTab('logs')">Loglar</div>
  </div>
  <div class="content">
    <div class="panel active" id="panel-dashboard"></div>
    <div class="panel" id="panel-channels"></div>
    <div class="panel" id="panel-groups"></div>
    <div class="panel" id="panel-overrides"></div>
    <div class="panel" id="panel-logs"></div>
  </div>
</div>

<div class="modal-bg" id="overrideModal">
  <div class="modal">
    <h3>Override Ayarla</h3>
    <label style="color:#8b949e;font-size:12px">Kanal ID</label>
    <input type="text" id="ovChId" readonly>
    <label style="color:#8b949e;font-size:12px">Stream URL</label>
    <textarea id="ovUrl" placeholder="https://..."></textarea>
    <div class="btns">
      <button class="btn" onclick="closeModal()">Iptal</button>
      <button class="btn btn-primary" onclick="saveOverride()">Kaydet</button>
    </div>
  </div>
</div>

<script>
var TOKEN = localStorage.getItem("vx_token") || "";
var currentTab = "dashboard";

function doLogin() {
  var pwd = document.getElementById("pwdInput").value;
  if (!pwd) return;
  var errEl = document.getElementById("loginErr");
  errEl.style.display = "none";
  fetch("/api/admin/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({password: pwd})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.success) {
      TOKEN = d.token;
      localStorage.setItem("vx_token", TOKEN);
      showApp();
    } else {
      errEl.textContent = d.error || "Giris basarisiz";
      errEl.style.display = "block";
    }
  })
  .catch(function(e) {
    errEl.textContent = "Baglanti hatasi";
    errEl.style.display = "block";
  });
}

function doLogout() {
  TOKEN = "";
  localStorage.removeItem("vx_token");
  document.getElementById("loginWrap").style.display = "flex";
  document.getElementById("appWrap").style.display = "none";
}

function showApp() {
  document.getElementById("loginWrap").style.display = "none";
  document.getElementById("appWrap").style.display = "block";
  refreshStatus();
  showTab("dashboard");
}

function authHeaders() {
  return {"Authorization": "Bearer " + TOKEN};
}

function showTab(name) {
  currentTab = name;
  var tabs = document.querySelectorAll(".tab");
  var panels = document.querySelectorAll(".panel");
  for (var i = 0; i < tabs.length; i++) {
    tabs[i].classList.remove("active");
  }
  for (var i = 0; i < panels.length; i++) {
    panels[i].classList.remove("active");
  }
  var idx = {"dashboard":0,"channels":1,"groups":2,"overrides":3,"logs":4};
  if (idx[name] !== undefined) {
    tabs[idx[name]].classList.add("active");
    panels[idx[name]].classList.add("active");
  }
  if (name === "dashboard") loadDashboard();
  if (name === "channels") loadChannels();
  if (name === "groups") loadGroups();
  if (name === "overrides") loadOverrides();
  if (name === "logs") loadLogs();
}

function refreshStatus() {
  fetch("/api/status")
  .then(function(r) { return r.json(); })
  .then(function(d) {
    var badge = document.getElementById("statusBadge");
    if (d.data_ready) {
      badge.textContent = "ONLINE";
      badge.style.background = "#238636";
    } else {
      badge.textContent = "LOADING";
      badge.style.background = "#d29922";
    }
  });
}

function escHtml(s) {
  if (!s) return "";
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function loadDashboard() {
  fetch("/api/status")
  .then(function(r) { return r.json(); })
  .then(function(d) {
    var h = '<div class="grid">';
    h += '<div class="card"><h3>Toplam Kanal</h3><div class="val">' + (d.total_channels || 0) + '</div></div>';
    h += '<div class="card"><h3>TR Kanallar</h3><div class="val">' + (d.tr_channels || 0) + '</div></div>';
    h += '<div class="card"><h3>DE Kanallar</h3><div class="val">' + (d.de_channels || 0) + '</div></div>';
    h += '<div class="card"><h3>Lokke Sig</h3><div class="val"><span class="' + (d.lokke_sig ? "badge-ok" : "badge-err") + '">' + (d.lokke_sig ? "OK" : "YOK") + '</span></div></div>';
    h += '<div class="card"><h3>Vavoo Token</h3><div class="val"><span class="' + (d.vavoo_token ? "badge-ok" : "badge-warn") + '">' + (d.vavoo_token ? "OK" : "YOK") + '</span></div>';
    if (d.vavoo_cooldown) h += '<div class="sub">Cooldown aktif</div>';
    h += '</div>';
    h += '<div class="card"><h3>live2 Domain</h3><div class="val" style="font-size:14px">' + escHtml(d.live2_domain || "-") + '</div></div>';
    h += '<div class="card"><h3>Resolve Cache</h3><div class="val">' + (d.resolve_cache || 0) + '</div></div>';
    h += '</div>';

    if (d.logs && d.logs.length > 0) {
      h += '<div class="card"><h3>Son Loglar</h3><div class="logbox" style="max-height:250px">';
      for (var i = 0; i < d.logs.length; i++) {
        h += '<div>' + escHtml(d.logs[i]) + '</div>';
      }
      h += '</div></div>';
    }
    document.getElementById("panel-dashboard").innerHTML = h;
  });
}

var chSearch = "";
var chGroup = "";
var chPage = 0;
var chTotal = 0;

function loadChannels() {
  var q = "/api/admin/channels?search=" + encodeURIComponent(chSearch) + "&grp=" + encodeURIComponent(chGroup);
  fetch(q, {headers: authHeaders()})
  .then(function(r) { return r.json(); })
  .then(function(d) {
    chTotal = d.total || 0;
    var chs = d.channels || [];
    var h = '<div class="toolbar">';
    h += '<input type="search" placeholder="Kanal ara..." value="' + escHtml(chSearch) + '" onkeyup="chSearch=this.value;chPage=0;loadChannels()">';
    h += '<button class="btn" onclick="showTab(\'groups\')">Gruplar</button>';
    h += '</div>';
    h += '<div style="color:#8b949e;font-size:12px;margin-bottom:12px">' + chTotal + ' kanal</div>';
    if (chs.length === 0) {
      h += '<div class="empty">Kanal bulunamadi</div>';
    } else {
      h += '<table><tr><th>ID</th><th>Isim</th><th>Grup</th><th>URL</th><th>HLS</th><th>Islem</th></tr>';
      var max = Math.min(chs.length, 200);
      for (var i = 0; i < max; i++) {
        var c = chs[i];
        h += '<tr>';
        h += '<td>' + c.id + '</td>';
        h += '<td>' + escHtml(c.name) + '</td>';
        h += '<td><span style="color:#8b949e">' + escHtml(c.grp) + '</span></td>';
        h += '<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (c.url ? '<span class="badge-ok">Var</span>' : '<span class="badge-err">Yok</span>') + '</td>';
        h += '<td>' + (c.hls ? '<span class="badge-ok">HLS</span>' : '<span class="badge-warn">Yok</span>') + '</td>';
        h += '<td><button class="btn btn-sm" onclick="testCh(' + c.id + ')">Test</button>';
        h += ' <button class="btn btn-sm" onclick="openOverride(' + c.id + ',\'' + escHtml(c.name).replace(/'/g, "\\'") + '\')">Override</button></td>';
        h += '</tr>';
      }
      h += '</table>';
    }
    document.getElementById("panel-channels").innerHTML = h;
  });
}

function testCh(id) {
  fetch("/test/" + id)
  .then(function(r) { return r.json(); })
  .then(function(d) {
    var msg = d.name + " | " + (d.resolve_method || "-") + " | " + (d.success ? "BASARILI" : "BASARISIZ");
    if (d.resolved_url) msg += "\n" + d.resolved_url;
    alert(msg);
  });
}

function loadGroups() {
  fetch("/api/admin/groups", {headers: authHeaders()})
  .then(function(r) { return r.json(); })
  .then(function(d) {
    var gs = d.groups || [];
    var h = '<table><tr><th>Grup</th><th>Kanal Sayisi</th><th>Islem</th></tr>';
    for (var i = 0; i < gs.length; i++) {
      var g = gs[i];
      h += '<tr><td>' + escHtml(g.name) + '</td><td>' + g.count + '</td>';
      h += '<td><button class="btn btn-sm" onclick="chGroup=\'' + escHtml(g.name).replace(/'/g, "\\'") + '\';showTab(\'channels\')">Filtrele</button></td></tr>';
    }
    h += '</table>';
    document.getElementById("panel-groups").innerHTML = h;
  });
}

function loadOverrides() {
  fetch("/api/admin/overrides", {headers: authHeaders()})
  .then(function(r) { return r.json(); })
  .then(function(d) {
    var ovs = d.overrides || [];
    var h = '<div style="margin-bottom:12px;color:#8b949e;font-size:13px">Manuel stream URL override\'lari</div>';
    if (ovs.length === 0) {
      h += '<div class="empty">Hic override yok</div>';
    } else {
      h += '<table><tr><th>ID</th><th>Kanal</th><th>URL</th><th>Durum</th></tr>';
      for (var i = 0; i < ovs.length; i++) {
        var o = ovs[i];
        h += '<tr><td>' + o.ch_id + '</td><td>' + escHtml(o.ch_name || "") + '</td>';
        h += '<td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escHtml(o.url || "") + '</td>';
        h += '<td>' + (o.enabled ? '<span class="badge-ok">Aktif</span>' : '<span class="badge-err">Pasif</span>') + '</td></tr>';
      }
      h += '</table>';
    }
    document.getElementById("panel-overrides").innerHTML = h;
  });
}

function loadLogs() {
  fetch("/api/logs")
  .then(function(r) { return r.json(); })
  .then(function(d) {
    var logs = d.logs || [];
    var h = '<div class="logbox">';
    for (var i = 0; i < logs.length; i++) {
      h += '<div>' + escHtml(logs[i]) + '</div>';
    }
    h += '</div>';
    document.getElementById("panel-logs").innerHTML = h;
  });
}

function openOverride(chId, chName) {
  document.getElementById("ovChId").value = chId;
  document.getElementById("ovUrl").value = "";
  document.getElementById("overrideModal").classList.add("show");
}

function closeModal() {
  document.getElementById("overrideModal").classList.remove("show");
}

function saveOverride() {
  var chId = parseInt(document.getElementById("ovChId").value);
  var url = document.getElementById("ovUrl").value.trim();
  if (!url) { alert("URL bos olamaz"); return; }
  fetch("/api/admin/override", {
    method: "POST",
    headers: Object.assign({"Content-Type": "application/json"}, authHeaders()),
    body: JSON.stringify({channel_id: chId, url: url, enabled: true})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.success) {
      closeModal();
      if (currentTab === "channels") loadChannels();
      if (currentTab === "overrides") loadOverrides();
    }
  });
}

document.getElementById("pwdInput").addEventListener("keydown", function(e) {
  if (e.key === "Enter") doLogin();
});

if (TOKEN) {
  fetch("/api/admin/channels", {headers: authHeaders()})
  .then(function(r) {
    if (r.status === 401) {
      TOKEN = "";
      localStorage.removeItem("vx_token");
    } else {
      showApp();
    }
  });
}
</script>
</body>
</html>'''

@app.get("/admin")
async def admin_panel():
    return HTMLResponse(ADMIN_HTML)
