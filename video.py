"""
video.py - FastAPI uygulama v10.0.0
Profesyonel mobil admin panel, grup/kanal yonetimi
v10.0: Resolve chain detayli log, Direct HLS destek
"""
import os, sqlite3, threading, re
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
import state

app = FastAPI(title="VxParser IPTV Proxy", version="10.0.0")
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
    hls_direct = state.check_direct_hls(ch["hls"]) if ch["hls"] else False
    return {"lid": ch["lid"], "name": ch["name"], "url": ch["url"], "hls": ch["hls"],
            "grp": ch["grp"], "hls_direct": hls_direct, "resolve_method": method, "resolved_url": url}

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

# ============ ADMIN API - GROUPS ============
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

# ============ ADMIN API - CHANNELS ============
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

# ============ ADMIN AUTH ============
@app.post("/api/admin/login")
async def adm_login(request:Request):
    body=await request.json()
    return {"ok": body.get("password")==ADMIN_PASSWORD}

# ============ ADMIN PANEL HTML v9 ============
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5,user-scalable=yes">
<title>VxParser Admin</title>
<style>
:root{--bg:#0a0e14;--card:#141920;--card2:#1a2028;--border:#2a3040;--purple:#8957e5;--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;--text:#d0d7de;--dim:#7a8390;--radius:12px}
*{margin:0;padding:0;box-sizing:border-box;-webkit-box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);font-size:15px;-webkit-tap-highlight-color:transparent;overflow-x:hidden}
html{scroll-behavior:smooth}

