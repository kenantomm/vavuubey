"""
state.py - Ortak state modulu.
Token fonksiyonlari ve resolve fonksiyonlari burada.
server.py ve video.py sadece state import eder, BIRBIRINI IMPORT ETMEZ.

v3.4.0 - Catalog/Resolve Validation error fix, clientVersion update
"""
import os
import random
import time
import json
import sqlite3
import requests
import urllib3
import threading

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIG - Tum URL'ler ve ayarlar
# ============================================================
CONFIG = {
    # addonSig (MediaHubMX imzasi) almak icin ping endpointleri
    "PING_URLS": [
        "https://www.vavoo.tv/api/app/ping",
        "https://www.lokke.app/api/app/ping",
    ],
    # API cagrilari icin base URL'ler (fallback sirasiyla)
    "BASE_URLS": [
        "https://vavoo.to",
        "https://kool.to",
        "https://oha.to",
    ],
    # vavoo_auth token almak icin ping2 endpointleri
    "PING2_URLS": [
        "https://www.vavoo.to/api/box/ping2",
        "https://www.vavoo.tv/api/box/ping2",
        "https://kool.to/api/box/ping2",
        "https://oha.to/api/box/ping2",
    ],
    # live2 kanal listesi icin URL'ler
    "LIVE2_URLS": [
        "https://www.vavoo.to/live2/index?output=json",
        "https://kool.to/live2/index?output=json",
        "https://oha.to/live2/index?output=json",
    ],
    # Cache suresi (sn)
    "SIG_CACHE_TTL": 8 * 60,         # 8 dakika (basarili)
    "SIG_FAIL_TTL": 3 * 60,          # 3 dakika (basarisiz - HIZLI retry!)
    "RESOLVE_CACHE_TTL": 45 * 60,    # 45 dakika - CDN URL suresinden once yenile
    "RESOLVE_TIMEOUT": 15,           # 15 saniye
    "CDN_USER_AGENT": "VAVOO/2.6",
    "API_USER_AGENT": "MediaHubMX/2",
    "APP_VERSION": "3.0.2",
}

# ============================================================
# PAYLASILAN STATE
# ============================================================
DATA_READY = False
STARTUP_ERROR = None
LOAD_TIME = 0
STARTUP_LOGS = []

DB_PATH = "/tmp/vxparser.db"
M3U_PATH = "/tmp/playlist.m3u"
PORT = 10000

# Token cache
_vavoo_sig = None
_vavoo_sig_time = 0
_vavoo_sig_failed = False     # HATA durumunda cache
_watched_sig = None
_watched_sig_time = 0
_watched_sig_failed = False   # HATA durumunda cache

# ============================================================
# RESOLVE CACHE (TTL destekli) - 1 saat bug fix
# ============================================================
_resolve_cache = {}            # {lid: {"url": str, "time": float, "method": str}}
_resolve_cache_lock = threading.Lock()
_resolve_stats = {"hits": 0, "misses": 0, "expired": 0, "errors": 0}


def get_resolve_cache_info():
    """Cache durumu hakkinda bilgi dondur."""
    now = time.time()
    active = 0
    expired = 0
    with _resolve_cache_lock:
        for lid, entry in _resolve_cache.items():
            if (now - entry["time"]) < CONFIG["RESOLVE_CACHE_TTL"]:
                active += 1
            else:
                expired += 1
    return {
        "total_cached": len(_resolve_cache),
        "active": active,
        "expired": expired,
        "ttl_seconds": CONFIG["RESOLVE_CACHE_TTL"],
        "hits": _resolve_stats["hits"],
        "misses": _resolve_stats["misses"],
        "expired_count": _resolve_stats["expired"],
        "errors": _resolve_stats["errors"],
    }


def clear_resolve_cache():
    """Tum resolve cache'i temizle."""
    with _resolve_cache_lock:
        _resolve_cache.clear()
        _resolve_stats["hits"] = 0
        _resolve_stats["misses"] = 0
        _resolve_stats["expired"] = 0
        _resolve_stats["errors"] = 0


