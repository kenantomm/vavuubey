"""
video.py - VxParser FastAPI application
Streaming endpoints + Admin Panel
"""
import os
import re
import sqlite3
from typing import Optional

from fastapi import FastAPI, Request, Response, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, PlainTextResponse, HTMLResponse

import state

# ============================================================
# FASTAPI APP
# ============================================================
app = FastAPI(title="VxParser", docs_url=None, redoc_url=None)


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
        logo_param = f' tvg-logo="{ch["logo"]}"' if ch.get("logo") else ""
        grp = ch.get("grp") or ""
        lines.append(f'#EXTINF:-1 group-title="{grp}"{logo_param},{ch["name"]}')
        lines.append(f'{host}/channel/{ch["lid"]}')
    return "\n".join(lines)


# ============================================================
# BASIC AUTH (for /admin routes)
# ============================================================
async def admin_auth(request: Request) -> Optional[str]:
    """Check basic auth if ADMIN_USER/ADMIN_PASS are set."""
    user = os.environ.get("ADMIN_USER", "")
    pw = os.environ.get("ADMIN_PASS", "")
    if not user or not pw:
        return None  # No auth required
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        import base64
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            if decoded == f"{user}:{pw}":
                return None
        except Exception:
            pass
    raise HTTPException(status_code=401, headers={"WWW-Authenticate": 'Basic realm="VxParser Admin"'})


# ============================================================
# STREAMING ENDPOINTS
# ============================================================
@app.get("/")
async def index():
    if not state.DATA_READY:
        return PlainTextResponse("VxParser is starting up...", status_code=503)
    return PlainTextResponse("VxParser is running. /admin for management.")


@app.get("/robots.txt")
async def robots_txt():
    return PlainTextResponse("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/get.php")
async def get_m3u(request: Request):
    if not state.DATA_READY:
        return PlainTextResponse("#EXTM3U\n# VxParser is starting...", status_code=503)
    host = str(request.base_url).rstrip("/")
    m3u = build_m3u(host)
    return PlainTextResponse(m3u, media_type="application/x-mpegurl")


@app.get("/player_api.php")
async def player_api(request: Request, action: str = Query("")):
    if not state.DATA_READY:
        return PlainTextResponse("VxParser is starting up...", status_code=503)
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
            c2 = conn.cursor()
            c2.execute("SELECT COUNT(*) as cnt FROM channels WHERE cid=?", (r["cid"],))
            cnt = c2.fetchone()["cnt"]
            cats.append({"category_id": r["cid"], "category_name": r["name"], "parent_id": 0})
        conn.close()
        return {"categories": cats}
    elif action == "":
        return PlainTextResponse(build_m3u(host), media_type="application/x-mpegurl")
    return PlainTextResponse("Unknown action", status_code=400)


@app.get("/channel/{lid}")
async def channel_stream(lid: int, request: Request):
    """Proxy channel stream with HLS rewriting."""
    if not state.DATA_READY:
        return PlainTextResponse("VxParser is starting up...", status_code=503)

    resolved_url, log_msg = state.resolve_channel(lid)
    if not resolved_url:
        return PlainTextResponse(f"Channel {lid} not resolvable: {log_msg}", status_code=404)

    import requests
    host = str(request.base_url).rstrip("/")
    headers_req = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
    }

    try:
        resp = requests.get(resolved_url, headers=headers_req, stream=True, timeout=15, verify=False)
    except Exception as e:
        return PlainTextResponse(f"Stream error: {e}", status_code=502)

    content_type = resp.headers.get("Content-Type", "video/mp4")

    if "mpegURL" in content_type or "x-mpegurl" in content_type or "m3u8" in content_type:
        # HLS manifest - rewrite URLs
        body = resp.text
        base_url = resolved_url.rsplit("/", 1)[0] if "/" in resolved_url else resolved_url

        def rewrite_url(match):
            url = match.group(0)
            if url.startswith("http"):
                return f"{host}/channel/{lid}/stream?url={url}"
            else:
                full = base_url + "/" + url
                return f"{host}/channel/{lid}/stream?url={full}"

        body = re.sub(r'https?://[^\s"]+|[^#\n\r][^\n\r]*', rewrite_url, body)
        return PlainTextResponse(body, media_type="application/vnd.apple.mpegurl")
    else:
        # Direct stream - proxy
        def stream_gen():
            try:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            except Exception:
                pass
        return StreamingResponse(stream_gen(), media_type=content_type)


