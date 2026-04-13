"""
state.py - VxParser State Management
Token management, channel resolve, database operations
Multi-domain fallback, 30min cooldown for failed pings
"""
import sqlite3
import httpx
import time
import logging
import json
import re
import asyncio
import os

log = logging.getLogger("vxparser")

# ===== CONFIG =====
CONFIG = {
    "PING_URLS": [
        "https://www.vavoo.tv/api/app/ping",
        "https://www.lokke.app/api/app/ping"
    ],
    "BASE_URLS": [
        "https://vavoo.to",
        "https://kool.to",
        "https://oha.to"
    ],
    "APP_VERSION": "3.1.8",
    "CATALOG_GROUPS": ["Turkey", "Germany"],
}

# ===== GLOBALS =====
DATA_READY = False
STARTUP_LOGS = []
VAVOO_TOKEN = ""
VAVOO_TOKEN_EXPIRES = 0
VAVOO_TOKEN_COOLDOWN = 0  # timestamp when cooldown ends
WATCHED_SIG = ""
DB_PATH = "/tmp/vxparser.db"
RESOLVE_CACHE = {}
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")
ADMIN_SESSIONS = {}
CATALOG_DOMAIN = CONFIG["BASE_URLS"][0]  # working catalog domain
RESOLVE_DOMAIN = CONFIG["BASE_URLS"][0]  # working resolve domain
LIVE2_DOMAIN = CONFIG["BASE_URLS"][0]    # working live2 domain

def add_log(msg):
    log.info(msg)
    ts = time.strftime('%H:%M:%S')
    STARTUP_LOGS.append(f"[{ts}] {msg}")
    if len(STARTUP_LOGS) > 200:
        STARTUP_LOGS.pop(0)

