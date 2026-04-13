"""
state.py - Ortak state modulu.
Token fonksiyonlari ve resolve fonksiyonlari burada.
server.py ve video.py sadece state import eder, BIRBIRINI IMPORT ETMEZ.

v4.0.0 - DIRECT HLS: Catalog URL direkt stream olarak denenir
         Resolve chain: Direct HLS -> MediaHubMX -> Auth -> live2 resolve
         FORCE sig refresh loop engellendi (cooldown)
         Resolve cache negatif sonuc icin kisa TTL
         Y4-Direct kaldirildi (dead URL)
         Better error handling
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
# CONFIG
# ============================================================
CONFIG = {
    "PING_URLS": [
        "https://www.vavoo.tv/api/app/ping",
        "https://www.lokke.app/api/app/ping",
    ],
    "BASE_URLS": [
        "https://vavoo.to",
        "https://kool.to",
        "https://oha.to",
    ],
    "PING2_URLS": [
        "https://www.vavoo.to/api/box/ping2",
        "https://www.vavoo.tv/api/box/ping2",
        "https://kool.to/api/box/ping2",
        "https://oha.to/api/box/ping2",
    ],
    "LIVE2_URLS": [
        "https://www.vavoo.to/live2/index?output=json",
        "https://kool.to/live2/index?output=json",
        "https://oha.to/live2/index?output=json",
    ],
    "SIG_CACHE_TTL": 8 * 60,
    "SIG_FAIL_TTL": 3 * 60,
    "RESOLVE_CACHE_TTL": 30 * 60,       # 30 dk (eski 45)
    "RESOLVE_FAIL_TTL": 2 * 60,          # 2 dk (basarisiz resolve kisa TTL)
    "RESOLVE_TIMEOUT": 12,
    "FORCE_SIG_COOLDOWN": 5 * 60,        # 5 dk FORCE sig cooldown
    "DIRECT_HLS_TIMEOUT": 8,             # Direct HLS HEAD check timeout
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

_vavoo_sig = None
_vavoo_sig_time = 0
_vavoo_sig_failed = False
_watched_sig = None
_watched_sig_time = 0
_watched_sig_failed = False

# Resolve cache (TTL)
_resolve_cache = {}
_resolve_cache_lock = threading.Lock()
_resolve_stats = {"hits": 0, "misses": 0, "expired": 0, "errors": 0}

# FORCE sig cooldown - loop engelleme
_last_force_sig_time = 0
_force_sig_lock = threading.Lock()


def get_resolve_cache_info():
    now = time.time()
    active = expired = failed = 0
    with _resolve_cache_lock:
        for entry in _resolve_cache.values():
            age = now - entry["time"]
            ttl = entry.get("ttl", CONFIG["RESOLVE_CACHE_TTL"])
            if age < ttl:
                active += 1
            else:
                expired += 1
            if entry.get("failed"):
                failed += 1
    return {"total": len(_resolve_cache), "active": active, "expired": expired,
            "failed": failed,
            "hits": _resolve_stats["hits"], "misses": _resolve_stats["misses"],
            "errors": _resolve_stats["errors"]}


def clear_resolve_cache():
    with _resolve_cache_lock:
        _resolve_cache.clear()
        _resolve_stats["hits"] = _resolve_stats["misses"] = _resolve_stats["expired"] = _resolve_stats["errors"] = 0


def slog(msg):
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    STARTUP_LOGS.append(entry)
    print(entry)


# ============================================================
# 1. VAVOO TOKEN (ping2)
# ============================================================
def get_auth_signature():
    global _vavoo_sig, _vavoo_sig_time, _vavoo_sig_failed
    if _vavoo_sig and (time.time() - _vavoo_sig_time) < CONFIG["SIG_CACHE_TTL"]:
        return _vavoo_sig
    if _vavoo_sig_failed and (time.time() - _vavoo_sig_time) < CONFIG["SIG_FAIL_TTL"]:
        return None

    slog("Vavoo Token (ping2) aliniyor...")
    headers = {"User-Agent": CONFIG["CDN_USER_AGENT"], "Accept": "application/json"}
    try:
        vec_req = requests.get("http://mastaaa1987.github.io/repo/veclist.json", headers=headers, timeout=10, verify=False)
        veclist = vec_req.json()["value"]
        slog(f"  veclist: {len(veclist)} vec")
        sig = None
        for ping_url in CONFIG["PING2_URLS"]:
            if sig:
                break
            for _ in range(3):
                vec = {"vec": random.choice(veclist)}
                try:
                    req = requests.post(ping_url, data=vec, headers=headers, timeout=10, verify=False).json()
                    if req.get("signed"):
                        sig = req["signed"]
                        slog(f"  Token alindi: {ping_url}")
                        break
                except Exception:
                    continue
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
# 2. ADDONSIG (app/ping) - FORCE cooldown ile
# ============================================================
def get_watchedsig(force=False):
    global _watched_sig, _watched_sig_time, _watched_sig_failed

    # FORCE cooldown kontrolu - sonsuz donguyu engelle
    if force:
        with _force_sig_lock:
            now = time.time()
            if (now - _last_force_sig_time) < CONFIG["FORCE_SIG_COOLDOWN"]:
                # Cooldown suresinde eski sig'i kullan (varsa)
                if _watched_sig:
                    return _watched_sig
                return None
            _last_force_sig_time = now

    if not force and _watched_sig and (time.time() - _watched_sig_time) < CONFIG["SIG_CACHE_TTL"]:
        return _watched_sig
    if not force and _watched_sig_failed and (time.time() - _watched_sig_time) < CONFIG["SIG_FAIL_TTL"]:
        return None

    tag = " (FORCE)" if force else ""
    slog(f"addonSig aliniyor{tag}...")
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
# 3. HLS RESOLVE (mediahubmx-resolve.json)
# ============================================================
def resolve_hls_link(link, force_sig=False):
    sig = get_watchedsig(force=force_sig)
    if not sig:
        return None, "addonSig yok"

    headers = {
        "user-agent": "MediaHubMX/2",
        "accept": "application/json",
        "content-type": "application/json; charset=utf-8",
        "accept-encoding": "gzip",
        "mediahubmx-signature": sig,
    }
    data = {"language": "de", "region": "AT", "url": link, "clientVersion": CONFIG["APP_VERSION"]}

    last_error = ""
    for base in CONFIG["BASE_URLS"]:
        try:
            url = f"{base}/mediahubmx-resolve.json"
            r = requests.post(url, data=json.dumps(data), headers=headers, timeout=CONFIG["RESOLVE_TIMEOUT"], verify=False)
            if r.status_code != 200:
                last_error = f"{r.status_code}"
                continue
            # HTML kontrolu - JSON degilse hata
            ct = r.headers.get("content-type", "")
            if "html" in ct.lower():
                last_error = "HTML response (not JSON)"
                continue
            result = r.json()
            if result and isinstance(result, list) and len(result) > 0:
                resolved = result[0].get("url")
                if resolved:
                    return resolved, f"resolve OK ({base})"
            elif isinstance(result, dict):
                last_error = result.get("error", "empty dict response")
            else:
                last_error = f"unexpected type: {type(result).__name__}"
        except requests.exceptions.Timeout:
            last_error = "timeout"
            continue
        except Exception as e:
            last_error = str(e)[:80]
            continue
    return None, f"resolve fail: {last_error}"


# ============================================================
# 3B. DIRECT HLS CHECK - URL zaten stream mi?
# .m3u8 URL'leri direkt kullanilabilir, resolve gerekmez
# ============================================================
def check_direct_hls(link):
    """
    URL'nin zaten direkt bir HLS stream olup olmadigini kontrol et.
    HEAD request ile 200 doneyse direkt kullanilabilir.
    """
    if not link:
        return False
    # .m3u8 uzantisi varsa yuksek ihtimalle direkt stream
    is_m3u8 = ".m3u8" in link.lower()
    # .ts uzantisi da direkt stream olabilir
    is_ts = ".ts" in link.lower() and "/live" in link.lower()

    if not is_m3u8 and not is_ts:
        return False

    try:
        h = {
            "user-agent": CONFIG["CDN_USER_AGENT"],
            "accept": "*/*",
        }
        r = requests.head(link, headers=h, timeout=CONFIG["DIRECT_HLS_TIMEOUT"],
                          verify=False, allow_redirects=True)
        if r.status_code in (200, 301, 302, 303, 307, 308):
            # Content-Type kontrolu
            ct = r.headers.get("content-type", "").lower()
            # HLS playlist veya MPEG2-TS
            if any(x in ct for x in ["mpegurl", "mp2t", "octet-stream", "video", "mpeg"]) or r.status_code == 200:
                return True
        return False
    except Exception:
        return False


# ============================================================
# 4. CHANNEL RESOLVE (Cache + TTL)
# Resolve chain:
#   Y0: Direct HLS (catalog URL zaten stream ise)
#   Y1: MediaHubMX resolve (catalog URL -> stream URL)
#   Y2: vavoo_auth (live2 URL + token)
#   Y3: MediaHubMX resolve with live2 URL
# ============================================================
def resolve_channel(lid):
    now = time.time()
    with _resolve_cache_lock:
        if lid in _resolve_cache:
            entry = _resolve_cache[lid]
            ttl = entry.get("ttl", CONFIG["RESOLVE_CACHE_TTL"])
            if (now - entry["time"]) < ttl:
                _resolve_stats["hits"] += 1
                if entry.get("failed"):
                    return None, f"CACHE-FAIL ({entry['method']}): {entry['name']}"
                return entry["url"], f"CACHE ({entry['method']}): {entry['name']}"
            else:
                _resolve_stats["expired"] += 1
                del _resolve_cache[lid]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM channels WHERE lid=?", (lid,))
    ch = c.fetchone()
    conn.close()
    if not ch:
        return None, "Kanal bulunamadi"

    name = ch["name"]
    url = ch["url"]
    hls = ch["hls"]

    # Y0: Direct HLS - Catalog URL zaten stream olabilir
    if hls:
        if check_direct_hls(hls):
            _cache_resolve(lid, hls, "Y0-Direct", name)
            return hls, f"Y0-Direct: {name}"

    # Y1: HLS field (catalog) + MediaHubMX resolve
    if hls:
        resolved, info = resolve_hls_link(hls)
        if resolved:
            _cache_resolve(lid, resolved, "Y1-HLS", name)
            return resolved, f"Y1-HLS: {name}"

        # FORCE sig ile tekrar dene (cooldown var, sonsuz dongu yok)
        resolved2, info2 = resolve_hls_link(hls, force_sig=True)
        if resolved2:
            _cache_resolve(lid, resolved2, "Y1-HLS-F", name)
            return resolved2, f"Y1-HLS(F): {name}"

    # Y2: vavoo_auth (live2 URL + token)
    if url:
        sig = get_auth_signature()
        if sig:
            sep = "&" if "?" in url else "?"
            final = url + sep + "n=1&b=5&vavoo_auth=" + sig
            _cache_resolve(lid, final, "Y2-Auth", name)
            return final, f"Y2-Auth: {name}"

    # Y3: MediaHubMX resolve with live2 URL
    if url:
        resolved, info = resolve_hls_link(url)
        if resolved:
            _cache_resolve(lid, resolved, "Y3-Resolve", name)
            return resolved, f"Y3-Resolve: {name}"

        resolved2, info2 = resolve_hls_link(url, force_sig=True)
        if resolved2:
            _cache_resolve(lid, resolved2, "Y3-Resolve-F", name)
            return resolved2, f"Y3-Resolve(F): {name}"

    # Basarisiz - kisa TTL ile cache'le (tekrar denemek icin)
    _resolve_stats["errors"] += 1
    _cache_resolve(lid, None, "FAIL", name, failed=True, ttl=CONFIG["RESOLVE_FAIL_TTL"])
    return None, "Tum yontemler basarisiz"


def _cache_resolve(lid, url, method, name, failed=False, ttl=None):
    if ttl is None:
        ttl = CONFIG["RESOLVE_FAIL_TTL"] if failed else CONFIG["RESOLVE_CACHE_TTL"]
    with _resolve_cache_lock:
        _resolve_cache[lid] = {
            "url": url, "method": method, "name": name,
            "time": time.time(), "failed": failed, "ttl": ttl
        }
        if len(_resolve_cache) > 5000:
            # En eski ve basarisiz olanlari temizle
            to_remove = sorted(
                [k for k, v in _resolve_cache.items() if v.get("failed")],
                key=lambda k: _resolve_cache[k]["time"]
            )
            for k in to_remove[:1000]:
                del _resolve_cache[k]
            if len(_resolve_cache) > 5000:
                oldest = min(_resolve_cache, key=lambda k: _resolve_cache[k]["time"])
                del _resolve_cache[oldest]


# ============================================================
# 5. CATALOG FETCH (mediahubmx-catalog.json)
# ============================================================
def fetch_catalog(sig, group_name):
    headers = {
        "accept-encoding": "gzip",
        "user-agent": "MediaHubMX/2",
        "accept": "application/json",
        "content-type": "application/json; charset=utf-8",
        "mediahubmx-signature": sig,
    }
    data = {
        "language": "de", "region": "AT",
        "catalogId": "iptv", "id": "iptv",
        "adult": False, "search": "", "sort": "name",
        "filter": {"group": group_name},
        "cursor": 0,
        "clientVersion": CONFIG["APP_VERSION"],
    }

    all_items = []

    for base in CONFIG["BASE_URLS"]:
        if all_items:
            break
        try:
            url = f"{base}/mediahubmx-catalog.json"
            resp = requests.post(url, data=json.dumps(data), headers=headers, timeout=20, verify=False)
            if resp.status_code != 200:
                slog(f"  Catalog {resp.status_code} ({base}): {resp.text[:150]}")
                continue
            catalog_data = resp.json()
            items = catalog_data.get("items", [])
            if items:
                all_items.extend(items)
                slog(f"  Catalog OK: {base} ({len(items)} kayit)")
                next_cursor = catalog_data.get("nextCursor")
                page = 1
                while next_cursor:
                    page += 1
                    data["cursor"] = next_cursor
                    try:
                        resp2 = requests.post(url, data=json.dumps(data), headers=headers, timeout=20, verify=False)
                        cd2 = resp2.json()
                        items2 = cd2.get("items", [])
                        if items2:
                            all_items.extend(items2)
                            slog(f"  Catalog sayfa {page}: +{len(items2)} kayit")
                        next_cursor = cd2.get("nextCursor")
                    except Exception as e:
                        slog(f"  Catalog sayfa {page} HATA: {str(e)[:60]}")
                        break
                break
            else:
                err_msg = catalog_data.get("error", "")
                slog(f"  Catalog bos ({base}): {err_msg}")
        except Exception as e:
            slog(f"  Catalog HATA ({base}): {str(e)[:80]}")

    return all_items


# ============================================================
# 6. EPG DATA (XMLTV)
# ============================================================
def get_epg_data():
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
        now = datetime.utcnow()
        for ch in channels:
            ch_el = ET.SubElement(tv, "channel")
            ch_el.set("id", str(ch["lid"]))
            ET.SubElement(ch_el, "display-name").text = ch["name"]
            prog = ET.SubElement(tv, "programme")
            prog.set("start", now.strftime("%Y%m%d%H%M%S") + " +0000")
            prog.set("stop", (now + timedelta(hours=6)).strftime("%Y%m%d%H%M%S") + " +0000")
            prog.set("channel", str(ch["lid"]))
            ET.SubElement(prog, "title").text = ch["name"]
            ET.SubElement(prog, "desc").text = f"{ch['name']} - Live"
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