@app.get("/channel/{lid}/stream")
async def channel_sub_stream(lid: int, url: str = Query("")):
    """Proxy sub-stream (HLS segments, etc.)."""
    if not url:
        return PlainTextResponse("No URL provided", status_code=400)

    import requests
    headers_req = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Referer": url.rsplit("/", 1)[0] if "/" in url else url,
    }

    try:
        resp = requests.get(url, headers=headers_req, stream=True, timeout=15, verify=False)
    except Exception as e:
        return PlainTextResponse(f"Stream error: {e}", status_code=502)

    content_type = resp.headers.get("Content-Type", "video/mp4")

    if "mpegURL" in content_type or "x-mpegurl" in content_type or "m3u8" in content_type:
        body = resp.text
        base_url = url.rsplit("/", 1)[0] if "/" in url else url
        host_base = ""

        def rewrite_url(match):
            u = match.group(0)
            if u.startswith("http"):
                return f"/channel/{lid}/stream?url={u}"
            else:
                full = base_url + "/" + u
                return f"/channel/{lid}/stream?url={full}"

        body = re.sub(r'https?://[^\s"]+|[^#\n\r][^\n\r]*', rewrite_url, body)
        return PlainTextResponse(body, media_type="application/vnd.apple.mpegurl")
    else:
        def stream_gen():
            try:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            except Exception:
                pass
        return StreamingResponse(stream_gen(), media_type=content_type)


# ============================================================
# ADMIN PANEL HTML
# ============================================================
ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>VxParser Admin</title>
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

/* Mini loading on save */
.saving-indicator{display:inline-block;width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--primary);border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-left:6px}

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
<div class="app">
  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-header">
      <div>
        <h1><span class="dot" id="statusDot"></span> VxParser</h1>
        <div class="subtitle">IPTV Channel Manager</div>
      </div>
      <button class="logout-btn" onclick="logout()">Logout</button>
    </div>
    <div class="group-list" id="groupList"></div>
    <div class="sidebar-footer">
      <div class="add-group-form">
        <input type="text" id="newGroupName" placeholder="New group name..." maxlength="50">
        <button onclick="createGroup()">+ Add</button>
      </div>
    </div>
  </aside>

  <!-- Main -->
  <div class="main">
    <div class="toolbar">
      <div class="title-area">
        <h2 id="currentTitle">All Channels</h2>
        <div class="channel-count" id="channelCount">0 channels</div>
      </div>
      <div class="search-box">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input type="text" id="searchInput" placeholder="Search channels..." oninput="debounceSearch()">
      </div>
    </div>
    <div class="bulk-bar" id="bulkBar">
      <span class="sel-count" id="selCount">0 selected</span>
      <select id="bulkGroupSelect"><option value="">Move to group...</option></select>
      <button class="btn btn-primary" onclick="bulkAssign()">Assign</button>
      <button class="btn btn-danger" onclick="bulkUngroup()">Remove from Group</button>
      <button class="btn" onclick="clearSelection()">Cancel</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th><input type="checkbox" class="ch-check" id="selectAll" onchange="toggleSelectAll(this)"></th>
            <th>Channel</th>
            <th></th>
            <th>Group</th>
            <th style="width:70px">Order</th>
          </tr>
        </thead>
        <tbody id="channelBody"></tbody>
      </table>
      <div class="empty-state" id="emptyState" style="display:none">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/></svg>
        <p>No channels found</p>
      </div>
    </div>
  </div>
</div>

<div class="loading-overlay" id="loadingOverlay"><div class="spinner"></div></div>
<div class="toast-container" id="toastContainer"></div>

<script>
let groups = [];
let channels = [];
let selectedGroup = null; // null = all
let selectedLids = new Set();
let searchTimer = null;
let allGroupsCache = [];
let isRendering = false; // prevents onchange from firing during render