# ===== DATABASE =====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY,
        name TEXT,
        url TEXT,
        hls TEXT DEFAULT '',
        grp TEXT DEFAULT '',
        country TEXT DEFAULT '',
        logo TEXT DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS overrides (
        ch_id INTEGER PRIMARY KEY,
        url TEXT,
        enabled INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        msg TEXT
    )""")
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

def set_override(ch_id, url, enabled=1):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO overrides (ch_id, url, enabled) VALUES (?,?,?)", (ch_id, url, enabled))
    conn.commit()
    conn.close()

def get_override(ch_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM overrides WHERE ch_id = ?", (ch_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_overrides():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT o.*, c.name as ch_name FROM overrides o LEFT JOIN channels c ON o.ch_id = c.id ORDER BY o.ch_id")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

# ===== LOKKE SIGNATURE =====
async def get_watched_sig():
    """Get mediahubmx-signature from Lokke (POST with full body)"""
    global WATCHED_SIG
    if WATCHED_SIG:
        return WATCHED_SIG
    try:
        now_ms = int(time.time()) * 1000
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
        }
        data = {
            "token": "",
            "reason": "boot",
            "locale": "de",
            "theme": "dark",
            "metadata": {
                "device": {"type": "desktop", "uniqueId": ""},
                "os": {"name": "win32", "version": "Windows 10", "abis": ["x64"], "host": "DESKTOP-VX"},
                "app": {"platform": "electron"},
                "version": {"package": "app.lokke.main", "binary": "1.0.19", "js": "1.0.19"}
            },
            "appFocusTime": 173,
            "playerActive": False,
            "playDuration": 0,
            "devMode": True,
            "hasAddon": True,
            "castConnected": False,
            "package": "app.lokke.main",
            "version": "1.0.19",
            "process": "app",
            "firstAppStart": now_ms,
            "lastAppStart": now_ms,
            "ipLocation": 0,
            "adblockEnabled": True,
            "proxy": {"supported": ["ss"], "engine": "cu", "enabled": False, "autoServer": True, "id": 0},
            "iap": {"supported": False}
        }
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.post("https://www.lokke.app/api/app/ping", json=data, headers=headers)
            result = r.json()
            sig = result.get("addonSig")
            if sig:
                WATCHED_SIG = sig
                add_log(f"Lokke addonSig alindi ({len(sig)} char)")
                return sig
            else:
                add_log(f"Lokke: addonSig bulunamadi")
    except Exception as e:
        add_log(f"Lokke hata: {e}")
    return ""

# ===== VAVOO TOKEN (with 30min cooldown) =====
async def get_vavoo_token():
    """Get Vavoo auth token from ping2 (4-domain fallback + 30min cooldown)"""
    global VAVOO_TOKEN, VAVOO_TOKEN_EXPIRES, VAVOO_TOKEN_COOLDOWN

    # Still valid?
    if VAVOO_TOKEN and time.time() < VAVOO_TOKEN_EXPIRES:
        return VAVOO_TOKEN

    # Cooldown active?
    if time.time() < VAVOO_TOKEN_COOLDOWN:
        return ""

    for base_url in CONFIG["BASE_URLS"]:
        try:
            ping_url = base_url + "/api/app/ping2"
            headers = {
                "user-agent": f"Vavoo/{CONFIG['APP_VERSION']} (Linux; Android 14)",
                "accept": "application/json",
                "content-type": "application/json; charset=utf-8",
                "origin": base_url,
                "referer": base_url + "/",
            }
            data = {
                "device": {
                    "uniqueId": "vx-" + os.urandom(8).hex(),
                    "model": "SM-S918B",
                    "os": "android",
                    "osVersion": "14",
                    "appVersion": CONFIG["APP_VERSION"]
                }
            }
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                r = await client.post(ping_url, json=data, headers=headers)
                if r.status_code == 200:
                    result = r.json()
                    token = result.get("token", "")
                    if token:
                        VAVOO_TOKEN = token
                        VAVOO_TOKEN_EXPIRES = time.time() + 3600
                        VAVOO_TOKEN_COOLDOWN = 0
                        add_log(f"Vavoo token alindi ({base_url})")
                        return token
                    else:
                        add_log(f"Vavoo {base_url}: token bos")
                else:
                    add_log(f"Vavoo {base_url}: HTTP {r.status_code}")
        except Exception as e:
            add_log(f"Vavoo {base_url}: {e}")

    # All domains failed - set 30min cooldown
    VAVOO_TOKEN_COOLDOWN = time.time() + 1800
    add_log("Vavoo token BASARISIZ - 30dk cooldown aktif")
    return ""

# ===== RESOLVE CHANNEL =====
async def resolve_channel(ch_id):
    """
    Resolve a channel to a playable stream URL.
    
    IMPORTANT: live2/play3 URLs return 404 when fetched directly!
    They MUST be resolved through MediaHubMX resolve endpoint.
    
    Priority:
    1. Override (manual URL)
    2. Y1: HLS catalog URL + MediaHubMX resolve (addonSig) - BEST
    3. Y1.5: live2 URL + MediaHubMX resolve (addonSig)
    4. Y2: live2 URL + direct proxy (likely 404, last resort)
    """
    ch = get_channel(ch_id)
    if not ch:
        return {"success": False, "error": "Kanal bulunamadi", "channel_id": ch_id}

    # Check override first
    ov = get_override(ch_id)
    if ov and ov.get("enabled") and ov.get("url"):
        add_log(f"[{ch_id}] Override: {ch.get('name','')}")
        return {"success": True, "method": "Override", "url": ov["url"], "channel_id": ch_id}

    ch_name = ch.get("name", "")
    url = ch.get("url", "")
    hls = ch.get("hls", "")

    # Y1: Resolve HLS catalog URL through MediaHubMX (BEST - has proper stream URLs)
    if hls:
        add_log(f"[{ch_id}] Y1-Resolve deneniyor (catalog HLS): {ch_name}")
        resolved = await resolve_mediahubmx(hls)
        if resolved:
            add_log(f"[{ch_id}] Y1-Resolve BASARILI: {ch_name} -> {resolved[:80]}")
            return {"success": True, "method": f"Y1-Resolve: {ch_name}", "url": resolved, "channel_id": ch_id}
        add_log(f"[{ch_id}] Y1-Resolve basarisiz: {ch_name}")

    # Y1.5: Resolve live2 URL through MediaHubMX
    if url:
        add_log(f"[{ch_id}] Y1.5-Resolve deneniyor (live2 URL): {ch_name}")
        resolved = await resolve_mediahubmx(url)
        if resolved:
            add_log(f"[{ch_id}] Y1.5-Resolve BASARILI: {ch_name} -> {resolved[:80]}")
            return {"success": True, "method": f"Y1.5-Resolve: {ch_name}", "url": resolved, "channel_id": ch_id}
        add_log(f"[{ch_id}] Y1.5-Resolve basarisiz: {ch_name}")

    # Y2: Direct proxy of live2 URL (likely 404 but try anyway)
    if url:
        add_log(f"[{ch_id}] Y2-Direct (son care): {ch_name} - muhtemelen calismaz!")
        return {"success": True, "method": f"Y2-Direct: {ch_name}", "url": url, "channel_id": ch_id}

    return {"success": False, "error": "Resolve edilemedi (HLS yok, URL yok)", "method": "FAILED", "channel_id": ch_id}

# ===== MEDIAHUBMX RESOLVE =====
async def resolve_mediahubmx(url):
    """Resolve a stream URL via MediaHubMX (multi-domain fallback)"""
    global WATCHED_SIG
    if not WATCHED_SIG:
        WATCHED_SIG = await get_watched_sig()
    if not WATCHED_SIG:
        return None

    for domain in CONFIG["BASE_URLS"]:
        try:
            headers = {
                "user-agent": "MediaHubMX/2",
                "accept": "application/json",
                "content-type": "application/json; charset=utf-8",
                "mediahubmx-signature": WATCHED_SIG,
            }
            data = {
                "language": "de",
                "region": "AT",
                "url": url,
                "clientVersion": "3.0.2"
            }
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                endpoint = f"{domain}/mediahubmx-resolve.json"
                r = await client.post(endpoint, json=data, headers=headers)
                if r.status_code == 200:
                    result = r.json()
                    if isinstance(result, list) and len(result) > 0:
                        resolved_url = result[0].get("url", "")
                        if resolved_url:
                            return resolved_url
                else:
                    add_log(f"Resolve {domain}: HTTP {r.status_code}")
        except Exception as e:
            add_log(f"Resolve {domain}: {e}")
    return None

# ===== CATALOG FETCH (multi-domain) =====
async def fetch_catalog(group, cursor=0):
    """Fetch MediaHubMX catalog for a group (domain fallback)"""
    global WATCHED_SIG
    if not WATCHED_SIG:
        return {}

    for domain in CONFIG["BASE_URLS"]:
        try:
            headers = {
                "user-agent": "MediaHubMX/2",
                "accept": "application/json",
                "content-type": "application/json; charset=utf-8",
                "mediahubmx-signature": WATCHED_SIG,
            }
            data = {
                "language": "de",
                "region": "AT",
                "catalogId": "iptv",
                "id": "iptv",
                "adult": False,
                "search": "",
                "sort": "name",
                "filter": {"group": group},
                "cursor": cursor,
                "clientVersion": "3.0.2"
            }
            async with httpx.AsyncClient(timeout=20, verify=False) as client:
                endpoint = f"{domain}/mediahubmx-catalog.json"
                r = await client.post(endpoint, json=data, headers=headers)
                if r.status_code == 200:
                    result = r.json()
                    if isinstance(result, dict) and "items" in result:
                        CATALOG_DOMAIN = domain
                        return result
                    else:
                        add_log(f"Catalog {domain}/{group}: unexpected format")
                else:
                    add_log(f"Catalog {domain}/{group}: HTTP {r.status_code}")
        except Exception as e:
            add_log(f"Catalog {domain}/{group}: {e}")
    return {}

async def fetch_all_catalog(group_name):
    """Fetch all pages of a catalog"""
    all_items = []
    cursor = 0
    for _ in range(50):  # max 50 pages safety
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

# ===== CHANNEL FETCHING =====
async def fetch_channels():
    """Fetch channels from live2 (domain fallback)"""
    global LIVE2_DOMAIN
    for domain in CONFIG["BASE_URLS"]:
        try:
            url = f"{domain}/live2/index?output.json"
            async with httpx.AsyncClient(timeout=30, verify=False) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    data = r.json()
                    channels = []
                    if isinstance(data, list):
                        channels = data
                    elif isinstance(data, dict):
                        for key in ["channels", "data", "items", "list", "results"]:
                            if key in data and isinstance(data[key], list):
                                channels = data[key]
                                break
                    if channels:
                        LIVE2_DOMAIN = domain
                        add_log(f"live2: {len(channels)} kanal ({domain})")
                        return channels
                else:
                    add_log(f"live2 {domain}: HTTP {r.status_code}")
        except Exception as e:
            add_log(f"live2 {domain}: {e}")
    return []

# ===== GROUP/COUNTRY DETECTION =====
def detect_country(ch):
    name = ch.get("name", "")
    group = ch.get("group", "")
    tvg_id = ch.get("tvg_id", "")
    n = name.upper()
    g = group.upper()
    t = tvg_id.lower() if tvg_id else ""
    is_tr = False
    is_de = False
    if any(k in n for k in ["TR:", "TR ", "TURK", "4K TR", "FHD TR", "HD TR"]):
        is_tr = True
    if any(k in n for k in ["DE:", "DE ", "GERMAN", "4K DE", "FHD DE", "HD DE"]):
        is_de = True
    if any(k in g for k in ["TURKEY", "TURKIYE", "TR ", "TR:"]):
        is_tr = True
    if any(k in g for k in ["GERMANY", "DEUTSCH", "DE ", "DE:"]):
        is_de = True
    if t.endswith(".de"):
        is_de = True
    if t.endswith(".tr"):
        is_tr = True
    if is_tr and is_de:
        return "BOTH"
    if is_tr:
        return "TR"
    if is_de:
        return "DE"
    return ""

def remap_group(name, original_group=""):
    n = name.upper()
    g = original_group.upper()
    combined = n + " " + g
    if any(k in combined for k in ["ULUSAL","SHOW TV","STAR TV","KANAL D","ATV","FOX TV","TV8","TRT 1","A2 TV","A2 HD","TEVE2","TV100","BLOOMBERG HT","TV A","TLC","BEYAZ TV","FLASH TV","KANAL 7","HALK TV","TELE1","ULKE TV"]):
        return "TR ULUSAL"
    if any(k in combined for k in ["HABER","NEWS","CNBC","NTV","BLOOMBERG","AHABER","CNN TURK","HABER TURK","TGRT HABER"]):
        return "TR HABER"
    if any(k in combined for k in ["BELGESEL","DOC","DISCOVERY","NAT GEO","NATIONAL GEO","HISTORY","ANIMAL","DA VINCI","YABAN","AV ","TRT BELGESEL","VIASAT EXPLORE","VIASAT HISTORY"]):
        return "TR BELGESEL"
    if any(k in combined for k in ["COCUK","CARTOON","MINIKA","TRT COCUK","BABY","DISNEY","NICKELODEON","KIDS","BOOMERANG","KINDER"]):
        return "TR COCUK"
    if any(k in combined for k in ["FILM","MOVIE","SINEMA","CINE","YESILCAM","DIZI","SERIES","FILMBOX","D-SMART","MOVIES","CINEMA"]):
        return "TR FILM"
    if any(k in combined for k in ["MUZIK","MUSIC","KRAL","POWER","NUMBER ONE","DREAM","NR1","TMB"]):
        return "TR MUZIK"
    if any(k in combined for k in ["SPOR","SPORT","BEIN","TIVIBU","DSMART","TRT SPOR","A SPOR","EUROSPORT","SPORTS"]):
        return "TR SPOR"
    if any(k in combined for k in ["DINI","DIN","LALE","SEMERKAND","HILAL","MEKKE","KURAN"]):
        return "TR DINI"
    if any(k in combined for k in ["RADYO","RADIO"]):
        return "TR RADYO"
    if any(k in combined for k in ["ARD","ZDF","ARTE","WDR","NDR","MDR","SWR","BR ","HR ","RB ","SR ","RBB","PHOENIX","TAGESSCHAU","3SAT","KIKA","ONE","ZDFNEO","ZDFINFO","PROSIEBEN","SAT.1","RTL","VOX","KABEL1","SUPER RTL"]):
        return "DE VOLLPROGRAMM"
    if any(k in combined for k in ["NACHRICHTEN","NTV DE","WELT","CNN DE","SPIEGEL","TAGESSPIEGEL"]):
        return "DE NACHRICHTEN"
    if any(k in combined for k in ["DOKU","DOKUMENTATION","DOCUMENTARY"]):
        return "DE DOKU"
    if any(k in combined for k in ["KINDER","CHILDREN"]):
        return "DE KINDER"
    if any(k in combined for k in ["DE: FILM","DE: MOVIE","DE: CINE","DE: SKY","DE: AXN","DE: TNT"]):
        return "DE FILM"
    if any(k in combined for k in ["DE: MUSIK","DE: MTV"]):
        return "DE MUSIK"
    if any(k in combined for k in ["DE: SPORT","DE: EUROSPORT","DE: SKY SPORT","DE: BEIN","DE: DAZN"]):
        return "DE SPORT"
    return "TR YEREL"

def clean_name(name):
    n = name.upper()
    for remove in [" (1)", " (2)", " (3)", " (4)", " (5)", "(BACKUP)", "+", " HEVC", " RAW", " SD", " FHD", " UHD", " 4K", " H265", " HD", " FHD", " UHD", " 1080", " 720", " AUSTRIA", " AT"]:
        n = n.replace(remove, "")
    n = re.sub(r'\([^)]*\)', '', n)
    n = re.sub(r'\[[^\]]*\]', '', n)
    return n.strip()

# ===== STARTUP SEQUENCE =====
async def startup_sequence():
    global DATA_READY
    add_log("=== VxParser Basliyor ===")
    init_db()
    add_log("Veritabani hazir")

    # 1. Lokke signature
    add_log("Lokke signature aliniyor...")
    sig = await get_watched_sig()
    if sig:
        add_log("Lokke signature: OK")
    else:
        add_log("Lokke signature: BASARISIZ")

    # 2. Vavoo token
    add_log("Vavoo token aliniyor...")
    token = await get_vavoo_token()
    if token:
        add_log("Vavoo token: OK")
    else:
        add_log("Vavoo token: BASARISIZ (Y3-Direct yine calisir)")

    # 3. Fetch channels
    add_log("Kanallar cekiliyor (live2)...")
    channels = await fetch_channels()
    if not channels:
        add_log("HATA: 0 kanal cekildi!")
        DATA_READY = True
        return

    add_log(f"Toplam {len(channels)} kanal cekildi")

    # 4. Filter TR + DE
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
            filtered.append({
                "id": ch_id,
                "name": name,
                "url": url,
                "hls": "",
                "grp": grp,
                "country": country if country != "BOTH" else "TR",
                "logo": logo,
                "clean_name": clean_name(name)
            })

    tr_count = sum(1 for c in filtered if c["country"] == "TR")
    de_count = sum(1 for c in filtered if c["country"] == "DE")
    add_log(f"Filtrelenmis: {len(filtered)} (TR={tr_count}, DE={de_count})")

    # 5. Save to DB
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM channels")
    for ch in filtered:
        c.execute("INSERT OR REPLACE INTO channels (id,name,url,hls,grp,country,logo) VALUES (?,?,?,?,?,?,?)",
            (ch["id"], ch["name"], ch["url"], ch["hls"], ch["grp"], ch["country"], ch["logo"]))
    conn.commit()
    conn.close()
    add_log(f"Veritabanina kaydedildi: {len(filtered)} kanal")

    # 6. Try catalog for HLS links (best-effort, non-blocking)
    if sig:
        add_log("MediaHubMX catalog deneniyor (best-effort)...")
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
            for group_name in CONFIG["CATALOG_GROUPS"]:
                add_log(f"Catalog: {group_name}...")
                items = await fetch_all_catalog(group_name)
                add_log(f"Catalog {group_name}: {len(items)} item")

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

            add_log(f"HLS linkleri: {total_hls} kanal eslesti")
        except Exception as e:
            add_log(f"Catalog hatasi: {e}")

    DATA_READY = True
    add_log("=== VxParser HAZIR (Y3-Direct aktif) ===")
