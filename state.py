"""
state.py - Ortak state modulu.
Token ve resolve fonksiyonlari burada.
video.py server.py'yi import ETMEZ, sadece state import eder.
"""
import os
import random
import re
import time
import json
import sqlite3
import asyncio
import httpx
import urllib3
import threading

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# PAYLASILAN STATE
# ============================================================
DATA_READY = False
STARTUP_ERROR = None
STARTUP_DONE = False          # True once startup_sequence finishes (success or fail)
STARTUP_TIME = None            # time.time() when server started (for uptime calc)
LOAD_TIME = 0
STARTUP_LOGS = []
STARTUP_LOCK = threading.Lock()  # Prevents concurrent startup/refresh

DB_PATH = "/tmp/vxparser.db"
M3U_PATH = "/tmp/playlist.m3u"
PORT = 10000

REFRESH_INTERVAL = 6 * 3600    # 6 hours auto-refresh
LAST_REFRESH = 0               # Last successful refresh timestamp

# Resolve cache (5 min TTL)
RESOLVE_CACHE = {}  # {str(ch_id): {"url": resolved_url, "expires": timestamp}}

# Token cache
_vavoo_sig = None
_vavoo_sig_time = 0
_watched_sig = None
_watched_sig_time = 0

# ONCEMLI: Token deneme lock - sonsuz donguyu onlemek icin
_token_lock = threading.Lock()
_vavoo_try_count = 0
_vavoo_cooldown_until = 0  # Basarisiz olursa 5 dk bekle

# Vavoo API headers (used for all Vavoo API calls)
VAVOO_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://vavoo.to/",
    "Origin": "https://vavoo.to",
    "Accept": "*/*",
}