// ====== API HELPERS ======
async function api(method, url, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(url, opts);
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
  // "All" item
  let html = `<div class="group-item ${selectedGroup === null ? 'active' : ''}" onclick="selectGroup(null)">
    <span class="g-icon">&#9776;</span>
    <span class="g-name">All Channels</span>
    <span class="g-count">${groups.reduce((s,g) => s + g.count, 0)}</span>
  </div>`;
  groups.forEach(g => {
    html += `<div class="group-item ${selectedGroup === g.cid ? 'active' : ''}" onclick="selectGroup(${g.cid})" data-cid="${g.cid}">
      <span class="g-icon">&#127909;</span>
      <span class="g-name" ondblclick="startRename(event, ${g.cid})" title="${g.name}">${escHtml(g.name)}</span>
      <span class="g-count">${g.count}</span>
      <span class="g-actions">
        <button class="g-action-btn" onclick="event.stopPropagation();startRename(event,${g.cid})" title="Rename">&#9998;</button>
        <button class="g-action-btn delete" onclick="event.stopPropagation();deleteGroup(${g.cid},'${escAttr(g.name)}')" title="Delete">&#128465;</button>
      </span>
    </div>`;
  });
  list.innerHTML = html;
}

function populateGroupDropdowns() {
  let opts = '<option value="">Move to group...</option>';
  opts += '<option value="0">Ungrouped</option>';
  groups.forEach(g => { opts += `<option value="${g.cid}">${escHtml(g.name)}</option>`; });
  const bulk = document.getElementById("bulkGroupSelect");
  bulk.innerHTML = opts;
}

async function selectGroup(cid) {
  selectedGroup = cid;
  selectedLids.clear();
  updateBulkBar();
  document.getElementById("selectAll").checked = false;
  renderSidebar();
  if (cid === null) {
    document.getElementById("currentTitle").textContent = "All Channels";
  } else {
    const g = groups.find(x => x.cid === cid);
    document.getElementById("currentTitle").textContent = g ? g.name : "Group";
  }
  await loadChannels();
}

async function createGroup() {
  const input = document.getElementById("newGroupName");
  const name = input.value.trim();
  if (!name) { toast("Enter a group name", "error"); return; }
  showLoading();
  try {
    await api("POST", "/admin/api/groups/create", { name });
    input.value = "";
    toast(`Group "${name}" created`, "success");
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
  const g = groups.find(x => x.cid === cid);
  nameEl.innerHTML = `<input class="rename-input" type="text" value="${escAttr(oldName)}" maxlength="50">`;
  const inp = nameEl.querySelector("input");
  inp.focus();
  inp.select();
  const finish = async () => {
    const newName = inp.value.trim();
    if (newName && newName !== oldName) {
      showLoading();
      try {
        await api("POST", "/admin/api/groups/rename", { cid, name: newName });
        toast(`Renamed to "${newName}"`, "success");
        await loadGroups();
        await loadChannels();
      } catch (err) { toast(err.message, "error"); }
      finally { hideLoading(); }
    } else {
      renderSidebar();
    }
  };
  inp.addEventListener("blur", finish);
  inp.addEventListener("keydown", ev => { if (ev.key === "Enter") inp.blur(); if (ev.key === "Escape") { inp.value = oldName; inp.blur(); } });
}

async function deleteGroup(cid, name) {
  if (!confirm(`Delete group "${name}"?\nChannels will be moved to Ungrouped.`)) return;
  showLoading();
  try {
    await api("POST", "/admin/api/groups/delete", { cid });
    toast(`Group "${name}" deleted`, "success");
    if (selectedGroup === cid) selectedGroup = null;
    await loadGroups();
    await loadChannels();
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
  countEl.textContent = channels.length + " channel" + (channels.length !== 1 ? "s" : "");

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
    const logo = ch.logo ? `<img class="ch-logo" src="${escAttr(ch.logo)}" alt="" onerror="this.style.display='none'">` : `<div class="ch-logo" style="display:flex;align-items:center;justify-content:center;font-size:14px;color:var(--muted)">&#127909;</div>`;
    // Build group options for individual select
    let grpOpts = `<option value="0" ${ch.cid == 0 ? 'selected' : ''}>Ungrouped</option>`;
    groups.forEach(g => {
      grpOpts += `<option value="${g.cid}" ${ch.cid === g.cid ? 'selected' : ''}>${escHtml(g.name)}</option>`;
    });

    html += `<tr draggable="true" data-lid="${ch.lid}" ondragstart="onDragStart(event)" ondragover="onDragOver(event)" ondragleave="onDragLeave(event)" ondrop="onDrop(event)" ondragend="onDragEnd(event)">
      <td><input type="checkbox" class="ch-check" ${checked} onchange="toggleChannel(${ch.lid}, this.checked)"></td>
      <td><span class="ch-name" title="${escAttr(ch.name)}">${escHtml(ch.name)}</span></td>
      <td>${logo}</td>
      <td><select class="grp-select" onchange="changeGroup(${ch.lid}, this.value, this)">${grpOpts}</select></td>
      <td><div class="order-btns">
        <button class="order-btn" onclick="reorderChannel(${ch.lid},'up',${idx})" ${idx === 0 ? 'disabled style="opacity:.3"' : ''}>&#9650;</button>
        <button class="order-btn" onclick="reorderChannel(${ch.lid},'down',${idx})" ${idx === channels.length - 1 ? 'disabled style="opacity:.3"' : ''}>&#9660;</button>
      </div></td>
    </tr>`;
  });
  body.innerHTML = html;
  // Use setTimeout to ensure DOM is ready before unblocking onchange handlers
  setTimeout(() => { isRendering = false; }, 50);
}

