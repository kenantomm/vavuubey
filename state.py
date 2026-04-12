"""
state.py - Ortak state modulu.
Token ve resolve fonksiyonlari burada.
video.py server.py'yi import ETMEZ, sadece state import eder.
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

# ONCEMLI: Token deneme lock - sonsuz donguyu onlemek icin
_token_lock = threading.Lock()
_vavoo_try_count = 0
_vavoo_cooldown_until = 0  # Basarisiz olursa 5 dk bekle


def slog(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    STARTUP_LOGS.append(entry)
    print(entry)


# ============================================================
# 1. VAVOO TOKEN (ping2) - RATE LIMITED
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
        vec_req = requests.get("http://mastaaa1987.github.io/repo/veclist.json", headers=headers, timeout=10, verify=False)
        veclist = vec_req.json()["value"]
        slog(f"veclist: {len(veclist)} vec")

        sig = None
        for i in range(5):
            vec = {"vec": random.choice(veclist)}
            req = requests.post("https://www.vavoo.tv/api/box/ping2", data=vec, headers=headers, timeout=10, verify=False).json()
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
# 2. LOKKE IMZA (app/ping)
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
        resp = requests.post("https://www.lokke.app/api/app/ping", json=data, headers=headers, timeout=15, verify=False)
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


# ============================================================
# 3. HLS RESOLVE (mediahubmx)
# ============================================================
def resolve_hls_link(link):
    sig = get_watchedsig()
    if not sig:
        return None
    headers = {"user-agent": "MediaHubMX/2", "accept": "application/json", "content-type": "application/json; charset=utf-8", "mediahubmx-signature": sig}
    data = {"language": "de", "region": "AT", "url": link, "clientVersion": "3.0.2"}
    try:
        r = requests.post("https://vavoo.to/mediahubmx-resolve.json", json=data, headers=headers, timeout=15, verify=False)
        result = r.json()
        if result and len(result) > 0:
            return result[0].get("url")
    except Exception as e:
        print(f"Resolve hatasi: {e}")
    return None


# ============================================================
# 4. CHANNEL RESOLVE (3 yontem + rate limiting)
# ============================================================
def resolve_channel(lid):
    """
    Kanal ID'sine gore stream URL'ini coz.
    Y1: HLS + Lokke (en stabil)
    Y2: URL + vavoo_auth token (rate limited - cooldown var)
    Y3: Direkt URL (token yoksa bile calisir)
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

    # Y1: HLS + Lokke
    if hls:
        resolved = resolve_hls_link(hls)
        if resolved:
            return resolved, f"Y1-HLS: {name}"

    # Y2: Standart Token (rate limited)
    if url:
        sig = get_auth_signature()
        if sig:
            sep = "&" if "?" in url else "?"
            final = url + sep + "n=1&b=5&vavoo_auth=" + sig
            return final, f"Y2-Auth: {name}"

    # Y3: Direkt URL (token yoksa bile HEMEN donecegiz, beklemiyoruz)
    if url:
        return url, f"Y3-Direct: {name}"

    return None, f"URL yok: {name}"


# ============================================================
# GRUP SIRALAMASI
# ============================================================
GROUP_ORDER = [
    # Turkish - sports processed LAST to avoid catching German channels
    "TR ULUSAL", "TR HABER", "TR BEIN SPORTS", "TR BELGESEL",
    "TR SINEMA UHD", "TR SINEMA", "TR MUZIK", "TR COCUK", "TR YEREL",
    "TR DINI", "TR RADYO",
    # German - sport groups MUST come before TR SPOR to catch "Eurosport","Sport1" etc
    "DE DEUTSCHLAND", "DE VIP SPORTS", "DE VIP SPORTS 2", "DE SPORT",
    "DE AUSTRIA", "DE SCHWEIZ", "DE FILM", "DE SERIEN", "DE KINO",
    "DE DOKU", "DE KIDS", "DE MUSIK", "DE INFOTAINMENT", "DE NEWS",
    "DE THEMEN", "DE SONSTIGE",
    # TR SPOR last - only catches remaining Turkish sport channels with specific keywords
    "TR SPOR",
]

GROUP_RULES = {
    # Turkish groups - VERY specific keywords to avoid matching German channels
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
    # German groups - German channels match HERE, not in TR groups
    "DE DEUTSCHLAND": [
        "ARD","ZDF","Das Erste","WDR","NDR","BR ","SWR","HR ","MDR",
        "RBB","Phoenix","3sat","KiKA","ONE","Arte","tagesschau24",
        "zdfinfo","zdfneo","tagesschau",
    ],
    "DE VIP SPORTS": [
        "Sky Sport","Sky Bundesliga","Eurosport","DAZN","Sport1",
        "beIN DE","beIN Sport DE",
    ],
    "DE VIP SPORTS 2": [
        "Sky Sport Austria","Telekom Sport","Magenta Sport",
    ],
    "DE SPORT": [
        "Sportdigital","Motorvision","Sport +","SPORT1",
        "Eurosport 2","Eurosport 3",
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
    # TR SPOR - ONLY very specific Turkish sport keywords (processed LAST in GROUP_ORDER)
    "TR SPOR": [
        "A Spor","TRT Spor","TJK TV","TJK","S Sport",
        "GS TV","FB TV","BJK TV",
        "Fenerbahce TV","Galatasaray TV","Trabzonspor TV",
        "S Sport 1","S Sport 2",
    ],
}
