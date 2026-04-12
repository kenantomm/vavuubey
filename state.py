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
# 1. VAVOO TOKEN (ping2)
# ============================================================
def get_auth_signature():
    global _vavoo_sig, _vavoo_sig_time
    if _vavoo_sig and (time.time() - _vavoo_sig_time) < 1800:
        return _vavoo_sig

    slog("Vavoo Token (ping2) aliniyor...")
    headers = {"User-Agent": "VAVOO/2.6", "Accept": "application/json"}
    try:
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
            slog("Vavoo Token alindi!")
            return sig
        else:
            slog("Vavoo Token ALINAMADI (5 deneme)")
    except Exception as e:
        slog(f"Vavoo Token HATASI: {e}")
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
        r = requests.post("https://www.vavoo.to/mediahubmx-resolve.json", json=data, headers=headers, timeout=15, verify=False)
        result = r.json()
        if result and len(result) > 0:
            return result[0].get("url")
    except Exception as e:
        print(f"Resolve hatasi: {e}")
    return None


# ============================================================
# 4. CHANNEL RESOLVE (3 yontem) - video.py'den cagrilir
# ============================================================
def resolve_channel(lid):
    """
    Kanal ID'sine gore stream URL'ini coz.
    Y1: HLS + Lokke (en stabil)
    Y2: URL + vavoo_auth token
    Y3: Direkt URL (son care)
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

    # Y1: HLS + Lokke
    if hls:
        resolved = resolve_hls_link(hls)
        if resolved:
            return resolved, f"Y1-HLS: {name}"

    # Y2: Standart Token
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
# 5. EPG DATA (XMLTV)
# ============================================================
def get_epg_data():
    """
    Vavoo EPG'den kanal program verisini cek ve XMLTV dondur.
    """
    import sqlite3
    from datetime import datetime, timedelta
    import xml.etree.ElementTree as ET

    sig = get_auth_signature()
    if not sig:
        return None

    headers = {"User-Agent": "VAVOO/2.6", "Accept": "application/json"}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT lid, name, grp FROM channels ORDER BY lid")
        channels = c.fetchall()
        conn.close()

        tv = ET.Element("tv")
        tv.set("generator-info-name", "VxParser")
        tv.set("source-info-url", "https://www.vavoo.to")

        now = datetime.utcnow()
        today_str = now.strftime("%Y-%m-%d")

        for ch in channels:
            lid = ch["lid"]
            name = ch["name"]

            # channel element
            ch_el = ET.SubElement(tv, "channel")
            ch_el.set("id", str(lid))
            display = ET.SubElement(ch_el, "display-name")
            display.text = name

            # programme element (1 gun)
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
