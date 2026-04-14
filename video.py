"""
video.py - FastAPI v11.0.0
IPTV proxy + Mobil Admin Panel
state.py'yi import eder, server.py'yi import ETMEZ.

v11.0 - Sifirdan yazilmis mobil admin panel
        Kasma fix: resolve cache 45dk, direkt redirect
"""
import os, sqlite3, threading, re
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse, Response, HTMLResponse
import state

app = FastAPI(title="VxParser IPTV Proxy", version="11.0.0")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "admin123")
ORD = "COALESCE(cat.sort_order,9999), c.sort_order, c.name"

def get_db():
    conn = sqlite3.connect(state.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_host(r: Request) -> str:
    p = r.headers.get("x-forwarded-proto", "https")
    h = r.headers.get("host", "localhost:10000")
    return f"{p}://{h}"

# ============ STATUS ============
@app.get("/")
async def root():
    return {"status": "ready" if state.DATA_READY else "loading", "error": state.STARTUP_ERROR,
            "load_time": round(state.LOAD_TIME,1) if state.DATA_READY else None,
            "message": "Hazir!" if state.DATA_READY else "Yukleniyor..."}

@app.get("/health")
async def health(): return {"status": "ok"}

@app.get("/ping")
@app.get("/pong")
async def pp(): return {"status": "pong", "ready": state.DATA_READY}

@app.get("/api/status")
async def api_status():
    c = get_db(); cu = c.cursor()
    cu.execute("SELECT COUNT(*) FROM channels"); total = cu.fetchone()[0]
    cu.execute("SELECT COUNT(*) FROM categories"); cats = cu.fetchone()[0]
    cu.execute("SELECT COUNT(*) FROM channels WHERE hls!='' AND hls IS NOT NULL"); hls = cu.fetchone()[0]
    c.close()
    return {"status": "ready" if state.DATA_READY else "loading", "data_ready": state.DATA_READY,
            "error": state.STARTUP_ERROR, "load_time": round(state.LOAD_TIME,1) if state.DATA_READY else None,
            "available_channels": total, "available_categories": cats, "hls_channels": hls,
            "vavoo_token": bool(state._vavoo_sig), "lokke_token": bool(state._watched_sig),
            "resolve_cache": state.get_resolve_cache_info(), "startup_logs": state.STARTUP_LOGS}

@app.get("/debug")
async def debug():
    return {"data_ready": state.DATA_READY, "vavoo_token": bool(state._vavoo_sig),
            "lokke_token": bool(state._watched_sig), "resolve_cache": state.get_resolve_cache_info(),
            "startup_logs": state.STARTUP_LOGS, "db_path": state.DB_PATH}

# ============ CHANNEL ============
@app.get("/test/{sid}")
async def test_ch(sid: str):
    c = get_db(); cu = c.cursor()
    cu.execute("SELECT lid,name,url,hls,grp FROM channels WHERE lid=?",(sid,))
    ch = cu.fetchone(); c.close()
    if not ch: return {"error": f"Kanal {sid} yok"}
    url, method = state.resolve_channel(sid)
    return {"lid": ch["lid"], "name": ch["name"], "url": ch["url"], "hls": ch["hls"],
            "grp": ch["grp"], "resolve_method": method, "resolved_url": url}

@app.get("/channel/{sid}")
async def play_ch(sid: str):
    url, _ = state.resolve_channel(sid)
    if url: return RedirectResponse(url=url, status_code=302)
    raise HTTPException(503, "Kanal cozumlenemedi")

# ============ M3U / XTREAM ============
@app.get("/get.php")
async def get_m3u(request: Request, username: str=Query("admin"), password: str=Query("admin")):
    host = get_host(request); c = get_db(); cu = c.cursor()
    cu.execute(f"SELECT c.lid,c.name,c.logo,COALESCE(cat.name,'Sonstige') as gn FROM channels c LEFT JOIN categories cat ON c.cid=cat.cid ORDER BY {ORD}")
    lines = [f'#EXTM3U url-tvg="{host}/epg.xml"']
    for r in cu.fetchall():
        lines.append(f'#EXTINF:-1 tvg-id="{r["lid"]}" tvg-logo="{r["logo"] or ""}" group-title="{r["gn"]}",{r["name"]}')
        lines.append(f"{host}/channel/{r['lid']}")
    c.close()
    return PlainTextResponse("\n".join(lines), media_type="audio/x-mpegurl")

@app.get("/epg.xml")
async def epg():
    x = state.get_epg_data()
    return Response(content=x or "<?xml version='1.0'?><tv/>", media_type="application/xml")

@app.get("/player_api.php")
async def xtream(request: Request, action: str=Query(None)):
    host = get_host(request); c = get_db(); cu = c.cursor()
    if action == "get_live_categories":
        cu.execute("SELECT cid as category_id,name as category_name FROM categories ORDER BY sort_order")
        d = [dict(r) for r in cu.fetchall()]; c.close(); return d
    elif action == "get_live_streams":
        cu.execute(f"SELECT c.lid as stream_id,c.name,c.logo as stream_icon,c.cid as category_id,COALESCE(cat.name,'Sonstige') as category_name FROM channels c LEFT JOIN categories cat ON c.cid=cat.cid ORDER BY {ORD}")
        d = []
        for r in cu.fetchall():
            row = dict(r); row["stream_url"] = f"{host}/channel/{row['stream_id']}"; d.append(row)
        c.close(); return d
    else:
        cu.execute("SELECT COUNT(*) FROM channels"); t = cu.fetchone()[0]
        cu.execute("SELECT COUNT(*) FROM categories"); ca = cu.fetchone()[0]
        c.close()
        return {"user_info":{"username":"admin","status":"Active"},"available_channels":t,"available_categories":ca}

# ============ RELOAD ============
@app.get("/reload")
async def reload():
    state.DATA_READY=False; state.STARTUP_ERROR=None; state.STARTUP_LOGS.clear(); state.clear_resolve_cache()
    def do():
        import server
        try: server.init_db(); server.fetch_vavoo_channels(); server.fetch_hls_links(); server.remap_groups(); state.DATA_READY=True
        except Exception as e: state.STARTUP_ERROR=str(e)
    threading.Thread(target=do, daemon=True).start()
    return {"status":"reloading","message":"Yukleniyor..."}

@app.get("/stats")
async def stats():
    c=get_db();cu=c.cursor()
    cu.execute("SELECT COUNT(*) FROM channels");t=cu.fetchone()[0]
    cu.execute("SELECT COUNT(*) FROM categories");ca=cu.fetchone()[0]
    cu.execute("SELECT cat.cid,COALESCE(cat.name,'?') as g,COUNT(*) as n FROM channels c LEFT JOIN categories cat ON c.cid=cat.cid GROUP BY c.cid ORDER BY MIN(cat.sort_order)")
    grps=[{"cid":r[0],"group":r[1],"count":r[2]} for r in cu.fetchall()]
    cu.execute("SELECT COUNT(*) FROM channels WHERE hls!='' AND hls IS NOT NULL");hls=cu.fetchone()[0]
    c.close()
    return {"total_channels":t,"total_categories":ca,"hls_channels":hls,"vavoo_token":bool(state._vavoo_sig),
            "lokke_token":bool(state._watched_sig),"groups":grps,"data_ready":state.DATA_READY}

# ============ ADMIN API ============
@app.post("/api/admin/login")
async def adm_login(request:Request):
    body=await request.json()
    return {"ok": body.get("password")==ADMIN_PASSWORD}

@app.get("/api/admin/groups")
async def adm_groups():
    c=get_db();cu=c.cursor()
    cu.execute("SELECT cat.cid,cat.name,cat.sort_order,(SELECT COUNT(*) FROM channels WHERE channels.cid=cat.cid) as cnt FROM categories cat ORDER BY cat.sort_order")
    g=[{"cid":r["cid"],"name":r["name"],"sort_order":r["sort_order"],"count":r["cnt"]} for r in cu.fetchall()]
    c.close(); return {"groups":g}

@app.post("/api/admin/groups")
async def adm_grp_add(request:Request):
    body=await request.json(); name=body.get("name","").strip()
    if not name: return {"ok":False,"error":"Bos"}
    c=get_db();cu=c.cursor()
    cu.execute("SELECT MAX(sort_order) FROM categories"); mx=cu.fetchone()[0] or 0
    cu.execute("INSERT INTO categories(name,sort_order) VALUES(?,?)",(name,mx+1))
    c.commit(); cid=cu.lastrowid; c.close()
    return {"ok":True,"cid":cid,"name":name}

@app.delete("/api/admin/groups/{cid}")
async def adm_grp_del(cid:int):
    c=get_db();cu=c.cursor()
    cu.execute("SELECT cid FROM categories WHERE name='DE SONSTIGE' LIMIT 1"); d=cu.fetchone()
    dcid=d[0] if d else 0
    cu.execute("UPDATE channels SET cid=?,grp='DE SONSTIGE' WHERE cid=?",(dcid,cid)); mv=cu.rowcount
    cu.execute("DELETE FROM categories WHERE cid=?",(cid,)); c.commit(); c.close()
    return {"ok":True,"moved":mv}

@app.put("/api/admin/groups/{cid}/move")
async def adm_grp_move(cid:int,request:Request):
    body=await request.json(); d=body.get("direction","up")
    c=get_db();cu=c.cursor()
    cu.execute("SELECT cid,sort_order FROM categories WHERE cid=?",(cid,)); row=cu.fetchone()
    if not row: c.close(); return {"ok":False,"error":"Yok"}
    so=row["sort_order"]
    if d=="up": cu.execute("SELECT cid,sort_order FROM categories WHERE sort_order<? ORDER BY sort_order DESC LIMIT 1",(so,))
    else: cu.execute("SELECT cid,sort_order FROM categories WHERE sort_order>? ORDER BY sort_order ASC LIMIT 1",(so,))
    sw=cu.fetchone()
    if not sw: c.close(); return {"ok":False}
    cu.execute("UPDATE categories SET sort_order=? WHERE cid=?",(sw["sort_order"],cid))
    cu.execute("UPDATE categories SET sort_order=? WHERE cid=?",(so,sw["cid"]))
    c.commit(); c.close(); return {"ok":True}

@app.get("/api/admin/channels")
async def adm_ch():
    c=get_db();cu=c.cursor()
    cu.execute(f"SELECT c.lid,c.name,c.url,c.hls,c.logo,c.cid,c.sort_order,COALESCE(cat.name,'Sonstige') as grp FROM channels c LEFT JOIN categories cat ON c.cid=cat.cid ORDER BY {ORD}")
    ch=[{"lid":r["lid"],"name":r["name"],"grp":r["grp"],"cid":r["cid"],"sort_order":r["sort_order"],
         "logo":r["logo"] or "","url":r["url"] or "","has_hls":bool(r["hls"])} for r in cu.fetchall()]
    c.close(); return {"channels":ch,"total":len(ch)}

@app.put("/api/admin/channels/{lid}/move")
async def adm_ch_move(lid:int,request:Request):
    body=await request.json(); d=body.get("direction","up")
    c=get_db();cu=c.cursor()
    cu.execute("SELECT lid,cid,sort_order FROM channels WHERE lid=?",(lid,)); row=cu.fetchone()
    if not row: c.close(); return {"ok":False}
    so=row["sort_order"]; cid=row["cid"]
    if d=="up": cu.execute("SELECT lid,sort_order FROM channels WHERE cid=? AND sort_order<? ORDER BY sort_order DESC LIMIT 1",(cid,so))
    else: cu.execute("SELECT lid,sort_order FROM channels WHERE cid=? AND sort_order>? ORDER BY sort_order ASC LIMIT 1",(cid,so))
    sw=cu.fetchone()
    if not sw: c.close(); return {"ok":False}
    cu.execute("UPDATE channels SET sort_order=? WHERE lid=?",(sw["sort_order"],lid))
    cu.execute("UPDATE channels SET sort_order=? WHERE lid=?",(so,sw["lid"]))
    c.commit(); c.close(); return {"ok":True}

@app.put("/api/admin/channels/{lid}/group")
async def adm_ch_grp(lid:int,request:Request):
    body=await request.json(); ncid=body.get("cid",0)
    c=get_db();cu=c.cursor()
    cu.execute("SELECT name FROM categories WHERE cid=?",(ncid,)); cat=cu.fetchone()
    if not cat: c.close(); return {"ok":False,"error":"Grup yok"}
    cu.execute("SELECT MAX(sort_order) FROM channels WHERE cid=?",(ncid,)); mx=cu.fetchone()[0] or 0
    cu.execute("UPDATE channels SET cid=?,grp=?,sort_order=? WHERE lid=?",(ncid,cat["name"],mx+1,lid))
    c.commit(); c.close(); return {"ok":True,"new_group":cat["name"]}

@app.get("/api/admin/resolve/{sid}")
async def adm_resolve(sid:str):
    url,method=state.resolve_channel(sid)
    return {"channel_id":sid,"resolve_method":method,"resolved_url":url,"success":bool(url),"resolve_cache":state.get_resolve_cache_info()}

@app.post("/api/admin/cache/clear")
async def adm_cache():
    state.clear_resolve_cache(); return {"ok":True}

# ============ ADMIN PANEL ============
@app.get("/admin")
async def admin_page():
    return HTMLResponse(ADMIN_HTML)

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5,user-scalable=yes">
<title>VxParser Admin</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--p:#8957e5;--g:#3fb950;--r:#f85149;--y:#d29922;--b:#58a6ff;--t:#e6edf3;--d:#8b949e}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--t);font-size:14px;-webkit-tap-highlight-color:transparent}
html{scroll-behavior:smooth}

.top{background:var(--card);padding:12px 16px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:50}
.top h1{font-size:16px;font-weight:800;background:linear-gradient(135deg,var(--p),#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.top .v{font-size:10px;color:var(--d);margin-left:auto}

.tabs{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--border);display:flex;z-index:50;padding-bottom:env(safe-area-inset-bottom)}
.tab{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 2px 6px;border:none;background:none;color:var(--d);font-size:9px;font-weight:600;cursor:pointer;-webkit-tap-highlight-color:transparent}
.tab.on{color:var(--p)}
.tab.on::before{content:'';position:absolute;top:0;left:50%;transform:translateX(-50%);width:28px;height:2px;background:var(--p);border-radius:2px}
.tab svg{width:18px;height:18px;fill:currentColor}

.wrap{padding:10px 10px 120px;max-width:800px;margin:0 auto}
.pg{display:none}.pg.on{display:block}

.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:8px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:8px 0}
.st{text-align:center;padding:12px 6px;border-radius:8px;background:var(--card);border:1px solid var(--border)}
.st b{display:block;font-size:20px;font-weight:800}
.st small{font-size:9px;color:var(--d);text-transform:uppercase;letter-spacing:.5px}
.c-g{color:var(--g)}.c-r{color:var(--r)}.c-y{color:var(--y)}.c-b{color:var(--b)}.c-p{color:var(--p)}

.dot{width:10px;height:10px;border-radius:50%;display:inline-block;flex-shrink:0}
.dot.ok{background:var(--g);box-shadow:0 0 8px #3fb95060}
.dot.no{background:var(--r);box-shadow:0 0 8px #f8514960}
.dot.ld{background:var(--y);animation:pls 1.5s infinite}
@keyframes pls{0%,100%{opacity:1}50%{opacity:.3}}

.sr{display:flex;align-items:center;gap:8px;padding:10px 14px;background:var(--card);border:1px solid var(--border);border-radius:10px;margin-bottom:8px;font-size:13px}

input,select{padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--t);font-size:14px;outline:none;-webkit-appearance:none;appearance:none}
input:focus,select:focus{border-color:var(--p)}

.si{display:flex;gap:6px;margin-bottom:8px}
.si input{flex:1;min-width:0}
.si select{width:auto;min-width:90px}

.ci{background:var(--card);border:1px solid var(--border);border-radius:10px;margin-bottom:6px;overflow:hidden}
.cm{display:flex;align-items:center;gap:8px;padding:10px 12px}
.cl{width:32px;height:32px;border-radius:6px;object-fit:contain;background:var(--bg);flex-shrink:0;border:1px solid var(--border)}
.cn{flex:1;min-width:0}
.cn b{display:block;font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cn small{color:var(--d);font-size:10px}
.badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:9px;font-weight:700;margin-left:4px}
.badge.h{background:#3fb95015;color:var(--g);border:1px solid #3fb95030}
.badge.u{background:#58a6ff15;color:var(--b);border:1px solid #58a6ff30}
.badge.n{background:#f8514915;color:var(--r);border:1px solid #f8514930}
.ex{width:36px;height:36px;border:none;background:none;color:var(--d);font-size:18px;cursor:pointer;border-radius:6px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.ex:active{background:var(--border)}

.ca{display:none;padding:8px 12px 12px;border-top:1px solid var(--border)}
.ca.open{display:block}
.cr{display:flex;gap:6px;margin-bottom:6px;align-items:center}
.cr label{font-size:10px;color:var(--d);min-width:36px;font-weight:600}
.btn{padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--t);font-size:12px;font-weight:600;cursor:pointer;flex:1;text-align:center;min-height:40px;display:flex;align-items:center;justify-content:center;gap:4px;-webkit-tap-highlight-color:transparent}
.btn:active{background:var(--p);color:#fff;border-color:var(--p)}
.btn.t{border-color:var(--b);color:var(--b)}.btn.t:active{background:var(--b);color:#fff}
.btn.sm{flex:0;padding:10px 12px}
.btn.red{border-color:#f8514940;color:var(--r)}.btn.red:active{background:var(--r);color:#fff}
.btn.green{border-color:#3fb95040;color:var(--g)}.btn.green:active{background:var(--g);color:#fff}

.ca select{flex:1;min-height:40px;font-size:12px}

.gi{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin-bottom:6px;display:flex;align-items:center;gap:8px}
.ga{display:flex;flex-direction:column;gap:3px;flex-shrink:0}
.gf{flex:1;min-width:0}
.gf b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gf small{color:var(--d);font-size:10px}
.gc{font-size:20px;font-weight:900;color:var(--p);min-width:32px;text-align:center;flex-shrink:0}
.ab{width:32px;height:28px;border-radius:5px;border:1px solid var(--border);background:none;color:var(--d);display:flex;align-items:center;justify-content:center;font-size:12px;cursor:pointer}
.ab:active{background:var(--p);color:#fff;border-color:var(--p)}

.lnk{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:8px}
.lnk label{font-size:9px;color:var(--d);text-transform:uppercase;letter-spacing:.5px;display:block;margin-bottom:6px;font-weight:700}
.lk{display:flex;gap:6px}
.lk input{flex:1;padding:10px;font-size:11px;font-family:monospace}
.lk button{padding:10px 14px;border-radius:8px;border:none;background:var(--p);color:#fff;font-size:11px;font-weight:700;cursor:pointer;white-space:nowrap;min-height:40px}

.log{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px;max-height:70vh;overflow-y:auto;font-family:monospace;font-size:10px;line-height:1.8;-webkit-overflow-scrolling:touch;word-break:break-all}
.l{color:var(--d)}.l.ok{color:var(--g)}.l.er{color:var(--r)}.l.w{color:var(--y)}

.login{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.lbox{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:32px 24px;width:100%;max-width:340px;text-align:center}
.lbox h2{background:linear-gradient(135deg,var(--p),#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;font-size:24px;margin-bottom:4px}
.lbox p{color:var(--d);margin-bottom:20px;font-size:12px}
.lbox input{width:100%;padding:14px;margin-bottom:14px;font-size:15px}
.lbox button{width:100%;padding:14px;border-radius:10px;border:none;background:linear-gradient(135deg,var(--p),#7c3aed);color:#fff;font-size:15px;font-weight:800;cursor:pointer;min-height:48px}
.lerr{color:var(--r);font-size:12px;margin-top:10px;min-height:18px}

.toast{position:fixed;top:12px;left:50%;transform:translateX(-50%) translateY(-20px);background:var(--card);border:1px solid var(--g);border-radius:10px;padding:10px 20px;color:var(--g);font-size:13px;font-weight:700;z-index:999;opacity:0;transition:all .3s;pointer-events:none;max-width:90%;text-align:center}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.err{border-color:var(--r);color:var(--r)}

.act{position:fixed;bottom:50px;left:0;right:0;background:var(--card);border-top:1px solid var(--border);display:flex;gap:5px;padding:6px 10px;z-index:49}

@media(min-width:768px){
.wrap{padding:14px 18px 80px}
.tabs{position:static;border-top:1px solid var(--border)}
.tab{flex-direction:row;gap:6px;padding:10px 14px;font-size:11px}
.tab.on::before{display:none}.tab.on{background:#8957e515}
.tab svg{width:16px;height:16px}
.act{position:static;border-bottom:1px solid var(--border)}
.grid{grid-template-columns:repeat(4,1fr)}
}

::-webkit-scrollbar{width:3px}::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
</head>
<body>

<div id="LP" class="login">
<div class="lbox">
<h2>VxParser</h2>
<p>Admin Panel v11</p>
<input type="password" id="pw" placeholder="Sifre" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()">
<button onclick="doLogin()">Giris Yap</button>
<div id="LE" class="lerr"></div>
</div>
</div>

<div id="AP" style="display:none">
<div class="top"><h1>VxParser</h1><span class="v">v3.7</span></div>
<div class="wrap">

<div id="p0" class="pg on">
<div class="sr" id="sR"><div class="dot ld" id="sD"></div><span id="sT">Yukleniyor...</span></div>
<div class="grid" id="sG"></div>
</div>

<div id="p1" class="pg">
<div class="si">
<input id="cQ" placeholder="Kanal ara..." oninput="rC()" type="search">
<select id="cG" onchange="rC()"><option value="">Tum Gruplar</option></select>
<select id="cT" onchange="rC()"><option value="">Tum</option><option value="hls">HLS</option><option value="no">HLS Yok</option></select>
</div>
<div id="cN" style="font-size:11px;color:var(--d);margin-bottom:6px;padding-left:2px"></div>
<div id="cL"></div>
</div>

<div id="p2" class="pg">
<div class="si">
<input id="nG" placeholder="Yeni grup adi..." type="text" onkeydown="if(event.key==='Enter')aG()">
<button class="btn green" onclick="aG()" style="flex:0;min-width:60px">+ Ekle</button>
</div>
<div id="gL"></div>
</div>

<div id="p3" class="pg">
<div class="lnk"><label>M3U Playlist</label><div class="lk"><input id="l1" readonly><button onclick="cp('l1')">Kopyala</button></div></div>
<div class="lnk"><label>Xtream Codes API</label><div class="lk"><input id="l2" readonly><button onclick="cp('l2')">Kopyala</button></div></div>
<div class="lnk"><label>EPG (XMLTV)</label><div class="lk"><input id="l3" readonly><button onclick="cp('l3')">Kopyala</button></div></div>
</div>

<div id="p4" class="pg">
<div class="sr"><span style="color:var(--d);font-size:11px">Baslangic loglari</span></div>
<div id="lB" class="log"></div>
</div>

</div>

<div class="act">
<button class="btn" onclick="doReload()">Reload</button>
<button class="btn" onclick="doCache()">Cache</button>
<button class="btn red" onclick="doOut()">Cikis</button>
</div>

<div class="tabs">
<button class="tab on" onclick="go(0,this)"><svg viewBox="0 0 24 24"><path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"/></svg>Durum</button>
<button class="tab" onclick="go(1,this)"><svg viewBox="0 0 24 24"><path d="M21 6H3a1 1 0 00-1 1v10a1 1 0 001 1h18a1 1 0 001-1V7a1 1 0 00-1-1zm-8 5H11v4h2v-4zm4 0h-2v4h2v-4zM9 11H7v4h2v-4z"/></svg>Kanallar</button>
<button class="tab" onclick="go(2,this)"><svg viewBox="0 0 24 24"><path d="M4 8h4V4H4v4zm6 12h4v-4h-4v4zm-6 0h4v-4H4v4zm0-6h4v-4H4v4zm6 0h4v-4h-4v4zm6-10v4h4V4h-4zm-6 4h4V4h-4v4zm6 6h4v-4h-4v4zm0 6h4v-4h-4v4z"/></svg>Gruplar</button>
<button class="tab" onclick="go(3,this)"><svg viewBox="0 0 24 24"><path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z"/></svg>Linkler</button>
<button class="tab" onclick="go(4,this)"><svg viewBox="0 0 24 24"><path d="M20 8h-3V4H3c-1.1 0-2 .9-2 2v11h2c0 1.66 1.34 3 3 3s3-1.34 3-3h6c0 1.66 1.34 3 3 3s3-1.34 3-3h2v-5l-3-4z"/></svg>Loglar</button>
</div>
</div>

<div id="MS" class="modal-bg" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);z-index:200;align-items:flex-end;justify-content:center" onclick="if(event.target===this)this.style.display='none'">
<div style="background:var(--card);border-radius:16px 16px 0 0;padding:20px 16px;width:100%;max-width:460px;max-height:70vh;overflow-y:auto;-webkit-overflow-scrolling:touch">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
<h3 style="color:var(--p);font-size:15px;font-weight:800" id="mT">Resolve Test</h3>
<button onclick="document.getElementById('MS').style.display='none'" style="background:none;border:none;color:var(--d);font-size:22px;cursor:pointer;min-width:40px;min-height:40px">&times;</button>
</div>
<div id="mB" style="font-size:12px;line-height:1.6"></div>
<pre id="mP" style="display:none;background:var(--bg);padding:10px;border-radius:8px;font-size:10px;overflow-x:auto;color:var(--d);word-break:break-all;max-height:25vh;margin-top:8px"></pre>
</div>
</div>

<div id="TT" class="toast"></div>

<script>
var CH=[],GR=[],oP=null;
var H=window.location.origin;

function toast(m,ok){var t=document.getElementById('TT');t.textContent=m;t.className='toast show '+(ok?'':'err');setTimeout(function(){t.className='toast'},2500)}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

function doLogin(){
var pw=document.getElementById('pw').value;
if(!pw){document.getElementById('LE').textContent='Sifre girin!';return}
document.getElementById('LE').textContent='';
fetch('/api/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})})
.then(function(r){return r.json()}).then(function(d){
if(d.ok){document.getElementById('LP').style.display='none';document.getElementById('AP').style.display='block';loadDash()}
else{document.getElementById('LE').textContent='Yanlis sifre!'}
}).catch(function(){document.getElementById('LE').textContent='Baglanti hatasi!'});
}

function doOut(){document.getElementById('AP').style.display='none';document.getElementById('LP').style.display='flex';document.getElementById('pw').value=''}

function go(p,el){
var pgs=document.querySelectorAll('.pg');for(var i=0;i<pgs.length;i++)pgs[i].classList.remove('on');
var tabs=document.querySelectorAll('.tab');for(var i=0;i<tabs.length;i++)tabs[i].classList.remove('on');
document.getElementById('p'+p).classList.add('on');
if(el)el.classList.add('on');
if(p===0)loadDash();if(p===1)loadCh();if(p===2)loadGrp();if(p===3)loadLnk();if(p===4)loadLog();
window.scrollTo(0,0);
}

function loadDash(){
fetch('/api/status').then(function(r){return r.json()}).then(function(d){
var dot=document.getElementById('sD'),txt=document.getElementById('sT');
if(d.data_ready){dot.className='dot ok';txt.textContent='Hazir! '+d.available_channels+' kanal'}
else if(d.error){dot.className='dot no';txt.textContent='HATA: '+d.error}
else{dot.className='dot ld';txt.textContent='Yukleniyor...'}
var r=d.resolve_cache||{};
document.getElementById('sG').innerHTML=
sc(d.available_channels||0,'Toplam','c-b')+sc(d.hls_channels||0,'HLS','c-g')+
sc(d.available_categories||0,'Grup','')+
sc(d.vavoo_token?'OK':'YOK','Vavoo',d.vavoo_token?'c-g':'c-r')+
sc(d.lokke_token?'OK':'YOK','Lokke',d.lokke_token?'c-g':'c-r')+
sc((r.active||0)+'/'+(r.total||0),'Cache','c-y')+
sc((d.load_time||0)+'s','Sure','');
}).catch(function(){});
}

function sc(v,l,c){return '<div class="st"><b class="'+(c||'')+'">'+v+'</b><small>'+l+'</small></div>'}

function loadCh(){
document.getElementById('cL').innerHTML='<div style="text-align:center;padding:30px;color:var(--d)">Yukleniyor...</div>';
Promise.all([fetch('/api/admin/channels').then(function(r){return r.json()}),fetch('/api/admin/groups').then(function(r){return r.json()})])
.then(function(res){CH=res[0].channels||[];GR=res[1].groups||[];rC()})
.catch(function(){document.getElementById('cL').innerHTML='<div style="text-align:center;padding:30px;color:var(--r)">Hata!</div>'});
}

function rC(){
var q=document.getElementById('cQ').value.toLowerCase();
var g=document.getElementById('cG').value;
var t=document.getElementById('cT').value;
var list=CH;
if(q)list=list.filter(function(c){return c.name.toLowerCase().indexOf(q)>=0});
if(g)list=list.filter(function(c){return String(c.cid)===g});
if(t==='hls')list=list.filter(function(c){return c.has_hls});
if(t==='no')list=list.filter(function(c){return !c.has_hls});
document.getElementById('cN').textContent=list.length+' / '+CH.length+' kanal';
var gs='<option value="">Tum Gruplar</option>'+GR.map(function(g){return '<option value="'+g.cid+'">'+esc(g.name)+' ('+g.count+')</option>'}).join('');
document.getElementById('cG').innerHTML=gs;
if(g)document.getElementById('cG').value=g;
var gOpts=GR.map(function(g){return '<option value="'+g.cid+'">'+esc(g.name)+'</option>'}).join('');
var lim=list.slice(0,150);
var el=document.getElementById('cL');
if(!lim.length){el.innerHTML='<div style="text-align:center;padding:30px;color:var(--d)">Kanal bulunamadi</div>';return}
el.innerHTML=lim.map(function(c,i){
var bd=c.has_hls?'<span class="badge h">HLS</span>':(c.url?'<span class="badge u">URL</span>':'<span class="badge n">YOK</span>');
var logo=c.logo?'<img class="cl" src="'+c.logo+'" loading="lazy" onerror="this.style.display=\'none\'">':'<div class="cl" style="display:flex;align-items:center;justify-content:center;font-size:12px">TV</div>';
var pid='pa'+c.lid;
return '<div class="ci"><div class="cm">'+logo+'<div class="cn"><b>'+esc(c.name)+'</b><small>'+esc(c.grp)+bd+'</small></div>'+
'<button class="ex" onclick="tP(\''+pid+'\',event)">&#9662;</button></div>'+
'<div class="ca" id="'+pid+'"><div class="cr"><label>Sira:</label>'+
'<button class="btn sm" onclick="mC('+c.lid+',\'up\')" '+(i===0?'disabled':'')+')>&#9650;</button>'+
'<button class="btn sm" onclick="mC('+c.lid+',\'down\')" '+(i===lim.length-1?'disabled':'')+')>&#9660;</button></div>'+
'<div class="cr"><label>Grup:</label><select onchange="mCG('+c.lid+',this.value)"><option value="">-- Sec --</option>'+gOpts+'</select></div>'+
'<div class="cr"><button class="btn t" onclick="openT('+c.lid+')">&#9654; Resolve Test</button></div></div></div>';
}).join('');
}

function tP(id,e){
e&&e.stopPropagation();
var p=document.getElementById(id);
if(oP&&oP!==p)oP.classList.remove('open');
p.classList.toggle('open');
oP=p.classList.contains('open')?p:null;
}

function mC(lid,d){
toast('Sira degistiriliyor...',true);
fetch('/api/admin/channels/'+lid+'/move',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({direction:d})})
.then(function(r){return r.json()}).then(function(d){if(d.ok){loadCh();toast('Tamam!',true)}else toast(d.error||'Hata!')})
.catch(function(){toast('Baglanti hatasi!')});
}

function mCG(lid,ncid){
if(!ncid)return;
toast('Grup degistiriliyor...',true);
fetch('/api/admin/channels/'+lid+'/group',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({cid:parseInt(ncid)})})
.then(function(r){return r.json()}).then(function(d){if(d.ok){toast(d.new_group+'!',true);loadCh()}else toast(d.error||'Hata!')})
.catch(function(){toast('Baglanti hatasi!')});
}

function loadGrp(){
fetch('/api/admin/groups').then(function(r){return r.json()}).then(function(d){
GR=d.groups||[];
var el=document.getElementById('gL');
if(!GR.length){el.innerHTML='<div style="text-align:center;padding:30px;color:var(--d)">Grup yok</div>';return}
el.innerHTML=GR.map(function(g,i){
return '<div class="gi"><div class="ga">'+
'<button class="ab" onclick="mGO('+g.cid+',\'up\')" '+(i===0?'disabled':'')+'>&#9650;</button>'+
'<button class="ab" onclick="mGO('+g.cid+',\'down\')" '+(i===GR.length-1?'disabled':'')+'>&#9660;</button></div>'+
'<div class="gf"><b>'+esc(g.name)+'</b><small>'+g.count+' kanal</small></div>'+
'<div class="gc">'+g.count+'</div>'+
'<button class="btn red sm" onclick="dG('+g.cid+')" style="flex:0">Sil</button></div>';
}).join('');
}).catch(function(){});
}

function aG(){
var inp=document.getElementById('nG');var n=inp.value.trim();
if(!n){toast('Grup adi girin!');return}
toast('Ekleniyor...',true);
fetch('/api/admin/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})})
.then(function(r){return r.json()}).then(function(d){if(d.ok){inp.value='';toast(n+' eklendi!',true);loadGrp()}else toast(d.error||'Hata!')})
.catch(function(){toast('Baglanti hatasi!')});
}

function dG(cid){
if(!confirm('Bu grubu silmek istiyor musunuz?'))return;
toast('Siliniyor...',true);
fetch('/api/admin/groups/'+cid,{method:'DELETE'}).then(function(r){return r.json()}).then(function(d){
if(d.ok){toast('Silindi! ('+d.moved+' kanal)',true);loadGrp()}else toast('Hata!')})
.catch(function(){toast('Baglanti hatasi!')});
}

function mGO(cid,d){
toast('Sira degistiriliyor...',true);
fetch('/api/admin/groups/'+cid+'/move',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({direction:d})})
.then(function(r){return r.json()}).then(function(d){if(d.ok){loadGrp();toast('Tamam!',true)}else toast('Hata!')})
.catch(function(){toast('Baglanti hatasi!')});
}

function loadLnk(){
document.getElementById('l1').value=H+'/get.php?username=admin&password=admin&type=m3u_plus';
document.getElementById('l2').value=H+'/player_api.php?username=admin&password=admin';
document.getElementById('l3').value=H+'/epg.xml';
}

function cp(id){
var inp=document.getElementById(id);
if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(inp.value).then(function(){toast('Kopyalandi!',true)}).catch(function(){inp.select();document.execCommand('copy');toast('Kopyalandi!',true)})}
else{inp.select();document.execCommand('copy');toast('Kopyalandi!',true)}
}

function loadLog(){
fetch('/api/status').then(function(r){return r.json()}).then(function(d){
var logs=d.startup_logs||[];
document.getElementById('lB').innerHTML=logs.map(function(l){
var c='l';
if(l.indexOf('OK')>=0||l.indexOf('alindi')>=0||l.indexOf('eslesti')>=0||l.indexOf('guncellendi')>=0)c+=' ok';
else if(l.indexOf('HATA')>=0||l.indexOf('BOS')>=0||l.indexOf('ALINAMADI')>=0)c+=' er';
else if(l.indexOf('cekiliyor')>=0||l.indexOf('aliniyor')>=0||l.indexOf('basladi')>=0)c+=' w';
return '<div class="'+c+'">'+esc(l)+'</div>';
}).join('');
var box=document.getElementById('lB');box.scrollTop=box.scrollHeight;
}).catch(function(){});
}

function doReload(){
if(!confirm('Tum verileri yeniden yukle?'))return;
toast('Reload basladi...',true);
fetch('/reload').then(function(){setTimeout(function(){loadDash()},5000);setTimeout(loadDash,15000)});
}

function doCache(){
toast('Cache temizleniyor...',true);
fetch('/api/admin/cache/clear',{method:'POST'}).then(function(r){return r.json()}).then(function(d){
if(d.ok)toast('Cache temizlendi!',true);else toast('Hata!')}).catch(function(){toast('Hata!')});
}

function openT(lid){
document.getElementById('MS').style.display='flex';
document.getElementById('mT').textContent='Resolve Test';
document.getElementById('mB').innerHTML='<p style="color:var(--d)">Yukleniyor...</p>';
document.getElementById('mP').style.display='none';
fetch('/api/admin/resolve/'+lid).then(function(r){return r.json()}).then(function(d){
var ok=d.success;
var col=ok?'var(--g)':'var(--r)';
var ico=ok?'&#10003;':'&#10007;';
document.getElementById('mB').innerHTML=
'<p style="font-size:14px;font-weight:700;color:'+col+'">'+ico+' '+(ok?'Basarili':'Basarisiz')+'</p>'+
'<p><b>Yontem:</b> '+esc(d.resolve_method||'-')+'</p>'+
'<p><b>URL:</b> <span style="font-size:10px;word-break:break-all">'+esc(d.resolved_url||'-')+'</span></p>'+
'<p style="margin-top:8px;font-size:11px;color:var(--d)">Cache: '+(d.resolve_cache?d.resolve_cache.total:0)+' entries</p>';
if(d.resolved_url){
document.getElementById('mP').style.display='block';
document.getElementById('mP').textContent=d.resolved_url;
}
}).catch(function(){document.getElementById('mB').innerHTML='<p style="color:var(--r)">Hata!</p>'});
}
</script>
</body>
</html>"""