def slog(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    STARTUP_LOGS.append(entry)
    # Keep logs bounded (last 500 entries)
    if len(STARTUP_LOGS) > 500:
        del STARTUP_LOGS[:100]
    print(entry)


def get_uptime():
    """Return uptime in seconds, or 0 if not started."""
    if STARTUP_TIME is None:
        return 0
    return int(time.time() - STARTUP_TIME)


def count_db_channels():
    """Count channels in DB. Returns 0 on error."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM channels")
        count = c.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


# ============================================================
# 1. VAVOO TOKEN (ping2) - RATE LIMITED (sync, startup only)
# ============================================================
def get_auth_signature(force=False):
    """
    Vavoo ping2 token al.
    force=True ile zorla tekrar dene.
    Basarisiz olursa 5 dakika cooldown.
    """
    global _vavoo_sig, _vavoo_sig_time, _vavoo_try_count, _vavoo_cooldown_until

    # Cache varsa kullan
    if _vavoo_sig and (time.time() - _vavoo_sig_time) < 1800:
        return _vavoo_sig

    # Cooldown aktifse bekle
    if not force and time.time() < _vavoo_cooldown_until:
        return None

    # Lock al - ayni anda birden fazla thread denemesin
    if not _token_lock.acquire(blocking=False):
        return None  # Baska biri zaten deniyor, bekleme

    try:
        # Cooldown kontrolu (lock icinde tekrar kontrol)
        if not force and time.time() < _vavoo_cooldown_until:
            return None

        slog("Vavoo Token (ping2) aliniyor...")
        headers = {"User-Agent": "VAVOO/2.6", "Accept": "application/json"}
        vec_req = httpx.get("http://mastaaa1987.github.io/repo/veclist.json", headers=headers, timeout=10, verify=False)
        veclist = vec_req.json()["value"]
        slog(f"veclist: {len(veclist)} vec")

        sig = None
        for i in range(5):
            vec = {"vec": random.choice(veclist)}
            req = httpx.post("https://www.vavoo.tv/api/box/ping2", data=vec, headers=headers, timeout=10, verify=False).json()
            if req.get("signed"):
                sig = req["signed"]
                break

        if sig:
            _vavoo_sig = sig
            _vavoo_sig_time = time.time()
            _vavoo_try_count = 0
            slog("Vavoo Token alindi!")
            return sig
        else:
            _vavoo_try_count += 1
            # 2 deneme sonra 10 dakika cooldown
            if _vavoo_try_count >= 2:
                _vavoo_cooldown_until = time.time() + 600
                slog(f"Vavoo Token {2} deneme basarisiz. 10 dakika cooldown.")
            else:
                slog("Vavoo Token ALINAMADI (5 deneme)")
    except Exception as e:
        slog(f"Vavoo Token HATASI: {e}")
    finally:
        _token_lock.release()

    return None


# ============================================================
# 2. LOKKE IMZA (app/ping) - sync, startup only
# ============================================================
def get_watchedsig():
    global _watched_sig, _watched_sig_time
    if _watched_sig and (time.time() - _watched_sig_time) < 1800:
        return _watched_sig

    slog("Lokke Imza (app/ping) aliniyor...")
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
    try:
        resp = httpx.post("https://www.lokke.app/api/app/ping", json=data, headers=headers, timeout=15, verify=False)
        result = resp.json()
        sig = result.get("addonSig")
        if sig:
            _watched_sig = sig
            _watched_sig_time = time.time()
            slog("Lokke Imzasi alindi!")
            return sig
        else:
            slog(f"Lokke cevap: {list(result.keys())}")
    except Exception as e:
        slog(f"Lokke HATASI: {e}")
    return None


def get_watched_sig_str():
    """Return the cached Lokke signature as a string (for use in async resolve)."""
    return _watched_sig or ""


# ============================================================
# 3. ASYNC RESOLVE (mediahubmx) - called per-request
# ============================================================
async def resolve_mediahubmx(url):
    """Resolve a stream URL via MediaHubMX. Returns resolved URL or None."""
    global _watched_sig

    # Check cache first
    # (cache is checked in resolve_channel, but this can also be called directly)

    if not _watched_sig:
        # Try to get signature sync (should already be available from startup)
        _watched_sig = get_watchedsig()
    if not _watched_sig:
        slog("Resolve: Lokke signature yok!")
        return None

    try:
        headers = {
            "user-agent": "MediaHubMX/2",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "mediahubmx-signature": _watched_sig,
        }
        data = {
            "language": "de",
            "region": "AT",
            "url": url,
            "clientVersion": "3.0.2"
        }
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.post("https://vavoo.to/mediahubmx-resolve.json", json=data, headers=headers)
            if r.status_code == 200:
                result = r.json()
                if isinstance(result, list) and len(result) > 0:
                    resolved_url = result[0].get("url", "")
                    if resolved_url:
                        return resolved_url
                slog(f"Resolve response: {json.dumps(result)[:200]}")
            else:
                slog(f"Resolve HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        slog(f"Resolve hata: {e}")
    return None


# ============================================================
# 4. CHANNEL RESOLVE (async, per-request, with cache)
# ============================================================
async def resolve_channel(lid):
    """
    Kanal ID'sine gore stream URL'ini coz.
    Y1: Check resolve cache -> if cached and not expired, use it
    Y2: HLS + Lokke (mediahubmx resolve)
    Y3: URL + vavoo_auth token (rate limited - cooldown var)
    Y4: Direct URL (fallback)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM channels WHERE lid=?", (lid,))
        ch = c.fetchone()
        conn.close()
    except Exception as e:
        return None, f"DB hatasi: {e}"

    if not ch:
        return None, f"Kanal {lid} DB'de yok"

    name = ch["name"]
    url = ch["url"]
    hls = ch["hls"]

    # Y0: Check resolve cache
    cache_key = str(lid)
    if cache_key in RESOLVE_CACHE:
        cached = RESOLVE_CACHE[cache_key]
        if cached.get("expires", 0) > time.time():
            return cached["url"], f"Cache: {name}"

    # Y1: HLS + Lokke (mediahubmx)
    if hls:
        resolved = await resolve_mediahubmx(hls)
        if resolved:
            RESOLVE_CACHE[cache_key] = {"url": resolved, "expires": time.time() + 300}
            return resolved, f"Y1-HLS: {name}"

    # Y2: Standart Token (rate limited)
    if url:
        sig = get_auth_signature()
        if sig:
            sep = "&" if "?" in url else "?"
            final = url + sep + "n=1&b=5&vavoo_auth=" + sig
            RESOLVE_CACHE[cache_key] = {"url": final, "expires": time.time() + 300}
            return final, f"Y2-Auth: {name}"

    # Y3: Direct URL
    if url:
        RESOLVE_CACHE[cache_key] = {"url": url, "expires": time.time() + 300}
        return url, f"Y3-Direct: {name}"

    return None, f"URL yok: {name}"


# ============================================================
# 5. ASYNC CATALOG FETCH (for HLS links)
# ============================================================
async def fetch_catalog(group, cursor=0):
    """Fetch MediaHubMX catalog for a group"""
    if not _watched_sig:
        return {}
    try:
        headers = {
            "user-agent": "MediaHubMX/2",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "mediahubmx-signature": _watched_sig,
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
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            r = await client.post("https://vavoo.to/mediahubmx-catalog.json", json=data, headers=headers)
            if r.status_code == 200:
                return r.json()
            else:
                slog(f"Catalog HTTP {r.status_code} for group '{group}'")
    except Exception as e:
        slog(f"Catalog hata ({group}): {e}")
    return {}


async def fetch_all_catalog(group_name):
    """Fetch all pages of a catalog with pagination."""
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


# ============================================================
# 6. ASYNC CHANNEL FETCH (correct API URL, proper headers)
# ============================================================
async def fetch_channels():
    """Fetch channels from live2/index API with correct URL and headers."""
    async with httpx.AsyncClient(timeout=30, verify=False, follow_redirects=True) as client:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://vavoo.to/",
            "Origin": "https://vavoo.to",
            "Accept": "application/json, */*",
        }
        r = await client.get("https://vavoo.to/live2/index?output.json", headers=headers)
        data = r.json()
        channels = []
        if isinstance(data, list):
            channels = data
        elif isinstance(data, dict):
            for key in ["channels", "data", "items", "list", "results"]:
                if key in data and isinstance(data[key], list):
                    channels = data[key]
                    break
        slog(f"API: {len(channels)} kanal")
        return channels


# ============================================================
# 7. NAME CLEANING (preserves Turkish chars)
# ============================================================
def clean_name(name):
    """Clean channel name for matching. Preserves Turkish chars, removes resolution tags."""
    n = name.upper()
    # Remove resolution tags
    for remove in [" (1)", " (2)", " (3)", " (4)", " (5)", "(BACKUP)", "+",
                    " HEVC", " RAW", " SD", " FHD", " UHD", " 4K", " H265",
                    " HD", " FHD", " UHD", " 1080", " 720", " AUSTRIA", " AT"]:
        n = n.replace(remove, "")
    # Remove parenthesized content
    n = re.sub(r'\([^)]*\)', '', n)
    n = re.sub(r'\[[^\]]*\]', '', n)
    n = n.strip()
    return n


# ============================================================
# 8. COUNTRY DETECTION
# ============================================================
def detect_country(ch):
    """Detect country from channel data."""
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
    """Map channel to a group based on name and original group."""
    n = name.upper()
    g = original_group.upper()
    combined = n + " " + g
    if any(k in combined for k in ["ULUSAL","SHOW TV","STAR TV","KANAL D","ATV","FOX TV","TV8","TRT 1","A2 TV","A2 HD","TEVE2","TV100","BLOOMBERG HT","TV A","TLC","BEYAZ TV","FLASH TV","KANAL 7","HALK TV","TELE1","ULKE TV"]):
        return "TR ULUSAL"
    if any(k in combined for k in ["HABER","NEWS","CNBC","NTV","BLOOMBERG","AHABER","CNN TURK","HABER TURK","TGRT HABER","TELE1"]):
        return "TR HABER"
    if any(k in combined for k in ["BELGESEL","DOC","DISCOVERY","NAT GEO","NATIONAL GEO","HISTORY","ANIMAL","DA VINCI","YABAN","AV ","TRT BELGESEL","VIASAT EXPLORE","VIASAT HISTORY"]):
        return "TR BELGESEL"
    if any(k in combined for k in ["COCUK","CARTOON","MINIKA","TRT COCUK","BABY","DISNEY","NICKELODEON","KIDS","BOOMERANG","KINDER"]):
        return "TR COCUK"
    if any(k in combined for k in ["FILM","MOVIE","SINEMA","CINE","YESILCAM","DIZI","SERIES","FILMBOX","D-SMART","MOVIES","CINEMA"]):
        return "TR SINEMA"
    if any(k in combined for k in ["MUZIK","MUSIC","KRAL","POWER","NUMBER ONE","DREAM","NR1","TMB"]):
        return "TR MUZIK"
    if any(k in combined for k in ["SPOR","SPORT","BEIN","TIVIBU","DSMART","TRT SPOR","A SPOR","EUROSPORT","SPORTS"]):
        return "TR SPOR"
    if any(k in combined for k in ["DINI","DIN","LALE","SEMERKAND","HILAL","MEKKE","KURAN"]):
        return "TR DINI"
    if any(k in combined for k in ["RADYO","RADIO"]):
        return "TR RADYO"
    if any(k in combined for k in ["ARD","ZDF","ARTE","WDR","NDR","MDR","SWR","BR ","HR ","RB ","SR ","RBB","PHOENIX","TAGESSCHAU","3SAT","KIKA","ONE","ZDFNEO","ZDFINFO","PROSIEBEN","SAT.1","RTL","VOX","KABEL1","SUPER RTL"]):
        return "DE DEUTSCHLAND"
    if any(k in combined for k in ["NACHRICHTEN","NTV DE","WELT","CNN DE","SPIEGEL","TAGESSPIEGEL"]):
        return "DE NEWS"
    if any(k in combined for k in ["DOKU","DOKUMENTATION","DOCUMENTARY"]):
        return "DE DOKU"
    if any(k in combined for k in ["KINDER","CHILDREN"]):
        return "DE KIDS"
    if any(k in combined for k in ["DE: FILM","DE: MOVIE","DE: CINE","DE: SKY","DE: AXN","DE: TNT"]):
        return "DE FILM"
    if any(k in combined for k in ["DE: MUSIK","DE: MTV"]):
        return "DE MUSIK"
    if any(k in combined for k in ["DE: SPORT","DE: EUROSPORT","DE: SKY SPORT","DE: BEIN","DE: DAZN"]):
        return "DE SPORT"
    return "TR YEREL"


# ============================================================
# GRUP SIRALAMASI (kept from Render version)
# ============================================================
GROUP_ORDER = [
    # Turkish - sports processed LAST to avoid catching German channels
    "TR ULUSAL", "TR HABER", "TR BEIN SPORTS", "TR BELGESEL",
    "TR SINEMA UHD", "TR SINEMA", "TR MUZIK", "TR COCUK", "TR YEREL",
    "TR DINI", "TR RADYO",
    # German - sport groups MUST come before TR SPOR
    "DE DEUTSCHLAND", "DE VIP SPORTS", "DE VIP SPORTS 2", "DE SPORT",
    "DE AUSTRIA", "DE SCHWEIZ", "DE FILM", "DE SERIEN", "DE KINO",
    "DE DOKU", "DE KIDS", "DE MUSIK", "DE INFOTAINMENT", "DE NEWS",
    "DE THEMEN", "DE SONSTIGE",
    # TR SPOR last
    "TR SPOR",
]

GROUP_RULES = {
    # Turkish groups - VERY specific keywords
    "TR ULUSAL": [
        "TRT 1","Show TV","Star TV","Kanal D","ATV","FOX TV","TV8",
        "Tele1","Beyaz TV","TV 8.5","A2","TRT 4K","Tabii","Gain",
        "TV 100","Flash TV","Kanal 7","TGRT","TLC","D MAX","ERT",
        "A2 TV","Teve2","TV100","Bloomberg HT",
    ],
    "TR HABER": [
        "Haber Turk","CNN Turk","TRT Haber","A Haber","Benguturk",
        "Haber Global","TGRT Haber","Bloomberg HT","TVNET","Ulusal Kanal",
        "NTV","HABER","Bloomberg","Sky Turk","360 TV",
    ],
    "TR BEIN SPORTS": [
        "beIN Sports","beIN SPORT","beIN MAX","beIN 4K",
        "beIN 1","beIN 2","beIN 3","beIN 4",
    ],
    "TR BELGESEL": [
        "TRT Belgesel","Nat Geo","Discovery","Animal Planet","History",
        "Yaban TV","BBC Earth","Da Vinci","Viasat Explore","Viasat History",
    ],
    "TR SINEMA UHD": [
        "4K TR","UHD TR","4K Film","UHD Film",
        "TR 4K","FHD TR","HD TR",
    ],
    "TR SINEMA": [
        "FilmBox","DigiMAX","Magic Box","Yesilcam","Dream TV",
        "Sinema TV","Sinematv","Movie Smart","Film TV",
        "D Smart","D-Smart",
    ],
    "TR MUZIK": [
        "Kral TV","Kral Pop","Power TV","Power Turk","Power Turk TV",
        "Number One TV","NR1 TV","Dream Turk","MTV TR",
    ],
    "TR COCUK": [
        "Cartoon","Minika","Baby TV","Pepee","TRT Cocuk",
        "Disney TR","Nick TR","Nickelodeon TR",
    ],
    "TR YEREL": [
        "Yerel","TV 36","Kanal 3","Kanal 26",
    ],
    "TR DINI": [
        "Diyanet","Semerkand","Hilal","Lalegul","Dini TV",
        "Mekke TV","Medine TV",
    ],
    "TR RADYO": [
        "Radyo","Radio","FM","Power FM","Kral FM","Super FM","Radyo Eksen",
    ],
    "DE DEUTSCHLAND": [
        "ARD","ZDF","Das Erste","WDR","NDR","BR ","SWR","HR ","MDR",
        "RBB","Phoenix","3sat","KiKA","ONE","Arte","tagesschau24",
        "zdfinfo","zdfneo","tagesschau",
    ],
    "DE VIP SPORTS": [
        "Sky Sport","Sky Bundesliga","Eurosport","DAZN","Sport1",
        "beIN DE","beIN Sport DE",
        "DAZN 1","DAZN 2",
    ],
    "DE VIP SPORTS 2": [
        "Sky Sport Austria","Telekom Sport","Magenta Sport",
    ],
    "DE SPORT": [
        "Sportdigital","Motorvision","Sport +","SPORT1",
        "Eurosport 2","Eurosport 3",
        "Motorvision TV",
    ],
    "DE AUSTRIA": [
        "ORF","Puls 4","Servus","AT ","Austria",
    ],
    "DE SCHWEIZ": [
        "SRF","Swiss","CH ","Schweiz",
    ],
    "DE FILM": [
        "Sky Cinema","13th Street","AXN","TNT Serie","TNT Film",
        "Sky Hits","Sky Action","Sky Crime","Sky Comedy","Sky Family",
        "RTL+","RTL Plus",
    ],
    "DE SERIEN": [
        "RTL","Sat.1","ProSieben","VOX","kabel eins","RTL2",
        "Super RTL","Sixx","TELE 5","ProSieben Maxx","Sat.1 Gold",
        "kabel eins Doku",
    ],
    "DE KINO": [
        "Kino",
    ],
    "DE DOKU": [
        "Doku","Docu","D-MAX","N24 Doku","Spiegel TV",
    ],
    "DE KIDS": [
        "Toggo","KiKA",
    ],
    "DE MUSIK": [
        "VIVA","Deluxe Music","MTV",
    ],
    "DE INFOTAINMENT": [
        "N24","WELT","n-tv","BBC World","France 24","ZDF Info",
    ],
    "DE NEWS": [
        "Tagesschau","Tagesspiegel",
    ],
    "DE THEMEN": [
        "Shop","QVC","HSE","Bibel TV","Sonstig","Regional",
    ],
    "TR SPOR": [
        "A Spor","TRT Spor","TJK TV","TJK","S Sport",
        "GS TV","FB TV","BJK TV",
        "Fenerbahce TV","Galatasaray TV","Trabzonspor TV",
        "S Sport 1","S Sport 2",
    ],
}