// ====== SELECTION ======
function toggleChannel(lid, checked) {
  if (checked) selectedLids.add(lid); else selectedLids.delete(lid);
  updateBulkBar();
  updateSelectAll();
}

function toggleSelectAll(el) {
  const checks = document.querySelectorAll("#channelBody .ch-check");
  checks.forEach(cb => {
    cb.checked = el.checked;
    const tr = cb.closest("tr");
    if (tr) {
      const lid = parseInt(tr.dataset.lid);
      if (el.checked) selectedLids.add(lid); else selectedLids.delete(lid);
    }
  });
  updateBulkBar();
}

function updateSelectAll() {
  const checks = document.querySelectorAll("#channelBody .ch-check");
  const allChecked = checks.length > 0 && Array.from(checks).every(cb => cb.checked);
  document.getElementById("selectAll").checked = allChecked;
}

function updateBulkBar() {
  const bar = document.getElementById("bulkBar");
  const count = selectedLids.size;
  document.getElementById("selCount").textContent = count + " selected";
  if (count > 0) bar.classList.add("visible"); else bar.classList.remove("visible");
}

function clearSelection() {
  selectedLids.clear();
  document.querySelectorAll("#channelBody .ch-check").forEach(cb => cb.checked = false);
  document.getElementById("selectAll").checked = false;
  updateBulkBar();
}

// ====== GROUP CHANGE (individual) ======
async function changeGroup(lid, newCid, selectEl) {
  if (isRendering) return; // Skip during render to prevent auto-trigger
  const oldCid = selectEl.getAttribute("data-original") ?? selectEl.value;
  const spinner = document.createElement("span");
  spinner.className = "saving-indicator";
  selectEl.parentNode.appendChild(spinner);
  try {
    await api("POST", "/admin/api/channels/assign", { lids: [lid], cid: parseInt(newCid) });
    toast("Group updated", "success");
    await loadGroups();
    await loadChannels();
  } catch (e) {
    toast(e.message, "error");
    selectEl.value = oldCid;
  } finally {
    spinner.remove();
  }
}

