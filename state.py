import sqlite3
import httpx
import time
import logging
import json
import re
import asyncio

log = logging.getLogger("vxparser")

DATA_READY = False
STARTUP_LOGS = []
VAVOO_TOKEN = ""
WATCHED_SIG = ""
WATCHED_SIG_TIME = 0
DB_PATH = "/tmp/vxparser.db"
RESOLVE_CACHE = {}
SIG_REFRESH_INTERVAL = 1800

def add_log(msg):
    log.info(msg)
    STARTUP_LOGS.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    if len(STARTUP_LOGS) > 100:
        STARTUP_LOGS.pop(0)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY, name TEXT, url TEXT, hls TEXT DEFAULT '', grp TEXT DEFAULT '', country TEXT DEFAULT '', logo TEXT DEFAULT '')")
    conn.commit()
    conn.close()

def get_channel(ch_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM channels WHERE id = ?", (ch_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_channels(ordered=True):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if ordered:
        c.execute("""SELECT * FROM channels ORDER BY CASE grp
            WHEN 'TR ULUSAL' THEN 1 WHEN 'TR HABER' THEN 2
            WHEN 'TR BELGESEL' THEN 3 WHEN 'TR COCUK' THEN 4
            WHEN 'TR FILM' THEN 5 WHEN 'TR MUZIK' THEN 6
            WHEN 'TR SPOR' THEN 7 WHEN 'TR DINI' THEN 8
            WHEN 'TR YEREL' THEN 9 WHEN 'TR RADYO' THEN 10
            WHEN 'DE VOLLPROGRAMM' THEN 11 WHEN 'DE NACHRICHTEN' THEN 12
            WHEN 'DE DOKU' THEN 13 WHEN 'DE KINDER' THEN 14
            WHEN 'DE FILM' THEN 15 WHEN 'DE MUSIK' THEN 16
            WHEN 'DE SPORT' THEN 17 WHEN 'DE SONSTIGE' THEN 18
            ELSE 99 END, name""")
    else:
        c.execute("SELECT * FROM channels")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def update_channel_hls(ch_id, hls_url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE channels SET hls = ? WHERE id = ?", (hls_url, ch_id))
    conn.commit()
    conn.close()

async def refresh_watched_sig(force=False):
    global WATCHED_SIG, WATCHED_SIG_TIME
    try:
        now_ms = int(time.time()) * 1000
        headers = {"user-agent": "okhttp/4.11.0", "accept": "application/json", "content-type": "application/json; charset=utf-8"}
        data = {"token": "", "reason": "boot", "locale": "de", "theme": "dark", "metadata": {"device": {"type": "desktop", "uniqueId": ""}, "os": {"name": "win32", "version": "Windows 10", "abis": ["x64"], "host": "DESKTOP-VX"}, "app": {"platform": "electron"}, "version": {"package": "app.lokke.main", "binary": "1.0.19", "js": "1.0.19"}}, "appFocusTime": 173, "playerActive": False, "playDuration": 0, "devMode": True, "hasAddon": True, "castConnected": False, "package": "app.lokke.main", "version": "1.0.19", "process": "app", "firstAppStart": now_ms, "lastAppStart": now_ms, "ipLocation": 0, "adblockEnabled": True, "proxy": {"supported": ["ss"], "engine": "cu", "enabled": False, "autoServer": True, "id": 0}, "iap": {"supported": False}}
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.post("https://www.lokke.app/api/app/ping", json=data, headers=headers)
            result = r.json()
            sig = result.get("addonSig")
            if sig:
                WATCHED_SIG = sig
                WATCHED_SIG_TIME = time.time()
                add_log(f"Signature yenilendi ({len(sig)} char)")
                return sig
            else:
                add_log(f"Sig basarisiz: {json.dumps(result)[:200]}")
    except Exception as e:
        add_log(f"Sig hata: {e}")
    return ""

async def get_watched_sig():
    global WATCHED_SIG, WATCHED_SIG_TIME
    if WATCHED_SIG and (time.time() - WATCHED_SIG_TIME) < SIG_REFRESH_INTERVAL:
        return WATCHED_SIG
    return await refresh_watched_sig()

async def resolve_mediahubmx(url):
    sig = await get_watched_sig()
    if not sig:
        sig = await refresh_watched_sig(force=True)
    if not sig:
        add_log("Resolve: sig alinamadi!")
        return None
    try:
        headers = {"user-agent": "MediaHubMX/2", "accept": "application/json", "content-type": "application/json; charset=utf-8", "mediahubmx-signature": sig}
        data = {"language": "de", "region": "AT", "url": url, "clientVersion": "3.0.2"}
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.post("https://vavoo.to/mediahubmx-resolve.json", json=data, headers=headers)
            if r.status_code == 200:
                result = r.json()
                if isinstance(result, list) and len(result) > 0:
                    resolved_url = result[0].get("url", "")
                    if resolved_url:
                        return resolved_url
                add_log("Resolve bos response")
            elif r.status_code == 403:
                add_log("Resolve 403 -> sig yenileniyor...")
                new_sig = await refresh_watched_sig(force=True)
                if new_sig:
                    headers["mediahubmx-signature"] = new_sig
                    r2 = await client.post("https://vavoo.to/mediahubmx-resolve.json", json=data, headers=headers)
                    if r2.status_code == 200:
                        result = r2.json()
                        if isinstance(result, list) and len(result) > 0:
                            resolved_url = result[0].get("url", "")
                            if resolved_url:
                                add_log("Resolve 2. deneme BASARILI!")
                                return resolved_url
                    add_log(f"Resolve 2. deneme basarisiz: HTTP {r2.status_code}")
                else:
                    add_log("Resolve: sig yenilenemedi!")
            else:
                add_log(f"Resolve HTTP {r.status_code}")
    except Exception as e:
        add_log(f"Resolve hata: {e}")
    return None

async def fetch_catalog(group, cursor=0):
    sig = await get_watched_sig()
    if not sig:
        sig = await refresh_watched_sig(force=True)
    if not sig:
        return {}
    try:
        headers = {"user-agent": "MediaHubMX/2", "accept": "application/json", "content-type": "application/json; charset=utf-8", "mediahubmx-signature": sig}
        data = {"language": "de", "region": "AT", "catalogId": "iptv", "id": "iptv", "adult": False, "search": "", "sort": "name", "filter": {"group": group}, "cursor": cursor, "clientVersion": "3.0.2"}
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            r = await client.post("https://vavoo.to/mediahubmx-catalog.json", json=data, headers=headers)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 403:
                new_sig = await refresh_watched_sig(force=True)
                if new_sig:
                    headers["mediahubmx-signature"] = new_sig
                    r2 = await client.post("https://vavoo.to/mediahubmx-catalog.json", json=data, headers=headers)
                    if r2.status_code == 200:
                        return r2.json()
            add_log(f"Catalog HTTP error '{group}'")
    except Exception as e:
        add_log(f"Catalog hata ({group}): {e}")
    return {}

async def fetch_all_catalog(group_name):
    all_items = []
    cursor = 0
    while True:
        result = await fetch_catalog(group_name, cursor)
        if not result or not isinstance(result, dict):
            break
        items = result.get("items", [])
        if not items:
            break
        all_items.extend(items)
        next_cursor = result.get("nextCursor")
        if not next_cursor:
            break
        cursor = next_cursor
    return all_items

async def sig_refresh_loop():
    while True:
        await asyncio.sleep(SIG_REFRESH_INTERVAL)
        add_log("Otomatik sig yenileme...")
        await refresh_watched_sig(force=True)
        RESOLVE_CACHE.clear()
        add_log("Resolve cache temizlendi")

async def fetch_channels():
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        r = await client.get("https://vavoo.to/live2/index?output.json")
        data = r.json()
        channels = []
        if isinstance(data, list):
            channels = data
        elif isinstance(data, dict):
            for key in ["channels", "data", "items", "list", "results"]:
                if key in data and isinstance(data[key], list):
                    channels = data[key]
                    break
        add_log(f"API: {len(channels)} kanal")
        return channels

def detect_country(ch):
    name = ch.get("name", "")
    group = ch.get("group", "")
    tvg_id = ch.get("tvg_id", "")
    n = name.upper()
    g = group.upper()
    t = tvg_id.lower() if tvg_id else ""
    is_tr = any(k in n for k in ["TR:", "TR ", "TURK", "4K TR", "FHD TR", "HD TR"])
    is_de = any(k in n for k in ["DE:", "DE ", "GERMAN", "4K DE", "FHD DE", "HD DE"])
    if any(k in g for k in ["TURKEY", "TURKIYE"]): is_tr = True
    if any(k in g for k in ["GERMANY", "DEUTSCH"]): is_de = True
    if t.endswith(".de"): is_de = True
    if t.endswith(".tr"): is_tr = True
    if is_tr and is_de: return "BOTH"
    if is_tr: return "TR"
    if is_de: return "DE"
    return ""

def remap_group(name, original_group=""):
    n = name.upper()
    g = original_group.upper()
    combined = n + " " + g
    if any(k in combined for k in ["ULUSAL","SHOW TV","STAR TV","KANAL D","ATV","FOX TV","TV8","TRT 1","A2 TV","A2 HD","TEVE2","TV100","BLOOMBERG HT","TV A","TLC","BEYAZ TV","FLASH TV","KANAL 7","HALK TV","TELE1","ULKE TV"]):
        return "TR ULUSAL"
    if any(k in combined for k in ["HABER","NEWS","CNBC","NTV","BLOOMBERG","AHABER","CNN TURK","HABER TURK","TGRT HABER"]):
        return "TR HABER"
    if any(k in combined for k in ["BELGESEL","DOC","DISCOVERY","NAT GEO","HISTORY","ANIMAL","DA VINCI","YABAN","TRT BELGESEL","VIASAT EXPLORE","VIASAT HISTORY"]):
        return "TR BELGESEL"
    if any(k in combined for k in ["COCUK","CARTOON","MINIKA","TRT COCUK","BABY","DISNEY","NICKELODEON","KIDS","BOOMERANG"]):
        return "TR COCUK"
    if any(k in combined for k in ["FILM","MOVIE","SINEMA","CINE","YESILCAM","DIZI","SERIES","FILMBOX"]):
        return "TR FILM"
    if any(k in combined for k in ["MUZIK","MUSIC","KRAL","POWER","NUMBER ONE","DREAM","NR1","TMB"]):
        return "TR MUZIK"
    if any(k in combined for k in ["SPOR","SPORT","BEIN","TIVIBU","DSMART","TRT SPOR","A SPOR","EUROSPORT"]):
        return "TR SPOR"
    if any(k in combined for k in ["DINI","DIN","LALE","SEMERKAND","HILAL","MEKKE","KURAN"]):
        return "TR DINI"
    if any(k in combined for k in ["RADYO","RADIO"]):
        return "TR RADYO"
    if any(k in combined for k in ["ARD","ZDF","ARTE","WDR","NDR","MDR","SWR","RBB","PHOENIX","TAGESSCHAU","3SAT","KIKA","ONE","ZDFNEO","PROSIEBEN","SAT.1","RTL","VOX","KABEL1"]):
        return "DE VOLLPROGRAMM"
    if any(k in combined for k in ["NACHRICHTEN","WELT","SPIEGEL"]):
        return "DE NACHRICHTEN"
    if any(k in combined for k in ["DOKU","DOKUMENTATION"]):
        return "DE DOKU"
    if any(k in combined for k in ["KINDER","CHILDREN"]):
        return "DE KINDER"
    if any(k in combined for k in ["DE: FILM","DE: MOVIE","DE: SKY","DE: AXN"]):
        return "DE FILM"
    if any(k in combined for k in ["DE: SPORT","DE: EUROSPORT","DE: SKY SPORT","DE: BEIN"]):
        return "DE SPORT"
    if any(k in combined for k in ["DE:","DE ","GERMAN","4K DE","FHD DE","HD DE"]):
        return "DE SONSTIGE"
    return "TR YEREL"

def clean_name(name):
    n = name.upper()
    for remove in [" (1)", " (2)", " (3)", " (4)", " (5)", "(BACKUP)", "+", " HEVC", " RAW", " SD", " FHD", " UHD", " 4K", " H265"]:
        n = n.replace(remove, "")
    n = re.sub(r'\([^)]*\)', '', n)
    n = n.strip()
    return n

async def startup_sequence():
    global DATA_READY
    add_log("=== VxParser Basliyor ===")
    init_db()
    add_log("Veritabani hazir")
    add_log("Lokke signature aliniyor...")
    sig = await refresh_watched_sig(force=True)
    if sig:
        add_log("Lokke signature BASARILI!")
    else:
        add_log("Lokke signature BASARISIZ")
    add_log("Kanallar cekiliyor...")
    try:
        channels = await fetch_channels()
    except Exception as e:
        add_log(f"Kanal hatasi: {e}")
        return
    if not channels:
        add_log("HATA: 0 kanal!")
        return
    add_log(f"Toplam {len(channels)} kanal")
    filtered = []
    for ch in channels:
        country = detect_country(ch)
        if country in ("TR", "DE", "BOTH"):
            name = ch.get("name", "Unknown")
            url = ch.get("url", "")
            logo = ch.get("logo", "")
            group = ch.get("group", "")
            grp = remap_group(name, group)
            ch_id = 0
            m = re.search(r'/play\d+/(\d+)\.m3u8', url)
            if m:
                ch_id = int(m.group(1))
            if ch_id == 0:
                ch_id = abs(hash(name)) % 9999999
            filtered.append({"id": ch_id, "name": name, "url": url, "hls": "", "grp": grp, "country": country if country != "BOTH" else "TR", "logo": logo, "clean_name": clean_name(name)})
    tr_count = sum(1 for c in filtered if c["country"] == "TR")
    de_count = sum(1 for c in filtered if c["country"] == "DE")
    add_log(f"Filtrelenmis: {len(filtered)} (TR={tr_count}, DE={de_count})")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM channels")
    for ch in filtered:
        c.execute("INSERT OR REPLACE INTO channels (id,name,url,hls,grp,country,logo) VALUES (?,?,?,?,?,?,?)",
            (ch["id"], ch["name"], ch["url"], ch["hls"], ch["grp"], ch["country"], ch["logo"]))
    conn.commit()
    conn.close()
    add_log(f"DB kaydedildi: {len(filtered)} kanal")
    if sig:
        add_log("MediaHubMX catalog cekiliyor...")
        try:
            id_lookup = {}
            for ch in filtered:
                m = re.search(r'/play\d+/(\d+)\.m3u8', ch["url"])
                if m:
                    sid = m.group(1)
                    id_lookup[sid] = ch["id"]
                    for l in range(len(sid), max(4, len(sid)-8), -1):
                        id_lookup[sid[:l]] = ch["id"]
            total_hls = 0
            for gn in ["Turkey", "Germany"]:
                add_log(f"Catalog: {gn}...")
                items = await fetch_all_catalog(gn)
                add_log(f"Catalog {gn}: {len(items)} item")
                for item in items:
                    cat_url = item.get("url", "")
                    cat_name = item.get("name", "")
                    if not cat_url:
                        continue
                    u = re.sub(r'.*/', '', cat_url)
                    uid = u[:max(4, len(u)-12)] if len(u) > 12 else u
                    matched_id = None
                    if uid in id_lookup:
                        matched_id = id_lookup[uid]
                    if not matched_id:
                        for sid, db_id in id_lookup.items():
                            if sid in cat_url:
                                matched_id = db_id
                                break
                    if not matched_id:
                        cat_clean = clean_name(cat_name)
                        for ch in filtered:
                            if ch["clean_name"] == cat_clean:
                                matched_id = ch["id"]
                                break
                    if matched_id:
                        update_channel_hls(matched_id, cat_url)
                        total_hls += 1
            add_log(f"HLS eslesme: {total_hls} kanal")
        except Exception as e:
            add_log(f"Catalog hatasi: {e}")
    DATA_READY = True
    add_log("=== VxParser HAZIR ===")