def slog(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    STARTUP_LOGS.append(entry)
    print(entry)


# ============================================================
# 1. VAVOO TOKEN (ping2) - vavoo_auth icin
# ============================================================
def get_auth_signature():
    global _vavoo_sig, _vavoo_sig_time, _vavoo_sig_failed

    # Basarili cache
    if _vavoo_sig and (time.time() - _vavoo_sig_time) < CONFIG["SIG_CACHE_TTL"]:
        return _vavoo_sig

    # BASARISIZ cache - 3 dk boyunca tekrar deneme
    if _vavoo_sig_failed and (time.time() - _vavoo_sig_time) < CONFIG["SIG_FAIL_TTL"]:
        return None

    slog("Vavoo Token (ping2) aliniyor...")
    headers = {"User-Agent": CONFIG["CDN_USER_AGENT"], "Accept": "application/json"}
    try:
        vec_req = requests.get("http://mastaaa1987.github.io/repo/veclist.json", headers=headers, timeout=10, verify=False)
        veclist = vec_req.json()["value"]
        slog(f"veclist: {len(veclist)} vec")

        sig = None
        for ping_url in CONFIG["PING2_URLS"]:
            if sig:
                break
            for i in range(3):
                vec = {"vec": random.choice(veclist)}
                try:
                    req = requests.post(ping_url, data=vec, headers=headers, timeout=10, verify=False).json()
                    if req.get("signed"):
                        sig = req["signed"]
                        slog(f"  Token alindi: {ping_url}")
                        break
                except Exception:
                    continue

        # Cache sonucu
        _vavoo_sig_time = time.time()
        if sig:
            _vavoo_sig = sig
            _vavoo_sig_failed = False
            slog("Vavoo Token alindi!")
            return sig
        else:
            _vavoo_sig = None
            _vavoo_sig_failed = True
            slog(f"Vavoo Token ALINAMADI ({CONFIG['SIG_FAIL_TTL']}s bekleyecek)")
    except Exception as e:
        _vavoo_sig = None
        _vavoo_sig_failed = True
        _vavoo_sig_time = time.time()
        slog(f"Vavoo Token HATASI: {e}")
    return None


# ============================================================
# 2. ADDONSIG (app/ping) - MediaHubMX imzasi icin
# ============================================================
def get_watchedsig(force=False):
    """
    addonSig al. force=True olursa cache'i atla ve her zaman yeni al.
    """
    global _watched_sig, _watched_sig_time, _watched_sig_failed

    # Basarili cache (force ile atla)
    if not force and _watched_sig and (time.time() - _watched_sig_time) < CONFIG["SIG_CACHE_TTL"]:
        return _watched_sig

    # BASARISIZ cache (force ile atla)
    if not force and _watched_sig_failed and (time.time() - _watched_sig_time) < CONFIG["SIG_FAIL_TTL"]:
        return None

    tag = " (FORCE)" if force else ""
    slog(f"addonSig (app/ping) aliniyor{tag}...")
    headers = {"user-agent": "okhttp/4.11.0", "accept": "application/json", "content-type": "application/json; charset=utf-8"}
    data = {
        "token": "", "reason": "boot", "locale": "de", "theme": "dark",
        "metadata": {
            "device": {"type": "desktop", "uniqueId": ""},
            "os": {"name": "linux", "version": "Ubuntu 22.04", "abis": ["x64"], "host": "RENDER"},
            "app": {"platform": "electron"},
            "version": {"package": "app.lokke.main", "binary": "1.0.19", "js": "1.0.19"},
        },
        "appFocusTime": 173, "playerActive": False, "playDuration": 0,
        "devMode": True, "hasAddon": True, "castConnected": False,
        "package": "app.lokke.main", "version": "1.0.19", "process": "app",
        "firstAppStart": int(time.time() * 1000) - 10000,
        "lastAppStart": int(time.time() * 1000) - 10000,
        "ipLocation": 0, "adblockEnabled": True,
        "proxy": {"supported": ["ss"], "engine": "cu", "enabled": False, "autoServer": True, "id": 0},
        "iap": {"supported": False},
    }

    for ping_url in CONFIG["PING_URLS"]:
        try:
            resp = requests.post(ping_url, json=data, headers=headers, timeout=15, verify=False)
            result = resp.json()
            sig = result.get("addonSig")
            if sig:
                _watched_sig = sig
                _watched_sig_time = time.time()
                _watched_sig_failed = False
                slog(f"  addonSig alindi{tag}: {ping_url}")
                return sig
        except Exception:
            continue

    _watched_sig = None
    _watched_sig_failed = True
    _watched_sig_time = time.time()
    slog(f"addonSig ALINAMADI ({CONFIG['SIG_FAIL_TTL']}s bekleyecek)")
    return None


# ============================================================
# 3. HLS RESOLVE (mediahubmx) - Tum BASE_URL fallback
# ============================================================
def resolve_hls_link(link, force_sig=False):
    """HLS linkini cozen fonksiyon. force_sig=True olursa addonSig'i yenile."""
    sig = get_watchedsig(force=force_sig)
    if not sig:
        return None

    headers = {
        "user-agent": CONFIG["API_USER_AGENT"],
        "accept": "application/json",
        "content-type": "application/json; charset=utf-8",
        "mediahubmx-signature": sig,
    }
    data = {
        "language": "de", "region": "AT",
        "url": link,
        "clientVersion": CONFIG["APP_VERSION"],
    }

    last_error = ""
    for base in CONFIG["BASE_URLS"]:
        try:
            url = f"{base}/mediahubmx-resolve.json"
            r = requests.post(url, json=data, headers=headers, timeout=CONFIG["RESOLVE_TIMEOUT"], verify=False)
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
                try:
                    err_body = r.text[:200]
                    slog(f"  Resolve {r.status_code} ({base}): {err_body}")
                except Exception:
                    pass
                continue
            result = r.json()
            if result and isinstance(result, list) and len(result) > 0:
                resolved = result[0].get("url")
                if resolved:
                    return resolved
            elif isinstance(result, dict):
                last_error = result.get("error", str(result))[:100]
                slog(f"  Resolve bos ({base}): {last_error}")
        except Exception as e:
            last_error = str(e)[:80]
            continue

    return None


# ============================================================
# 4. CHANNEL RESOLVE (Cache + TTL) - video.py'den cagrilir
# ============================================================
def resolve_channel(lid):
    """
    Kanal ID'sine gore stream URL'ini coz.
    
    RESOLVE CACHE MEKANIZMASI (v3.3.0):
    - Basarili resolve'ler 45 dakika cache'lenir
    - Cache suresi dolunca otomatik yeniden resolve
    - Cache miss/expired durumunda Y1 -> Y1.5 -> Y2 -> Y3 sirasiyla denenir
    - Y1/Y1.5 basarisiz olursa addonSig 1 kez force-refresh edilip tekrar denenir
    - Basarisiz sonuclar cache'lenmez (her istekte tekrar denenir)
    
    Y1:   HLS field + Lokke resolve (catalog'tan gelen) - EN IYI
    Y1.5: URL field + Lokke resolve
    Y2:   URL + vavoo_auth token
    Y3:   Direkt URL (son care - genelde calismaz)
    """
    # --- CACHE CHECK ---
    now = time.time()
    cached = None
    with _resolve_cache_lock:
        if lid in _resolve_cache:
            entry = _resolve_cache[lid]
            age = now - entry["time"]
            if age < CONFIG["RESOLVE_CACHE_TTL"]:
                _resolve_stats["hits"] += 1
                return entry["url"], f"CACHE ({entry['method']}): {entry['name']} [{int(age)}s]"
            else:
                _resolve_stats["expired"] += 1
                cached = entry  # Eski cache var ama suresi doldu

    # --- DB'DEN KANAL BILGISI CEK ---
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM channels WHERE lid=?", (lid,))
    ch = c.fetchone()
    conn.close()
    if not ch:
        _resolve_stats["misses"] += 1
        return None, "Kanal bulunamadi (DB'de yok)"

    name = ch["name"]
    url = ch["url"]
    hls = ch["hls"]

    # --- Y1: HLS field + Lokke resolve (catalog'tan gelen) ---
    if hls:
        resolved = resolve_hls_link(hls)
        if not resolved:
            # ILK DENEME BASARISIZ -> addonSig'i force-refresh et ve TEKRAR DENE
            slog(f"  Y1 ilk deneme basarisiz, addonSig force-refresh... ({name})")
            resolved = resolve_hls_link(hls, force_sig=True)
        
        if resolved:
            _cache_resolve(lid, resolved, f"Y1-HLS", name)
            return resolved, f"Y1-HLS: {name}"

    # --- Y1.5: URL field + Lokke resolve ---
    if url:
        # live2/play3 gibi direkt URL'leri resolve etme (calismaz!)
        if "/live2/" in url or "/play3/" in url or "/play/" in url:
            # live2 direkt URL - resolve etme, dogrudan Y2'ye gec
            pass
        else:
            resolved = resolve_hls_link(url)
            if not resolved:
                slog(f"  Y1.5 ilk deneme basarisiz, addonSig force-refresh... ({name})")
                resolved = resolve_hls_link(url, force_sig=True)
            
            if resolved:
                _cache_resolve(lid, resolved, f"Y1.5-URL", name)
                return resolved, f"Y1.5-URL-Resolve: {name}"

    # --- Y2: Standart vavoo_auth Token ---
    if url:
        sig = get_auth_signature()
        if sig:
            sep = "&" if "?" in url else "?"
            final = url + sep + "n=1&b=5&vavoo_auth=" + sig
            _cache_resolve(lid, final, f"Y2-Auth", name)
            return final, f"Y2-Auth: {name}"

    # --- Y3: Direkt URL (son care) ---
    if url:
        _resolve_stats["errors"] += 1
        return url, f"Y3-Direct: {name}"

    _resolve_stats["misses"] += 1
    return None, f"URL yok: {name}"


def _cache_resolve(lid, url, method, name):
    """Basarili resolve sonucunu cache'le."""
    with _resolve_cache_lock:
        _resolve_cache[lid] = {
            "url": url,
            "method": method,
            "name": name,
            "time": time.time(),
        }
        # Cache'i temiz tut (max 5000 kayit)
        if len(_resolve_cache) > 5000:
            oldest_lid = min(_resolve_cache, key=lambda k: _resolve_cache[k]["time"])
            del _resolve_cache[oldest_lid]


# ============================================================
# 5. CATALOG FETCH - Tum BASE_URL fallback
# ============================================================
def fetch_catalog(sig, group_name):
    headers = {
        "user-agent": CONFIG["API_USER_AGENT"],
        "accept": "application/json",
        "mediahubmx-signature": sig,
    }
    data = {
        "language": "de", "region": "AT",
        "catalogId": "iptv", "id": "iptv",
        "adult": False, "search": "", "sort": "name",
        "clientVersion": CONFIG["APP_VERSION"],
        "filter": {"group": group_name},
    }

    for base in CONFIG["BASE_URLS"]:
        try:
            url = f"{base}/mediahubmx-catalog.json"
            resp = requests.post(url, json=data, headers=headers, timeout=20, verify=False)
            if resp.status_code != 200:
                try:
                    slog(f"  Catalog {resp.status_code} ({base}): {resp.text[:200]}")
                except Exception:
                    slog(f"  Catalog {resp.status_code} ({base})")
                continue
            catalog_data = resp.json()
            items = catalog_data.get("items", [])
            if items:
                slog(f"  Catalog OK: {base} ({len(items)} kayit)")
                return items
            else:
                err_msg = catalog_data.get("error", "")
                slog(f"  Catalog 400 ({base}): {err_msg}")
        except Exception as e:
            slog(f"  Catalog HATA ({base}): {str(e)[:80]}")

    return []


# ============================================================
# 6. EPG DATA (XMLTV)
# ============================================================
def get_epg_data():
    import sqlite3
    from datetime import datetime, timedelta
    import xml.etree.ElementTree as ET

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT lid, name, grp FROM channels ORDER BY lid")
        channels = c.fetchall()
        conn.close()

        tv = ET.Element("tv")
        tv.set("generator-info-name", "VxParser")
        tv.set("source-info-url", CONFIG["BASE_URLS"][0])

        now = datetime.utcnow()

        for ch in channels:
            lid = ch["lid"]
            name = ch["name"]

            ch_el = ET.SubElement(tv, "channel")
            ch_el.set("id", str(lid))
            display = ET.SubElement(ch_el, "display-name")
            display.text = name

            prog = ET.SubElement(tv, "programme")
            prog.set("start", now.strftime("%Y%m%d%H%M%S") + " +0000")
            prog.set("stop", (now + timedelta(hours=6)).strftime("%Y%m%d%H%M%S") + " +0000")
            prog.set("channel", str(lid))
            title = ET.SubElement(prog, "title")
            title.text = name
            desc = ET.SubElement(prog, "desc")
            desc.text = f"{name} - Live"

        return ET.tostring(tv, encoding="unicode", xml_declaration=True)
    except Exception as e:
        slog(f"EPG HATASI: {e}")
        return None


# ============================================================
# GRUP SIRALAMASI
# ============================================================
GROUP_ORDER = [
    "TR ULUSAL", "TR HABER", "TR BEIN SPORTS", "TR SPOR", "TR BELGESEL",
    "TR SINEMA UHD", "TR SINEMA", "TR MUZIK", "TR COCUK", "TR YEREL",
    "TR DINI", "TR RADYO",
    "DE DEUTSCHLAND", "DE VIP SPORTS", "DE VIP SPORTS 2", "DE SPORT",
    "DE AUSTRIA", "DE SCHWEIZ", "DE FILM", "DE SERIEN", "DE KINO",
    "DE DOKU", "DE KIDS", "DE MUSIK", "DE INFOTAINMENT", "DE NEWS",
    "DE THEMEN", "DE SONSTIGE",
]

GROUP_RULES = {
    "TR ULUSAL": ["TRT 1","Show TV","Star TV","ATV","Kanal D","FOX TV","TV8","Tele1","Beyaz TV","TV 8.5","A2","TRT 4K","Tabii","Gain","TV 100","Flash TV","Kanal 7","TGRT","TLC","D MAX","ERT"],
    "TR HABER": ["Haber","CNN Turk","HABER","NTV","TRT Haber","Bloomberg","TVNET","A Haber","Benguturk","Haber Global","Ulusal Kanal","Sky Turk","TGRT Haber"],
    "TR BEIN SPORTS": ["beIN Sports","beIN SPORT","beIN","beIN 4K","beIN MAX"],
    "TR SPOR": ["Spor","A Spor","TRT Spor","TJK","S Sport","GS TV","FB TV","BJK TV","Fenerbahce","Galatasaray"],
    "TR BELGESEL": ["Belgesel","Nat Geo","Discovery","Animal","History","Yaban TV","BBC Earth"],
    "TR SINEMA UHD": ["4K","UHD"],
    "TR SINEMA": ["Film","Sinema","Cinema","Movie","Movies","DigiMAX","FilmBox","Magic Box","Yesilcam","Dream TV"],
    "TR MUZIK": ["Muzik","Kral TV","Kral Pop","Power TV","Power Turk","Number One","NR1"],
    "TR COCUK": ["Cocuk","Cartoon","Disney","Nick","Minika","Baby TV","Pepee"],
    "TR YEREL": ["Yerel"],
    "TR DINI": ["Dini","Din","Diyanet","Semerkand","Hilal","Lalegul"],
    "TR RADYO": ["Radyo","Radio","FM"],
    "DE DEUTSCHLAND": ["ARD","ZDF","Das Erste","WDR","NDR","BR ","SWR","HR ","MDR","RBB","Phoenix","3sat","KiKA","ONE","Arte","tagesschau24","zdfinfo","zdfneo"],
    "DE VIP SPORTS": ["Sky Sport","Sky Bundesliga","Eurosport","DAZN","Sport1"],
    "DE VIP SPORTS 2": ["Sky Sport Austria","Telekom Sport","Magenta Sport"],
    "DE SPORT": ["Sport ","Eurosport","Sportdigital","Motorvision"],
    "DE AUSTRIA": ["ORF","Puls 4","Servus"],
    "DE SCHWEIZ": ["SRF","Swiss"],
    "DE FILM": ["Sky Cinema","RTL+","13th Street","AXN","TNT Serie","TNT Film","Sky Hits","Sky Action"],
    "DE SERIEN": ["Serie","RTL","Sat.1","ProSieben","VOX","kabel eins","RTL2","Super RTL","Sixx","TELE 5"],
    "DE KINO": ["Kino"],
    "DE DOKU": ["Doku","Docu","D-MAX","N24 Doku","Spiegel TV"],
    "DE KIDS": ["Kind","Kids","Toggo"],
    "DE MUSIK": ["Musik","VIVA","Deluxe Music"],
    "DE INFOTAINMENT": ["Info","N24","WELT","n-tv","BBC World","France 24"],
    "DE NEWS": ["News","Tagesschau"],
    "DE THEMEN": ["Shop","QVC","HSE","Bibel TV","Sonstig","Regional"],
}