// ====== BULK ASSIGN ======
async function bulkAssign() {
  const cid = parseInt(document.getElementById("bulkGroupSelect").value);
  if (isNaN(cid) && document.getElementById("bulkGroupSelect").value !== "0") { toast("Select a group", "error"); return; }
  const targetCid = cid === 0 ? 0 : cid;
  if (selectedLids.size === 0) return;
  showLoading();
  try {
    const result = await api("POST", "/admin/api/channels/assign", { lids: Array.from(selectedLids), cid: targetCid });
    toast(`${result.updated} channels reassigned`, "success");
    selectedLids.clear();
    document.getElementById("selectAll").checked = false;
    updateBulkBar();
    // Normalize sort_orders and reload
    await api("POST", "/admin/api/channels/normalize");
    await loadGroups();
    await loadChannels();
  } catch (e) { toast(e.message, "error"); }
  finally { hideLoading(); }
}

// ====== BULK UNGROUP ======
async function bulkUngroup() {
  if (selectedLids.size === 0) return;
  showLoading();
  try {
    await api("POST", "/admin/api/channels/ungroup", { lids: Array.from(selectedLids) });
    toast(`${selectedLids.size} channels ungrouped`, "success");
    selectedLids.clear();
    document.getElementById("selectAll").checked = false;
    updateBulkBar();
    await loadGroups();
    await loadChannels();
  } catch (e) { toast(e.message, "error"); }
  finally { hideLoading(); }
}

// ====== REORDER ======
async function reorderChannel(lid, direction, idx) {
  try {
    await api("POST", "/admin/api/channels/reorder", { lid, direction });
    await loadChannels();
  } catch (e) { toast(e.message, "error"); }
}

// ====== DRAG & DROP ======
let dragLid = null;
function onDragStart(e) {
  dragLid = parseInt(e.currentTarget.dataset.lid);
  e.currentTarget.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
}
function onDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  const tr = e.currentTarget;
  if (tr.dataset.lid != dragLid) tr.classList.add("drag-over");
}
function onDragLeave(e) { e.currentTarget.classList.remove("drag-over"); }
async function onDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove("drag-over");
  const targetLid = parseInt(e.currentTarget.dataset.lid);
  if (dragLid !== null && dragLid !== targetLid) {
    showLoading();
    try {
      await api("POST", "/admin/api/channels/move", { lid: dragLid, target_lid: targetLid });
      toast("Channel moved", "success");
      await loadChannels();
    } catch (err) { toast(err.message, "error"); }
    finally { hideLoading(); }
  }
}
function onDragEnd(e) {
  e.currentTarget.classList.remove("dragging");
  document.querySelectorAll(".drag-over").forEach(el => el.classList.remove("drag-over"));
  dragLid = null;
}

// ====== SEARCH ======
function debounceSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadChannels(), 300);
}

// ====== LOGOUT ======
function logout() {
  // Basic auth logout: send request with wrong credentials to clear the session
  fetch('/admin', {
    headers: { 'Authorization': 'Basic ' + btoa('logout:logout') }
  }).catch(() => {
    window.location.href = '/admin';
  });
}

