"""
state.py - Ortak state modulu.
Token fonksiyonlari ve resolve fonksiyonlari burada.
server.py ve video.py sadece state import eder, BIRBIRINI IMPORT ETMEZ.
"""
import os
import random
import time
import json
import sqlite3
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIG - Tum URL'ler ve ayarlar
# ============================================================
CONFIG = {
    # addonSig (MediaHubMX imzasi) almak icin ping endpointleri
    "PING_URLS": [
        "https://www.lokke.app/api/app/ping",
        "https://www.vavoo.tv/api/app/ping",
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
    "SIG_CACHE_TTL": 8 * 60,        # 8 dakika
    "RESOLVE_TIMEOUT": 15,          # 15 saniye
    "CDN_USER_AGENT": "VAVOO/2.6",
    "API_USER_AGENT": "MediaHubMX/2",
    "APP_VERSION": "3.1.8",
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
_watched_sig = None
_watched_sig_time = 0


def slog(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    STARTUP_LOGS.append(entry)
    print(entry)


# ============================================================
# YARDIMCI: URL dene (fallback)
# ============================================================
def _try_urls(url_list, method="GET", headers=None, json_data=None, form_data=None, timeout=15, key_to_check=None):
    """
    Birden fazla URL'yi sirayla dener. Ilk basarili sonucu dondurur.
    Returns: (response_json_or_text, success, tried_url)
    """
    for url in url_list:
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, timeout=timeout, verify=False)
            elif method == "POST_JSON":
                r = requests.post(url, json=json_data, headers=headers, timeout=timeout, verify=False)
            elif method == "POST_FORM":
                r = requests.post(url, data=form_data, headers=headers, timeout=timeout, verify=False)
            else:
                continue

            if r.status_code == 200:
                try:
                    data = r.json()
                    if key_to_check:
                        if data.get(key_to_check):
                            return data, True, url
                        else:
                            slog(f"  {url} -> 200 ama '{key_to_check}' yok, keys={list(data.keys())[:5]}")
                    else:
                        return data, True, url
                except:
                    return r.text, True, url
            else:
                slog(f"  {url} -> status={r.status_code}")
        except Exception as e:
            slog(f"  {url} -> HATA: {str(e)[:80]}")
    return None, False, ""


# ============================================================
# 1. VAVOO TOKEN (ping2) - vavoo_auth icin
# ============================================================
def get_auth_signature():
    global _vavoo_sig, _vavoo_sig_time
    if _vavoo_sig and (time.time() - _vavoo_sig_time) < CONFIG["SIG_CACHE_TTL"]:
        return _vavoo_sig

    slog("Vavoo Token (ping2) aliniyor...")
    headers = {"User-Agent": CONFIG["CDN_USER_AGENT"], "Accept": "application/json"}
    try:
        vec_req = requests.get("http://mastaaa1987.github.io/repo/veclist.json", headers=headers, timeout=10, verify=False)
        veclist = vec_req.json()["value"]
        slog(f"veclist: {len(veclist)} vec")

        sig = None
        # Tum ping2 URL'lerini dene
        for ping_url in CONFIG["PING2_URLS"]:
            if sig:
                break
            slog(f"  Deneniyor: {ping_url}")
            for i in range(3):
                vec = {"vec": random.choice(veclist)}
                try:
                    req = requests.post(ping_url, data=vec, headers=headers, timeout=10, verify=False).json()
                    if req.get("signed"):
                        sig = req["signed"]
                        slog(f"  Token alindi: {ping_url}")
                        break
                except Exception as e:
                    continue

        if sig:
            _vavoo_sig = sig
            _vavoo_sig_time = time.time()
            slog("Vavoo Token alindi!")
            return sig
        else:
            slog("Vavoo Token ALINAMADI (tum URL'ler denendi)")
    except Exception as e:
        slog(f"Vavoo Token HATASI: {e}")
    return None


# ============================================================
# 2. ADDONSIG (app/ping) - MediaHubMX imzasi icin
# ============================================================
def get_watchedsig():
    global _watched_sig, _watched_sig_time
    if _watched_sig and (time.time() - _watched_sig_time) < CONFIG["SIG_CACHE_TTL"]:
        return _watched_sig

    slog("addonSig (app/ping) aliniyor...")
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

    # Tum PING_URL'leri dene
    for ping_url in CONFIG["PING_URLS"]:
        try:
            slog(f"  Deneniyor: {ping_url}")
            resp = requests.post(ping_url, json=data, headers=headers, timeout=15, verify=False)
            result = resp.json()
            sig = result.get("addonSig")
            if sig:
                _watched_sig = sig
                _watched_sig_time = time.time()
                slog(f"  addonSig alindi: {ping_url}")
                return sig
            else:
                slog(f"  {ping_url} -> addonSig yok, keys={list(result.keys())[:5]}")
        except Exception as e:
            slog(f"  {ping_url} -> HATA: {str(e)[:80]}")

    slog("addonSig ALINAMADI (tum URL'ler denendi)")
    return None


# ============================================================
# 3. HLS RESOLVE (mediahubmx) - Tum BASE_URL fallback
# ============================================================
def resolve_hls_link(link):
    sig = get_watchedsig()
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

    # Tum BASE_URL'leri dene
    for base in CONFIG["BASE_URLS"]:
        try:
            url = f"{base}/mediahubmx-resolve.json"
            r = requests.post(url, json=data, headers=headers, timeout=CONFIG["RESOLVE_TIMEOUT"], verify=False)
            result = r.json()
            if result and isinstance(result, list) and len(result) > 0:
                resolved = result[0].get("url")
                if resolved:
                    slog(f"  Resolve OK: {base}")
                    return resolved
        except Exception as e:
            continue

    return None


# ============================================================
# 4. CHANNEL RESOLVE (4 yontem) - video.py'den cagrilir
# ============================================================
def resolve_channel(lid):
    """
    Kanal ID'sine gore stream URL'ini coz.
    Y1:   HLS field + Lokke resolve (catalog'tan gelen)
    Y1.5: URL field + Lokke resolve (catalog bos olsa bile)
    Y2:   URL + vavoo_auth token
    Y3:   Direkt URL (son care)
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM channels WHERE lid=?", (lid,))
    ch = c.fetchone()
    conn.close()
    if not ch:
        return None, "Kanal bulunamadi (DB'de yok)"

    name = ch["name"]
    url = ch["url"]
    hls = ch["hls"]

    # Y1: HLS field + Lokke (catalog'tan gelen)
    if hls:
        resolved = resolve_hls_link(hls)
        if resolved:
            return resolved, f"Y1-HLS: {name}"

    # Y1.5: URL field + Lokke resolve
    # Catalog bos olsa bile, live2 URL'sini resolve et
    if url:
        resolved = resolve_hls_link(url)
        if resolved:
            return resolved, f"Y1.5-URL-Resolve: {name}"

    # Y2: Standart vavoo_auth Token
    if url:
        sig = get_auth_signature()
        if sig:
            sep = "&" if "?" in url else "?"
            final = url + sep + "n=1&b=5&vavoo_auth=" + sig
            return final, f"Y2-Auth: {name}"

    # Y3: Direkt URL
    if url:
        return url, f"Y3-Direct: {name}"

    return None, f"URL yok: {name}"


# ============================================================
# 5. CATALOG FETCH (server.py'den cagrilir) - Tum BASE_URL fallback
# ============================================================
def fetch_catalog(sig, group_name):
    """
    MediaHubMX catalog'tan HLS linklerini cek.
    Tum BASE_URL'leri fallback olarak dener.
    """
    headers = {
        "user-agent": CONFIG["API_USER_AGENT"],
        "accept": "application/json",
        "mediahubmx-signature": sig,
    }
    data = {
        "language": "de", "region": "AT",
        "catalogId": "iptv", "id": "iptv",
        "adult": False, "sort": "name",
        "clientVersion": CONFIG["APP_VERSION"],
        "filter": {"group": group_name},
    }

    for base in CONFIG["BASE_URLS"]:
        try:
            url = f"{base}/mediahubmx-catalog.json"
            slog(f"  Catalog deneniyor: {url}")
            resp = requests.post(url, json=data, headers=headers, timeout=20, verify=False)
            catalog_data = resp.json()
            items = catalog_data.get("items", [])
            if items:
                slog(f"  Catalog OK: {base} ({len(items)} kayit)")
                return items
            else:
                slog(f"  Catalog bos: {base} status={resp.status_code} keys={list(catalog_data.keys())[:5]}")
        except Exception as e:
            slog(f"  Catalog HATA: {base} -> {str(e)[:80]}")

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
