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
        
        # Build EXTINF line
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
    # Return empty gz
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
        # Try to find channel by name and get its picon URL
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
    # Fallback: return 1x1 transparent PNG
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

    # Check resolve cache first
    cache_key = str(ch_id)
    if cache_key in state.RESOLVE_CACHE:
        cached = state.RESOLVE_CACHE[cache_key]
        if cached.get("expires", 0) > time.time():
            return await proxy_stream(cached["url"], ch_id)

    # Try HLS URL from catalog first
    hls = ch.get("hls", "")
    if hls:
        state.add_log(f"[{ch_id}] Catalog HLS resolve: {hls[:80]}")
        resolved = await state.resolve_mediahubmx(hls)
        if resolved:
            state.RESOLVE_CACHE[cache_key] = {"url": resolved, "expires": time.time() + 300}
            return await proxy_stream(resolved, ch_id)

    # Try resolving the original URL
    url = ch.get("url", "")
    if url:
        resolved = await state.resolve_mediahubmx(url)
        if resolved:
            state.RESOLVE_CACHE[cache_key] = {"url": resolved, "expires": time.time() + 300}
            return await proxy_stream(resolved, ch_id)

    # Fallback: try direct proxy of original URL
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
        "id": c["id"], "has_override": c["name"].upper().strip() in overrides
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