// ====== UTILS ======
function escHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
function escAttr(s) { return s.replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

// ====== KEYBOARD SHORTCUTS ======
document.addEventListener("keydown", e => {
  // Ctrl+A to select all channels when not focused on input
  if ((e.ctrlKey || e.metaKey) && e.key === "a" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "SELECT") {
    e.preventDefault();
    document.getElementById("selectAll").checked = true;
    toggleSelectAll(document.getElementById("selectAll"));
  }
});

// Enter key on new group input
document.getElementById("newGroupName").addEventListener("keydown", e => {
  if (e.key === "Enter") createGroup();
});

// ====== INIT ======
async function init() {
  try {
    // Normalize sort_orders first to fix any duplicate sort_order issues
    await api("POST", "/admin/api/channels/normalize");
    await loadGroups();
    await loadChannels();
  } catch (e) {
    toast("Failed to load data: " + e.message, "error");
  }
}
init();
</script>
</body>
</html>'''


# ============================================================
# ADMIN API ENDPOINTS
# ============================================================
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(auth: Optional[str] = Depends(admin_auth)):
    return HTMLResponse(ADMIN_HTML)


@app.get("/admin/api/groups")
async def api_get_groups(auth: Optional[str] = Depends(admin_auth)):
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
    # Count ungrouped
    c.execute("SELECT COUNT(*) as cnt FROM channels WHERE cid=0")
    ungrouped = c.fetchone()["cnt"]
    if ungrouped > 0:
        groups.append({"cid": 0, "name": "Ungrouped", "sort_order": 99999, "count": ungrouped})
    conn.close()
    return groups


@app.post("/admin/api/groups/create")
async def api_create_group(request: Request, auth: Optional[str] = Depends(admin_auth)):
    """Create a new group."""
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name required")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="Name too long")

    conn = get_db()
    c = conn.cursor()
    # Check duplicate
    c.execute("SELECT cid FROM categories WHERE name=?", (name,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="Group already exists")

    # Get max sort_order
    c.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 as nxt FROM categories")
    nxt = c.fetchone()["nxt"]
    c.execute("INSERT INTO categories(name, sort_order) VALUES(?, ?)", (name, nxt))
    cid = c.lastrowid
    conn.commit()
    conn.close()
    return {"success": True, "cid": cid, "name": name}


@app.post("/admin/api/groups/rename")
async def api_rename_group(request: Request, auth: Optional[str] = Depends(admin_auth)):
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
    # Also update the grp field on all channels in this group
    c.execute("UPDATE channels SET grp=? WHERE cid=?", (name, cid))
    conn.commit()
    conn.close()
    return {"success": True}


@app.post("/admin/api/groups/delete")
async def api_delete_group(request: Request, auth: Optional[str] = Depends(admin_auth)):
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

    # Move channels to ungrouped
    c.execute("UPDATE channels SET cid=0, grp='' WHERE cid=?", (cid,))
    # Delete category
    c.execute("DELETE FROM categories WHERE cid=?", (cid,))
    conn.commit()
    conn.close()
    return {"success": True}


@app.post("/admin/api/groups/reorder")
async def api_reorder_groups(request: Request, auth: Optional[str] = Depends(admin_auth)):
    """Reorder groups."""
    body = await request.json()
    items = body.get("groups", [])
    if not items:
        raise HTTPException(status_code=400, detail="groups array required")

    conn = get_db()
    c = conn.cursor()
    c.execute("BEGIN")
    for item in items:
        cid = item.get("cid")
        sort_order = item.get("sort_order")
        if cid is not None and sort_order is not None:
            c.execute("UPDATE categories SET sort_order=? WHERE cid=?", (sort_order, cid))
    conn.commit()
    conn.close()
    return {"success": True}


@app.get("/admin/api/channels")
async def api_get_channels(
    request: Request,
    group_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    auth: Optional[str] = Depends(admin_auth)
):
    """List channels with optional group filter and search."""
    conn = get_db()
    c = conn.cursor()

    if group_id is not None and group_id > 0:
        # Filter by group
        if search:
            c.execute("""
                SELECT lid, name, grp, cid, logo, url, hls, sort_order
                FROM channels
                WHERE cid=? AND (name LIKE ? OR grp LIKE ?)
                ORDER BY sort_order, name
            """, (group_id, f"%{search}%", f"%{search}%"))
        else:
            c.execute("""
                SELECT lid, name, grp, cid, logo, url, hls, sort_order
                FROM channels
                WHERE cid=?
                ORDER BY sort_order, name
            """, (group_id,))
    elif group_id == 0:
        # Ungrouped only
        if search:
            c.execute("""
                SELECT lid, name, grp, cid, logo, url, hls, sort_order
                FROM channels
                WHERE cid=0 AND (name LIKE ? OR grp LIKE ?)
                ORDER BY sort_order, name
            """, (f"%{search}%", f"%{search}%"))
        else:
            c.execute("""
                SELECT lid, name, grp, cid, logo, url, hls, sort_order
                FROM channels
                WHERE cid=0
                ORDER BY sort_order, name
            """)
    else:
        # All channels
        if search:
            c.execute("""
                SELECT ch.lid, ch.name, ch.grp, ch.cid, ch.logo, ch.url, ch.hls, ch.sort_order
                FROM channels ch
                LEFT JOIN categories cat ON ch.cid = cat.cid
                WHERE (ch.name LIKE ? OR ch.grp LIKE ?)
                ORDER BY COALESCE(cat.sort_order, 9999), ch.sort_order, ch.name
            """, (f"%{search}%", f"%{search}%"))
        else:
            c.execute("""
                SELECT ch.lid, ch.name, ch.grp, ch.cid, ch.logo, ch.url, ch.hls, ch.sort_order
                FROM channels ch
                LEFT JOIN categories cat ON ch.cid = cat.cid
                ORDER BY COALESCE(cat.sort_order, 9999), ch.sort_order, ch.name
            """)

    result = []
    for r in c.fetchall():
        result.append({
            "lid": r["lid"],
            "name": r["name"],
            "grp": r["grp"] or "",
            "cid": r["cid"] or 0,
            "logo": r["logo"] or "",
            "url": r["url"] or "",
            "hls": r["hls"] or "",
            "sort_order": r["sort_order"],
        })
    conn.close()
    return result


@app.post("/admin/api/channels/assign")
async def api_assign_channels(request: Request, auth: Optional[str] = Depends(admin_auth)):
    """
    Batch assign channels to a group.
    CRITICAL: Uses a single transaction to update ALL channels.
    Also normalizes sort_orders so channels get unique sequential order.
    """
    body = await request.json()
    lids = body.get("lids", [])
    cid = body.get("cid")

    if not lids or not isinstance(lids, list) or len(lids) == 0:
        raise HTTPException(status_code=400, detail="lids array required with at least 1 ID")
    if cid is None:
        raise HTTPException(status_code=400, detail="cid required")

    if cid == 0:
        # Ungroup
        target_grp = ""
    else:
        # Look up group name
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

    # CRITICAL FIX: Single transaction for ALL updates
    try:
        c.execute("BEGIN")
        for lid in lids:
            c.execute("UPDATE channels SET cid=?, grp=? WHERE lid=?", (cid, target_grp, lid))
        # Commit the group assignment first
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    # Verify the updates actually landed
    c.execute("SELECT COUNT(*) as cnt FROM channels WHERE cid=? AND lid IN ({})".format(
        ",".join("?" * len(lids))), [cid] + list(lids))
    saved_count = c.fetchone()["cnt"]

    if saved_count != len(lids):
        conn.close()
        raise HTTPException(status_code=500, detail=f"Only {saved_count}/{len(lids)} channels updated")

    # Normalize sort_orders for the target group:
    # Give each channel a unique sequential sort_order
    c.execute("SELECT lid FROM channels WHERE cid=? ORDER BY sort_order, name", (cid,))
    group_lids = [r["lid"] for r in c.fetchall()]
    c.execute("BEGIN")
    for idx, glid in enumerate(group_lids):
        c.execute("UPDATE channels SET sort_order=? WHERE lid=?", (idx + 1, glid))
    conn.commit()
    conn.close()

    return {"success": True, "updated": saved_count}


@app.post("/admin/api/channels/reorder")
async def api_reorder_channel(request: Request, auth: Optional[str] = Depends(admin_auth)):
    """Reorder a channel within its group (swap with neighbor).
    Uses name as tiebreaker when sort_orders are equal."""
    body = await request.json()
    lid = body.get("lid")
    direction = body.get("direction", "up")

    if not lid or direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="lid and direction required")

    conn = get_db()
    c = conn.cursor()

    # Get the channel (include name for tiebreaker)
    c.execute("SELECT lid, cid, sort_order, name FROM channels WHERE lid=?", (lid,))
    ch = c.fetchone()
    if not ch:
        conn.close()
        raise HTTPException(status_code=404, detail="Channel not found")

    ch_cid = ch["cid"]
    ch_sort = ch["sort_order"]
    ch_name = ch["name"]

    if direction == "up":
        # Find the channel just above in the same group
        # Use name as tiebreaker when sort_orders are equal
        c.execute("""
            SELECT lid, sort_order FROM channels
            WHERE cid=? AND lid != ? AND (
                sort_order < ? OR
                (sort_order = ? AND name < ?)
            )
            ORDER BY sort_order DESC, name DESC LIMIT 1
        """, (ch_cid, lid, ch_sort, ch_sort, ch_name))
    else:
        # Find the channel just below
        c.execute("""
            SELECT lid, sort_order FROM channels
            WHERE cid=? AND lid != ? AND (
                sort_order > ? OR
                (sort_order = ? AND name > ?)
            )
            ORDER BY sort_order ASC, name ASC LIMIT 1
        """, (ch_cid, lid, ch_sort, ch_sort, ch_name))

    neighbor = c.fetchone()
    if not neighbor:
        conn.close()
        return {"success": True, "swapped": False}

    # Swap sort_orders
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
async def api_move_channel(request: Request, auth: Optional[str] = Depends(admin_auth)):
    """Move a channel to a specific position (drag & drop target)."""
    body = await request.json()
    lid = body.get("lid")
    target_lid = body.get("target_lid")

    if not lid or not target_lid:
        raise HTTPException(status_code=400, detail="lid and target_lid required")

    conn = get_db()
    c = conn.cursor()

    # Get source channel
    c.execute("SELECT lid, cid, sort_order FROM channels WHERE lid=?", (lid,))
    src = c.fetchone()
    if not src:
        conn.close()
        raise HTTPException(status_code=404, detail="Source channel not found")

    # Get target channel
    c.execute("SELECT lid, cid, sort_order FROM channels WHERE lid=?", (target_lid,))
    tgt = c.fetchone()
    if not tgt:
        conn.close()
        raise HTTPException(status_code=404, detail="Target channel not found")

    src_cid = src["cid"]
    tgt_cid = tgt["cid"]

    # If different groups, just reassign (assign handles grp name)
    if src_cid != tgt_cid:
        try:
            c.execute("BEGIN")
            # Look up target group name
            c.execute("SELECT name FROM categories WHERE cid=?", (tgt_cid,))
            cat_row = c.fetchone()
            target_grp = cat_row["name"] if cat_row else ""

            # Get all sort_orders in target group, find insert position
            c.execute("SELECT sort_order FROM channels WHERE cid=? ORDER BY sort_order", (tgt_cid,))
            orders = [r["sort_order"] for r in c.fetchall()]
            target_order = tgt["sort_order"]

            # Shift everything at or after target position down by 1
            c.execute("UPDATE channels SET sort_order=sort_order+1 WHERE cid=? AND sort_order>=?", (tgt_cid, target_order))

            # Set the moved channel's position
            c.execute("UPDATE channels SET cid=?, grp=?, sort_order=? WHERE lid=?", (tgt_cid, target_grp, target_order, lid))
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            raise HTTPException(status_code=500, detail=f"DB error: {e}")
        conn.close()
        return {"success": True, "new_group": tgt_cid}
    else:
        # Same group: reorder by swapping sort_orders
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
async def api_ungroup_channels(request: Request, auth: Optional[str] = Depends(admin_auth)):
    """Remove channels from their group (set to ungrouped)."""
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

    # Verify
    c.execute("SELECT COUNT(*) as cnt FROM channels WHERE cid=0 AND lid IN ({})".format(
        ",".join("?" * len(lids))), list(lids))
    saved_count = c.fetchone()["cnt"]
    conn.close()

    return {"success": True, "ungrouped": saved_count}


@app.post("/admin/api/channels/normalize")
async def api_normalize_sort(auth: Optional[str] = Depends(admin_auth)):
    """Normalize sort_orders so every channel has a unique sequential order within its group.
    Fixes the root cause where all channels in a group had identical sort_order values."""
    conn = get_db()
    c = conn.cursor()

    # Get all channels grouped by cid, ordered by name for stable ordering
    c.execute("SELECT lid, cid FROM channels ORDER BY cid, sort_order, name")
    rows = c.fetchall()

    current_cid = None
    order = 0
    updated = 0
    c.execute("BEGIN")
    for row in rows:
        if row["cid"] != current_cid:
            current_cid = row["cid"]
            order = 1
        c.execute("UPDATE channels SET sort_order=? WHERE lid=?", (order, row["lid"]))
        order += 1
        updated += 1
    conn.commit()
    conn.close()

    return {"success": True, "normalized": updated}