/* TOP BAR */
.topbar{background:linear-gradient(135deg,#141920 0%,#1a1525 100%);padding:14px 16px;display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;backdrop-filter:blur(10px)}
.topbar h1{font-size:17px;font-weight:800;background:linear-gradient(135deg,var(--purple),#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.topbar .ver{font-size:11px;color:var(--dim);margin-left:auto}

/* TAB BAR */
.tabbar{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--border);display:flex;z-index:100;padding-bottom:env(safe-area-inset-bottom,4px)}
.tab{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;padding:10px 2px 8px;border:none;background:none;color:var(--dim);font-size:10px;font-weight:600;cursor:pointer;transition:all .15s;position:relative}
.tab.active{color:var(--purple)}
.tab.active::before{content:'';position:absolute;top:0;left:50%;transform:translateX(-50%);width:32px;height:3px;background:var(--purple);border-radius:0 0 3px 3px}
.tab svg{width:20px;height:20px;fill:currentColor}

/* ACTIONS BAR */
.actions{position:fixed;bottom:58px;left:0;right:0;background:var(--card);border-top:1px solid var(--border);display:flex;gap:6px;padding:8px 12px;z-index:99;padding-bottom:calc(8px + env(safe-area-inset-bottom,0px))}
.act-btn{flex:1;padding:12px 0;border:1px solid var(--border);border-radius:10px;background:none;color:var(--text);font-size:13px;font-weight:700;cursor:pointer;text-align:center;transition:all .1s}
.act-btn:active{background:var(--purple);border-color:var(--purple);color:#fff;transform:scale(.97)}
.act-btn.red{border-color:#f8514940;color:var(--red)}
.act-btn.red:active{background:var(--red);color:#fff}

/* CONTENT */
.content{padding:12px 12px 140px 12px;max-width:900px;margin:0 auto}
.page{display:none}.page.active{display:block}

/* STATS GRID */
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}
.st{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px 8px;text-align:center}
.st b{display:block;font-size:22px;font-weight:800;color:var(--text)}
.st small{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-top:2px;display:block}
.st.ok b{color:var(--green)}.st.no b{color:var(--red)}.st.warn b{color:var(--yellow)}.st.info b{color:var(--blue)}

/* STATUS BAR */
.sbar{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:12px 16px;margin-bottom:12px;display:flex;align-items:center;gap:10px;font-size:14px}
.sdot{width:11px;height:11px;border-radius:50%;flex-shrink:0}
.sdot.ok{background:var(--green);box-shadow:0 0 10px #3fb95060}
.sdot.load{background:var(--yellow);animation:pulse 1.5s infinite}
.sdot.err{background:var(--red);box-shadow:0 0 10px #f8514960}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.85)}}

/* SEARCH */
.search{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.search input{flex:1;min-width:140px;padding:12px 14px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--text);font-size:15px;outline:none;transition:border .2s}
.search input:focus{border-color:var(--purple)}
.search select{padding:12px 10px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--text);font-size:13px;outline:none;min-width:100px}

/* CHANNEL LIST */
.ch-count{font-size:12px;color:var(--dim);margin-bottom:8px;padding-left:4px}
.ch-item{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:8px;overflow:hidden}
.ch-main{display:flex;align-items:center;gap:10px;padding:12px 14px}
.ch-logo{width:38px;height:38px;border-radius:8px;object-fit:contain;background:var(--card2);flex-shrink:0;border:1px solid var(--border)}
.ch-logo-empty{width:38px;height:38px;border-radius:8px;background:var(--card2);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:16px;border:1px solid var(--border)}
.ch-info{flex:1;min-width:0}
.ch-name{font-weight:700;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.3}
.ch-sub{font-size:11px;color:var(--dim);margin-top:2px;display:flex;align-items:center;gap:6px}
.ch-badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:800;letter-spacing:.3px}
.ch-badge.hls{background:#3fb95018;color:var(--green);border:1px solid #3fb95030}
.ch-badge.url{background:#58a6ff18;color:var(--blue);border:1px solid #58a6ff30}
.ch-badge.no{background:#f8514918;color:var(--red);border:1px solid #f8514930}
.ch-expand{width:40px;height:40px;border:none;background:none;color:var(--dim);display:flex;align-items:center;justify-content:center;font-size:20px;cursor:pointer;border-radius:8px;flex-shrink:0}
.ch-expand:active{background:var(--card2)}

/* CHANNEL ACTION PANEL (expanded) */
.ch-actions{display:none;padding:0 14px 14px;border-top:1px solid var(--border);margin-top:0}
.ch-actions.open{display:block;padding-top:12px}
.ch-row{display:flex;gap:8px;margin-bottom:8px;align-items:center}
.ch-row label{font-size:11px;color:var(--dim);min-width:46px;font-weight:600}
.ch-btn{padding:10px 16px;border-radius:8px;border:1px solid var(--border);background:var(--card2);color:var(--text);font-size:13px;font-weight:600;cursor:pointer;transition:all .1s;flex:1;text-align:center;min-height:44px;display:flex;align-items:center;justify-content:center;gap:4px}
.ch-btn:active{background:var(--purple);color:#fff;border-color:var(--purple);transform:scale(.97)}
.ch-btn:disabled{opacity:.3;pointer-events:none}
.ch-btn.test{border-color:var(--blue);color:var(--blue)}
.ch-btn.test:active{background:var(--blue);color:#fff}
.ch-btn.sm{flex:0;padding:10px 14px}
.ch-actions select{flex:1;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--card);color:var(--text);font-size:13px;min-height:44px}

/* GROUP LIST */
.grp-add{display:flex;gap:8px;margin-bottom:14px}
.grp-add input{flex:1;padding:14px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--text);font-size:15px;outline:none}
.grp-add input:focus{border-color:var(--purple)}
.grp-add button{padding:14px 22px;border-radius:10px;border:none;background:linear-gradient(135deg,var(--green),#2ea043);color:#fff;font-size:14px;font-weight:800;white-space:nowrap;cursor:pointer;min-height:48px;transition:transform .1s}
.grp-add button:active{transform:scale(.96)}
.grp-item{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px;margin-bottom:8px;display:flex;align-items:center;gap:10px}
.grp-arrows{display:flex;flex-direction:column;gap:4px;flex-shrink:0}
.grp-info{flex:1;min-width:0}
.grp-name{font-weight:700;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.grp-cnt{font-size:11px;color:var(--dim);margin-top:2px}
.grp-count{font-size:24px;font-weight:900;color:var(--purple);min-width:40px;text-align:center;flex-shrink:0}
.grp-del{padding:10px 14px;border-radius:8px;border:1px solid #f8514940;background:none;color:var(--red);font-size:12px;font-weight:700;cursor:pointer;min-height:44px;display:flex;align-items:center;flex-shrink:0;transition:all .1s}
.grp-del:active{background:var(--red);color:#fff;transform:scale(.96)}
.arr{width:36px;height:32px;border-radius:6px;border:1px solid var(--border);background:none;color:var(--dim);display:flex;align-items:center;justify-content:center;font-size:14px;cursor:pointer;transition:all .1s}
.arr:active{background:var(--purple);color:#fff;border-color:var(--purple)}
.arr:disabled{opacity:.2;pointer-events:none}

/* LINKS */
.lnk{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px;margin-bottom:8px}
.lnk label{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;display:block;margin-bottom:8px;font-weight:700}
.lnk-row{display:flex;gap:8px}
.lnk input{flex:1;padding:12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:12px;font-family:monospace}
.lnk button{padding:12px 16px;border-radius:8px;border:none;background:var(--purple);color:#fff;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;min-height:44px;transition:transform .1s}
.lnk button:active{transform:scale(.96)}

/* LOGS */
.log{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:12px;max-height:60vh;overflow-y:auto;font-family:'SF Mono',Monaco,Consolas,monospace;font-size:11px;line-height:1.9;-webkit-overflow-scrolling:touch}
.l{color:var(--dim);word-break:break-all}.l.ok{color:var(--green)}.l.er{color:var(--red)}.l.w{color:var(--yellow)}

/* LOGIN */
.login{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;background:var(--bg)}
.login-box{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:36px 28px;width:100%;max-width:360px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.4)}
.login-box h2{background:linear-gradient(135deg,var(--purple),#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-size:26px;margin-bottom:6px}
.login-box p{color:var(--dim);margin-bottom:24px;font-size:13px}
.login-box input{width:100%;padding:16px;border-radius:12px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:16px;margin-bottom:16px;outline:none}
.login-box input:focus{border-color:var(--purple)}
.login-box button{width:100%;padding:16px;border-radius:12px;border:none;background:linear-gradient(135deg,var(--purple),#7c3aed);color:#fff;font-size:16px;font-weight:800;cursor:pointer;transition:transform .1s;min-height:52px}
.login-box button:active{transform:scale(.97)}
.login-err{color:var(--red);font-size:13px;margin-top:12px;min-height:20px}

/* TOAST */
.toast{position:fixed;top:16px;left:50%;transform:translateX(-50%) translateY(-20px);background:var(--card);border:1px solid var(--green);border-radius:12px;padding:12px 24px;color:var(--green);font-size:14px;font-weight:700;z-index:999;opacity:0;transition:all .3s;pointer-events:none;max-width:90%;text-align:center;box-shadow:0 8px 30px rgba(0,0,0,.4)}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.err{border-color:var(--red);color:var(--red)}

/* MODAL */
.modal-bg{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.75);z-index:200;align-items:flex-end;justify-content:center}
.modal-bg.show{display:flex}
.modal{background:var(--card);border-radius:20px 20px 0 0;padding:24px 20px;width:100%;max-width:500px;max-height:75vh;overflow-y:auto;-webkit-overflow-scrolling:touch}
.modal h3{color:var(--purple);margin-bottom:14px;font-size:17px;font-weight:800}
.modal p{font-size:13px;line-height:1.6;color:var(--text);margin-bottom:6px}
.modal pre{background:var(--bg);padding:12px;border-radius:10px;font-size:11px;overflow-x:auto;color:var(--dim);word-break:break-all;max-height:30vh;overflow-y:auto;margin-top:10px}
.modal .close{float:right;background:none;border:none;color:var(--dim);font-size:24px;cursor:pointer;padding:4px;min-width:44px;min-height:44px;display:flex;align-items:center;justify-content:center;border-radius:8px}
.modal .close:active{background:var(--card2)}
.modal .tbtn{width:100%;padding:14px;border-radius:10px;border:none;background:var(--purple);color:#fff;font-size:15px;font-weight:800;cursor:pointer;margin-top:12px;min-height:50px;transition:transform .1s}
.modal .tbtn:active{transform:scale(.98)}

/* DESKTOP */
@media(min-width:768px){
  .content{padding:16px 20px 80px}
  .tabbar{position:static;border-top:1px solid var(--border);border-bottom:none}
  .tab{flex-direction:row;gap:8px;padding:12px 16px;font-size:12px}
  .tab.active::before{display:none}
  .tab.active{background:var(--card2)}
  .tab svg{width:18px;height:18px}
  .actions{position:static;border-top:1px solid var(--border);border-bottom:1px solid var(--border);padding-bottom:8px}
  .stats{grid-template-columns:repeat(4,1fr)}
  .content{padding-bottom:24px}
}

::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
</head>
<body>

<!-- LOGIN -->
<div id="loginPage" class="login">
  <div class="login-box">
    <h2>VxParser</h2>
    <p>Admin Panel v9</p>
    <input type="password" id="pw" placeholder="Sifre" autocomplete="current-password" onkeydown="if(event.key==='Enter')login()">
    <button onclick="login()">Giris Yap</button>
    <div id="loginErr" class="login-err"></div>
  </div>
</div>

<!-- APP -->
<div id="app" style="display:none">
  <div class="topbar"><h1>VxParser</h1><span class="ver">v4.0</span></div>

  <div class="content">
    <!-- DASHBOARD -->
    <div id="p-dash" class="page active">
      <div class="sbar"><div class="sdot load" id="sDot"></div><span id="sTxt">Yukleniyor...</span></div>
      <div class="stats" id="statsGrid"></div>
    </div>

    <!-- CHANNELS -->
    <div id="p-ch" class="page">
      <div class="search">
        <input id="chQ" placeholder="Kanal ara..." oninput="renderCh()" type="search">
        <select id="chG" onchange="renderCh()"><option value="">Tum Gruplar</option></select>
        <select id="chT" onchange="renderCh()">
          <option value="">Tum Tip</option><option value="hls">HLS</option><option value="no">HLS Yok</option>
        </select>
      </div>
      <div class="ch-count" id="chCnt"></div>
      <div id="chList"></div>
    </div>

    <!-- GROUPS -->
    <div id="p-grp" class="page">
      <div class="grp-add">
        <input id="newGrp" placeholder="Yeni grup adi..." type="text" onkeydown="if(event.key==='Enter')addGrp()">
        <button onclick="addGrp()">+ Ekle</button>
      </div>
      <div id="grpList"></div>
    </div>

    <!-- LINKS -->
    <div id="p-lnk" class="page">
      <div class="lnk"><label>M3U Playlist</label><div class="lnk-row"><input id="lk1" readonly><button onclick="cp('lk1')">Kopyala</button></div></div>
      <div class="lnk"><label>Xtream Codes API</label><div class="lnk-row"><input id="lk2" readonly><button onclick="cp('lk2')">Kopyala</button></div></div>
      <div class="lnk"><label>EPG (XMLTV)</label><div class="lnk-row"><input id="lk3" readonly><button onclick="cp('lk3')">Kopyala</button></div></div>
    </div>

    <!-- LOGS -->
    <div id="p-log" class="page">
      <div class="sbar" style="margin-bottom:10px"><span style="color:var(--dim);font-size:12px">Baslangic loglari</span></div>
      <div id="logBox" class="log"></div>
    </div>
  </div>

  <!-- ACTIONS -->
  <div class="actions">
    <button class="act-btn" onclick="doReload()">Reload</button>
    <button class="act-btn" onclick="doCache()">Cache Temizle</button>
    <button class="act-btn red" onclick="doLogout()">Cikis</button>
  </div>

  <!-- TAB BAR -->
  <div class="tabbar">
    <button class="tab active" onclick="go('dash',this)" id="t-dash">
      <svg viewBox="0 0 24 24"><path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"/></svg>Durum
    </button>
    <button class="tab" onclick="go('ch',this)" id="t-ch">
      <svg viewBox="0 0 24 24"><path d="M21 6H3a1 1 0 00-1 1v10a1 1 0 001 1h18a1 1 0 001-1V7a1 1 0 00-1-1zm-8 5H11v4h2v-4zm4 0h-2v4h2v-4zM9 11H7v4h2v-4z"/></svg>Kanallar
    </button>
    <button class="tab" onclick="go('grp',this)" id="t-grp">
      <svg viewBox="0 0 24 24"><path d="M4 8h4V4H4v4zm6 12h4v-4h-4v4zm-6 0h4v-4H4v4zm0-6h4v-4H4v4zm6 0h4v-4h-4v4zm6-10v4h4V4h-4zm-6 4h4V4h-4v4zm6 6h4v-4h-4v4zm0 6h4v-4h-4v4z"/></svg>Gruplar
    </button>
    <button class="tab" onclick="go('lnk',this)" id="t-lnk">
      <svg viewBox="0 0 24 24"><path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z"/></svg>Linkler
    </button>
    <button class="tab" onclick="go('log',this)" id="t-log">
      <svg viewBox="0 0 24 24"><path d="M20 8h-3V4H3c-1.1 0-2 .9-2 2v11h2c0 1.66 1.34 3 3 3s3-1.34 3-3h6c0 1.66 1.34 3 3 3s3-1.34 3-3h2v-5l-3-4zM6 18.5c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zm13.5-9l1.96 2.5H17V9.5h2.5zm-1.5 9c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5z"/></svg>Loglar
    </button>
  </div>
</div>

<!-- MODAL -->
<div id="modal" class="modal-bg" onclick="if(event.target===this)closeM()">
  <div class="modal">
    <button class="close" onclick="closeM()">&times;</button>
    <h3 id="mTitle">Test</h3>
    <div id="mBody"></div>
    <button class="tbtn" id="mBtn" onclick="runTest()">Resolve Test</button>
    <pre id="mPre" style="display:none"></pre>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
let CH=[],GR=[],testLid=null,openPanel=null;
const H=window.location.origin;

function toast(m,ok){const t=document.getElementById('toast');t.textContent=m;t.className='toast show '+(ok?'':'err');setTimeout(()=>t.className='toast',2500)}

// AUTH
function login(){
  const pw=document.getElementById('pw').value;
  if(!pw){document.getElementById('loginErr').textContent='Sifre girin!';return}
  document.getElementById('loginErr').textContent='';
  fetch('/api/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){document.getElementById('loginPage').style.display='none';document.getElementById('app').style.display='block';loadDash()}
    else{document.getElementById('loginErr').textContent='Yanlis sifre!'}
  }).catch(e=>{document.getElementById('loginErr').textContent='Baglanti hatasi!'});
}
function doLogout(){document.getElementById('app').style.display='none';document.getElementById('loginPage').style.display='flex';document.getElementById('pw').value=''}

// NAV
function go(p,el){
  document.querySelectorAll('.page').forEach(e=>e.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));
  document.getElementById('p-'+p).classList.add('active');
  if(el)el.classList.add('active');
  if(p==='dash')loadDash();
  if(p==='ch')loadCh();
  if(p==='grp')loadGrp();
  if(p==='lnk')loadLnk();
  if(p==='log')loadLog();
  window.scrollTo(0,0);
}

// DASHBOARD
function loadDash(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    const dot=document.getElementById('sDot'),txt=document.getElementById('sTxt');
    if(d.data_ready){dot.className='sdot ok';txt.textContent='Hazir! '+d.available_channels+' kanal yuklu'}
    else if(d.error){dot.className='sdot err';txt.textContent='HATA: '+d.error}
    else{dot.className='sdot load';txt.textContent='Yukleniyor...'}
    const r=d.resolve_cache||{};
    document.getElementById('statsGrid').innerHTML=
      sc(d.available_channels||0,'Toplam','info')+sc(d.hls_channels||0,'HLS','ok')+
      sc(d.available_categories||0,'Grup','')+
      sc(d.vavoo_token?'OK':'YOK','Vavoo',d.vavoo_token?'ok':'no')+
      sc(d.lokke_token?'OK':'YOK','Lokke',d.lokke_token?'ok':'no')+
      sc((r.active||0)+'/'+(r.total||0),'Cache','warn')+
      sc((d.load_time||0)+'s','Sure','');
  }).catch(()=>{});
}
function sc(v,l,c){return '<div class="st '+(c||'')+'"><b>'+v+'</b><small>'+l+'</small></div>'}

// CHANNELS
function loadCh(){
  document.getElementById('chList').innerHTML='<div style="text-align:center;padding:40px;color:var(--dim)">Yukleniyor...</div>';
  Promise.all([fetch('/api/admin/channels').then(r=>r.json()),fetch('/api/admin/groups').then(r=>r.json())])
  .then(([cd,grd])=>{CH=cd.channels||[];GR=grd.groups||[];renderCh()})
  .catch(e=>{document.getElementById('chList').innerHTML='<div style="text-align:center;padding:40px;color:var(--red)">Yukleme hatasi!</div>'});
}
function renderCh(){
  const q=document.getElementById('chQ').value.toLowerCase();
  const g=document.getElementById('chG').value;
  const t=document.getElementById('chT').value;
  let list=CH;
  if(q)list=list.filter(c=>c.name.toLowerCase().includes(q));
  if(g)list=list.filter(c=>String(c.cid)===g);
  if(t==='hls')list=list.filter(c=>c.has_hls);
  if(t==='no')list=list.filter(c=>!c.has_hls);
  document.getElementById('chCnt').textContent=list.length+' / '+CH.length+' kanal';
  const gs='<option value="">Tum Gruplar</option>'+GR.map(g=>'<option value="'+g.cid+'">'+g.name+' ('+g.count+')</option>').join('');
  document.getElementById('chG').innerHTML=gs;
  if(g)document.getElementById('chG').value=g;
  const gOpts=GR.map(g=>'<option value="'+g.cid+'">'+g.name+'</option>').join('');
  const limit=list.slice(0,200);
  const el=document.getElementById('chList');
  if(limit.length===0){el.innerHTML='<div style="text-align:center;padding:40px;color:var(--dim)">Kanal bulunamadi</div>';return}
  el.innerHTML=limit.map((c,i)=>{
    const badge=c.has_hls?'<span class="ch-badge hls">HLS</span>':(c.url?'<span class="ch-badge url">URL</span>':'<span class="ch-badge no">YOK</span>');
    const logo=c.logo?'<img class="ch-logo" src="'+c.logo+'" loading="lazy" onerror="this.style.display=\'none\'">':'<div class="ch-logo-empty">TV</div>';
    const first=i===0,last=i===limit.length-1;
    const pid='pa'+c.lid;
    return '<div class="ch-item"><div class="ch-main">'+logo+
      '<div class="ch-info"><div class="ch-name">'+esc(c.name)+'</div><div class="ch-sub">'+esc(c.grp)+badge+'</div></div>'+
      '<button class="ch-expand" onclick="togglePanel(\''+pid+'\',event)" aria-label="Islemler">&#9662;</button>'+
      '</div>'+
      '<div class="ch-actions" id="'+pid+'">'+
        '<div class="ch-row"><label>Sira:</label>'+
          '<button class="ch-btn sm" onclick="mvCh('+c.lid+',\'up\')" '+(first?'disabled':'')+'>&#9650; Yukari</button>'+
          '<button class="ch-btn sm" onclick="mvCh('+c.lid+',\'down\')" '+(last?'disabled':'')+'>&#9660; Asagi</button>'+
        '</div>'+
        '<div class="ch-row"><label>Grup:</label>'+
          '<select onchange="mvChGrp('+c.lid+',this.value)" ontouchstart="this.style.zoom=1.01" ontouchend="this.style.zoom=\'\'"><option value="">-- Grup Sec --</option>'+gOpts+'</select>'+
        '</div>'+
        '<div class="ch-row">'+
          '<button class="ch-btn test" onclick="openTest('+c.lid+')">&#9654; Resolve Test</button>'+
        '</div>'+
      '</div></div>';
  }).join('');
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function togglePanel(id,e){
  e&&e.stopPropagation();
  const p=document.getElementById(id);
  if(openPanel&&openPanel!==p){openPanel.classList.remove('open')}
  p.classList.toggle('open');
  openPanel=p.classList.contains('open')?p:null;
}
function mvCh(lid,d){
  toast('Sira degistiriliyor...',true);
  fetch('/api/admin/channels/'+lid+'/move',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({direction:d})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){loadCh();toast('Sira degistirildi!',true)}
    else toast(d.error||'Hata!');
  }).catch(()=>toast('Baglanti hatasi!'));
}
function mvChGrp(lid,ncid){
  if(!ncid)return;
  toast('Grup degistiriliyor...',true);
  fetch('/api/admin/channels/'+lid+'/group',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({cid:parseInt(ncid)})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){toast(d.new_group+' grubuna tasindi!',true);loadCh()}
    else toast(d.error||'Hata!');
  }).catch(()=>toast('Baglanti hatasi!'));
}

// GROUPS
function loadGrp(){
  fetch('/api/admin/groups').then(r=>r.json()).then(d=>{
    GR=d.groups||[];
    const el=document.getElementById('grpList');
    if(GR.length===0){el.innerHTML='<div style="text-align:center;padding:40px;color:var(--dim)">Grup yok</div>';return}
    el.innerHTML=GR.map((g,i)=>{
      return '<div class="grp-item">'+
        '<div class="grp-arrows">'+
          '<button class="arr" onclick="mvGrpOrder('+g.cid+',\'up\')" '+(i===0?'disabled':'')+'>&#9650;</button>'+
          '<button class="arr" onclick="mvGrpOrder('+g.cid+',\'down\')" '+(i===GR.length-1?'disabled':'')+'>&#9660;</button>'+
        '</div>'+
        '<div class="grp-info"><div class="grp-name">'+esc(g.name)+'</div><div class="grp-cnt">'+g.count+' kanal</div></div>'+
        '<div class="grp-count">'+g.count+'</div>'+
        '<button class="grp-del" onclick="delGrp('+g.cid+')">Sil</button>'+
      '</div>';
    }).join('');
  }).catch(()=>{});
}
function addGrp(){
  const inp=document.getElementById('newGrp');const n=inp.value.trim();
  if(!n){toast('Grup adi girin!');return}
  toast('Ekleniyor...',true);
  fetch('/api/admin/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){inp.value='';toast(n+' eklendi!',true);loadGrp()}
    else toast(d.error||'Hata!');
  }).catch(()=>toast('Baglanti hatasi!'));
}
function delGrp(cid){
  if(!confirm('Bu grubu silmek istiyor musunuz?\nKanallar DE SONSTIGE grubuna tasinacak.'))return;
  toast('Siliniyor...',true);
  fetch('/api/admin/groups/'+cid,{method:'DELETE'}).then(r=>r.json()).then(d=>{
    if(d.ok){toast('Grup silindi! ('+d.moved+' kanal tasindi)',true);loadGrp()}
    else toast('Hata!');
  }).catch(()=>toast('Baglanti hatasi!'));
}
function mvGrpOrder(cid,d){
  toast('Sira degistiriliyor...',true);
  fetch('/api/admin/groups/'+cid+'/move',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({direction:d})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){loadGrp();toast('Grup sirasi degistirildi!',true)}
    else toast('Hata!');
  }).catch(()=>toast('Baglanti hatasi!'));
}

// LINKS
function loadLnk(){
  document.getElementById('lk1').value=H+'/get.php?username=admin&password=admin&type=m3u_plus';
  document.getElementById('lk2').value=H+'/player_api.php?username=admin&password=admin';
  document.getElementById('lk3').value=H+'/epg.xml';
}
function cp(id){
  const inp=document.getElementById(id);
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(inp.value).then(()=>toast('Kopyalandi!',true)).catch(()=>{inp.select();document.execCommand('copy');toast('Kopyalandi!',true)});
  }else{inp.select();document.execCommand('copy');toast('Kopyalandi!',true)}
}

// LOGS
function loadLog(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    const logs=d.startup_logs||[];
    document.getElementById('logBox').innerHTML=logs.map(l=>{
      let c='l';
      if(l.includes('OK')||l.includes('alindi')||l.includes('eslesti')||l.includes('guncellendi'))c+=' ok';
      else if(l.includes('HATA')||l.includes('BOS')||l.includes('ALINAMADI')||l.includes('HATA'))c+=' er';
      else if(l.includes('cekiliyor')||l.includes('aliniyor')||l.includes('basladi'))c+=' w';
      return '<div class="'+c+'">'+esc(l)+'</div>';
    }).join('');
    // Scroll to bottom
    const box=document.getElementById('logBox');
    box.scrollTop=box.scrollHeight;
  }).catch(()=>{});
}

// RELOAD & CACHE
function doReload(){
  if(!confirm('Tum verileri yeniden yukle?\nCatalog + Kanallar + HLS linkleri'))return;
  toast('Reload basladi...',true);
  fetch('/reload').then(()=>{setTimeout(()=>{loadDash();toast('Reload devam ediyor...',true)},5000);setTimeout(loadDash,15000);setTimeout(loadDash,30000)});
}
function doCache(){
  toast('Cache temizleniyor...',true);
  fetch('/api/admin/cache/clear',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok)toast('Cache temizlendi!',true);else toast('Hata!');
  }).catch(()=>toast('Baglanti hatasi!'));
}

// TEST MODAL
function openTest(lid){
  testLid=lid;
  document.getElementById('mTitle').textContent='Resolve Test';
  document.getElementById('mBody').innerHTML='<p style="color:var(--dim)">Kanal bilgisi yukleniyor...</p>';
  document.getElementById('mPre').style.display='none';
  const btn=document.getElementById('mBtn');btn.style.display='block';btn.textContent='Resolve Test';btn.disabled=false;btn.style.background='';
  document.getElementById('modal').classList.add('show');
  fetch('/test/'+lid).then(r=>r.json()).then(d=>{
    if(d.error){document.getElementById('mBody').innerHTML='<p style="color:var(--red)">'+esc(d.error)+'</p>';return}
    document.getElementById('mBody').innerHTML=
      '<p><b>Kanal:</b> '+esc(d.name)+'</p>'+
      '<p><b>HLS:</b> '+(d.hls?'<span style="color:var(--green)">Var</span>':'<span style="color:var(--red)">YOK</span>')+'</p>'+
      '<p><b>URL:</b> '+(d.url?'<span style="color:var(--green)">Var</span>':'<span style="color:var(--red)">YOK</span>')+'</p>'+
      '<p><b>Grup:</b> '+esc(d.grp)+'</p>';
  }).catch(()=>{document.getElementById('mBody').innerHTML='<p style="color:var(--red)">Baglanti hatasi!</p>'});
}
function runTest(){
  const btn=document.getElementById('mBtn');btn.textContent='Test ediliyor...';btn.disabled=true;
  fetch('/api/admin/resolve/'+testLid).then(r=>r.json()).then(d=>{
    btn.disabled=false;const pre=document.getElementById('mPre');pre.style.display='block';
    pre.textContent=JSON.stringify(d,null,2);
    if(d.resolved_url){btn.textContent='BASARILI!';btn.style.background='var(--green)'}
    else{btn.textContent='BASARISIZ';btn.style.background='var(--red)'}
    setTimeout(()=>btn.style.background='',5000);
  }).catch(()=>{btn.disabled=false;btn.textContent='Baglanti hatasi!'});
}
function closeM(){document.getElementById('modal').classList.remove('show')}

// AUTO REFRESH
setInterval(()=>{if(document.getElementById('p-dash').classList.contains('active'))loadDash()},30000);
</script>
</body>
</html>"""


@app.get("/admin")
async def admin():
    return Response(content=ADMIN_HTML, media_type="text/html")