ADMIN_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VxParser Admin</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
h1{font-size:22px;margin-bottom:4px}h1 span{color:#58a6ff}
.sub{color:#8b949e;font-size:13px;margin-bottom:16px}
.stats{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.st{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;flex:1;min-width:100px}
.st b{font-size:20px;display:block}.st small{color:#8b949e;font-size:11px}
.toolbar{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
input[type=text],select{background:#161b22;border:1px solid #30363d;color:#e6edf3;padding:8px 12px;border-radius:6px;font-size:14px;outline:none}
input[type=text]:focus,select:focus{border-color:#58a6ff}
input[type=text]{flex:1;min-width:180px}
.btn{background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:13px;transition:.15s}
.btn:hover{background:#30363d}.btn-g{background:#238636;border-color:#2ea043}.btn-g:hover{background:#2ea043}
.btn-r{background:#da3633;border-color:#f85149}.btn-r:hover{background:#f85149}
.btn-b{background:#1f6feb;border-color:#388bfd}.btn-b:hover{background:#388bfd}
.tbl{background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.tbl table{width:100%;border-collapse:collapse}
.tbl th{background:#21262d;padding:10px 12px;text-align:left;font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;position:sticky;top:0;z-index:2}
.tbl td{padding:6px 10px;border-top:1px solid #21262d;font-size:13px}
.tbl .scr{max-height:65vh;overflow-y:auto}
tr.changed{background:#1c2d1c}tr.changed td{border-top-color:#238636}
.gsel{background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:4px 6px;border-radius:4px;font-size:12px;width:170px}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700}
.b-tr{background:#1a2332;color:#58a6ff}.b-de{background:#2a1a1a;color:#f85149}
.ov{color:#d29922;font-size:11px}
.toast{position:fixed;bottom:20px;right:20px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 20px;transform:translateY(80px);opacity:0;transition:.3s;z-index:99;font-size:14px}
.toast.show{transform:translateY(0);opacity:1}.toast.ok{border-color:#3fb950}.toast.err{border-color:#f85149}
#fileIn{display:none}
.empty{text-align:center;padding:40px;color:#8b949e}
.hdr{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
</style>
</head>
<body>
<div class="wrap">
<div class="hdr">
<div><h1>&#x1f4fa; VxParser <span>Admin</span></h1>
<p class="sub">Kanallar\u0131 gruplar\u0131na ay\u0131r, de\u011fi\u015fiklikleri kaydet ve yedekle</p></div>
</div>
<div class="stats">
<div class="st"><b id="sTotal">-</b><small>Toplam Kanal</small></div>
<div class="st"><b id="sGroups">-</b><small>Grup</small></div>
<div class="st"><b id="sOverride">-</b><small>Override</small></div>
<div class="st"><b id="sChanged">0</b><small>Kaydedilmemi\u015f</small></div>
</div>
<div class="toolbar">
<input type="text" id="search" placeholder="&#x1f50d; Kanal ara..." oninput="render()">
<select id="gFilter" onchange="render()"><option value="">T\u00fcm Gruplar</option></select>
<button class="btn btn-g" onclick="save()">&#x1f4be; Kaydet</button>
</div>
<div class="tbl"><div class="scr">
<table>
<thead><tr><th>Kanal Ad\u0131</th><th>\u00dclke</th><th>Mevcut Grup</th><th>Yeni Grup</th><th></th></tr></thead>
<tbody id="list"></tbody>
</table>
</div></div>
<div class="acts">
<button class="btn btn-b" onclick="doExport()">&#x1f4e4; D\u0131\u015fa Aktar JSON</button>
<button class="btn" onclick="document.getElementById('fileIn').click()">&#x1f4e5; \u0130\u00e7e Aktar JSON</button>
<button class="btn" onclick="copyJson()">&#x1f4cb; Kopyala</button>
<button class="btn btn-r" onclick="doReset()">&#x1f504; S\u0131f\u0131rla</button>
</div>
<input type="file" id="fileIn" accept=".json" onchange="doImport(event)">
</div>
<div class="toast" id="toast"></div>
<script>
const GRPS=["TR ULUSAL","TR SPOR","TR SINEMA","TR SINEMA VOD","TR DIZI","TR 7/24 DIZI","TR BELGESEL","TR COCUK","TR MUZIK","TR HABER","TR DINI","TR YEREL","TR RADYO","TR 4K","TR 8K","TR RAW","DE VOLLPROGRAMM","DE NACHRICHTEN","DE DOKU","DE KINDER","DE FILM","DE MUSIK","DE SPORT","DE SONSTIGE"];
let channels=[],overrides={},changes={};

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function escA(s){return String(s).replace(/'/g,"\\\\'").replace(/"/g,'&quot;')}

async function load(){
  try{
    const r=await fetch('/api/admin/channels');channels=await r.json();
    const r2=await fetch('/api/admin/overrides');overrides=await r2.json();
    changes={};
    fillGroupFilter();render();
  }catch(e){toast('Veri yuklenemedi: '+e,'err')}
}

function fillGroupFilter(){
  const sel=document.getElementById('gFilter');
  const seen=new Set();channels.forEach(c=>seen.add(c.grp));
  sel.innerHTML='<option value="">Tüm Gruplar</option>';
  [...seen].sort().forEach(g=>{sel.innerHTML+=`<option value="${g}">${g}</option>`});
}

function render(){
  const q=document.getElementById('search').value.toUpperCase();
  const gf=document.getElementById('gFilter').value;
  let fl=channels.filter(c=>{
    if(gf&&c.grp!==gf)return false;
    if(q&&!c.name.toUpperCase().includes(q))return false;
    return true;
  });
  const tb=document.getElementById('list');
  if(!fl.length){tb.innerHTML='<tr><td colspan="5" class="empty">Kanal bulunamadı</td></tr>';updStats();return}
  let h='';
  fl.forEach(c=>{
    const k=c.name,orig=c.grp;
    const isOvr=overrides.hasOwnProperty(k.toUpperCase());
    const isChg=changes.hasOwnProperty(k);
    const cur=isChg?changes[k]:orig;
    const rowCls=isChg?'changed':'';
    const ovMark=isOvr&&!isChg?'<span class="ov">&#x270e; override</span>':'';
    const chgMark=isChg?'<span class="ov" style="color:#3fb950">&#x270e; değişti</span>':'';
    h+=`<tr class="${rowCls}"><td>${esc(c.name)} ${ovMark}${chgMark}</td><td><span class="badge ${c.country==='TR'?'b-tr':'b-de'}">${c.country}</span></td><td>${esc(orig)}</td><td><select class="gsel" onchange="chg('${escA(c.name)}',this.value)">${GRPS.map(g=>`<option value="${g}"${cur===g?' selected':''}>${g}</option>`).join('')}</select></td><td>${isChg?'<button class="btn" style="padding:3px 8px;font-size:11px" onclick="revert(\\''+escA(c.name)+'\\')">Geri Al</button>':''}</td></tr>`;
  });
  tb.innerHTML=h;
  updStats();
}

function chg(name,val){
  const c=channels.find(x=>x.name===name);if(!c)return;
  if(val===c.grp&&!overrides[name.toUpperCase()])delete changes[name];
  else changes[name]=val;
  render();
}

function revert(name){delete changes[name];render()}

async function save(){
  const n=Object.keys(changes).length;
  if(!n){toast('Kaydedilecek değişiklik yok');return}
  try{
    const r=await fetch('/api/admin/overrides',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(changes)});
    if(r.ok){overrides={...overrides};Object.entries(changes).forEach(([k,v])=>{overrides[k.toUpperCase()]=v});const c=channels.find(x=>x.name===Object.keys(changes)[0]);changes={};render();toast(n+' kanal kaydedildi!','ok')}
    else toast('Hata!','err');
  }catch(e){toast('Hata: '+e,'err')}
}

async function doExport(){
  try{
    const r=await fetch('/api/admin/overrides/export');
    const blob=await r.blob();const a=document.createElement('a');
    a.href=URL.createObjectURL(blob);a.download='vxparser-overrides.json';a.click();
    toast('Dosya indirildi','ok');
  }catch(e){toast('Export hatası','err')}
}

function doImport(ev){
  const f=ev.target.files[0];if(!f)return;
  const rd=new FileReader();
  rd.onload=async(e)=>{
    try{
      const d=JSON.parse(e.target.result);
      const r=await fetch('/api/admin/overrides/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
      if(r.ok){const j=await r.json();toast(j.imported+' override yuklendi','ok');load()}
      else toast('Import hatası','err');
    }catch(ex){toast('JSON parse hatası','err')}
  };rd.readAsText(f);ev.target.value='';
}

function copyJson(){
  const t=JSON.stringify(overrides,null,2);navigator.clipboard.writeText(t).then(()=>toast('Kopyalandı!','ok')).catch(()=>toast('Kopyalama hatası','err'));
}

async function doReset(){
  if(!confirm('Tüm override\'ları silmek istediğinize emin misiniz?'))return;
  try{
    await fetch('/api/admin/overrides',{method:'DELETE'});
    overrides={};changes={};render();toast('Tüm override\'lar silindi','ok');
  }catch(e){toast('Hata','err')}
}

function updStats(){
  document.getElementById('sTotal').textContent=channels.length;
  const g=new Set(channels.map(c=>c.grp));
  document.getElementById('sGroups').textContent=g.size;
  document.getElementById('sOverride').textContent=Object.keys(overrides).length;
  const nc=Object.keys(changes).length;
  const el=document.getElementById('sChanged');el.textContent=nc;
  el.style.color=nc?'#d29922':'';
}

function toast(msg,type){
  const t=document.getElementById('toast');t.textContent=msg;t.className='toast show '+(type||'');
  setTimeout(()=>t.className='toast',2500);
}

load();
</script>
</body>
</html>"""
