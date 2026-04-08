import os
import sys
import sqlite3
import json
import random
import time
import re
import base64
import ssl
import logging
import threading
import traceback

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("vxparser")

PORT = int(os.environ.get("PORT", 10000))
DB_PATH = os.environ.get("DB_PATH", "/tmp/vxparser.db")
M3U_PATH = os.environ.get("M3U_PATH", "/tmp/playlist.m3u")
BASE_HOST = os.environ.get("BASE_HOST", "")  # Render URL, örn: https://vavuubey.onrender.com

DATA_READY = False
STARTUP_ERROR = None
LOAD_TIME = 0

urllib3_installed = False
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    urllib3_installed = True
except ImportError:
    pass

# ============================================================
# GRUP SIRALAMASI
# ============================================================
GROUP_ORDER = [
    "TR ULUSAL",
    "TR HABER",
    "TR BEIN SPORTS",
    "TR SPOR",
    "TR BELGESEL",
    "TR SINEMA UHD",
    "TR SINEMA",
    "TR MUZIK",
    "TR COCUK",
    "TR YEREL",
    "TR DINI",
    "TR RADYO",
    "DE DEUTSCHLAND",
    "DE VIP SPORTS",
    "DE VIP SPORTS 2",
    "DE SPORT",
    "DE AUSTRIA",
    "DE SCHWEIZ",
    "DE FILM",
    "DE SERIEN",
    "DE KINO",
    "DE DOKU",
    "DE KIDS",
    "DE MUSIK",
    "DE INFOTAINMENT",
    "DE NEWS",
    "DE THEMEN",
    "DE SONSTIGE",
]

GROUP_RULES = {
    "TR ULUSAL": [
        "TRT 1", "Show TV", "Star TV", "ATV", "Kanal D", "FOX TV", "TV8",
        "Tele1", "Beyaz TV", "TV 8.5", "A2", "TRT 4K", "Tabii", "Gain",
        "TV 100", "Flash TV", "Kanal 7", "TGRT", "TLC", "D MAX", "ERT",
    ],
    "TR HABER": [
        "Haber", "CNN Turk", "HABER", "NTV", "TRT Haber", "Bloomberg",
        "TVNET", "A Haber", "Benguturk", "Haber Global", "Ulusal Kanal",
        "Sky Turk", "TGRT Haber",
    ],
    "TR BEIN SPORTS": [
        "beIN Sports", "beIN SPORT", "beIN", "beIN 4K", "beIN MAX",
    ],
    "TR SPOR": [
        "Spor", "A Spor", "TRT Spor", "TJK", "S Sport", "GS TV",
        "FB TV", "BJK TV", "Fenerbahce", "Galatasaray",
    ],
    "TR BELGESEL": [
        "Belgesel", "Nat Geo", "Discovery", "Animal", "History",
        "Yaban TV", "BBC Earth",
    ],
    "TR SINEMA UHD": ["4K", "UHD"],
    "TR SINEMA": [
        "Film", "Sinema", "Cinema", "Movie", "Movies", "DigiMAX",
        "FilmBox", "Magic Box", "Yesilcam", "Dream TV",
    ],
    "TR MUZIK": [
        "Muzik", "Kral TV", "Kral Pop", "Power TV", "Power Turk",
        "Number One", "NR1",
    ],
    "TR COCUK": [
        "Cocuk", "Cartoon", "Disney", "Nick", "Minika", "Baby TV", "Pepee",
    ],
    "TR YEREL": ["Yerel"],
    "TR DINI": [
        "Dini", "Din", "Diyanet", "Semerkand", "Hilal", "Lalegul",
    ],
    "TR RADYO": ["Radyo", "Radio", "FM"],
    "DE DEUTSCHLAND": [
        "ARD", "ZDF", "Das Erste", "WDR", "NDR", "BR ", "SWR", "HR ",
        "MDR", "RBB", "Phoenix", "3sat", "KiKA", "ONE", "Arte",
        "tagesschau24", "zdfinfo", "zdfneo",
    ],
    "DE VIP SPORTS": [
        "Sky Sport", "Sky Bundesliga", "Eurosport", "DAZN", "Sport1",
    ],
    "DE VIP SPORTS 2": [
        "Sky Sport Austria", "Telekom Sport", "Magenta Sport",
    ],
    "DE SPORT": [
        "Sport ", "Eurosport", "Sportdigital", "Motorvision",
    ],
    "DE AUSTRIA": ["ORF", "Puls 4", "Servus"],
    "DE SCHWEIZ": ["SRF", "Swiss"],
    "DE FILM": [
        "Sky Cinema", "RTL+", "13th Street", "AXN", "TNT Serie",
        "TNT Film", "Sky Hits", "Sky Action",
    ],
    "DE SERIEN": [
        "Serie", "RTL", "Sat.1", "ProSieben", "VOX", "kabel eins",
        "RTL2", "Super RTL", "Sixx", "TELE 5",
    ],
    "DE KINO": ["Kino"],
    "DE DOKU": [
        "Doku", "Docu", "D-MAX", "N24 Doku", "Spiegel TV",
    ],
    "DE KIDS": ["Kind", "Kids", "Toggo"],
    "DE MUSIK": ["Musik", "VIVA", "Deluxe Music"],
    "DE INFOTAINMENT": [
        "Info", "N24", "WELT", "n-tv", "BBC World", "France 24",
    ],
    "DE NEWS": ["News", "Tagesschau"],
    "DE THEMEN": [
        "Shop", "QVC", "HSE", "Bibel TV", "Sonstig", "Regional",
    ],
}

# ============================================================
# LOKKE / WATCHED IMZA SISTEMI (vavoo.py'den portlandi)
# ============================================================
_watched_sig = None
_watched_sig_time = 0


def get_watchedsig():
    """Lokke imzasi al (app/ping endpoint)"""
    global _watched_sig, _watched_sig_time

    # Cache: 30 dakika
    if _watched_sig and (time.time() - _watched_sig_time) < 1800:
        return _watched_sig

    log.info("Lokke Imza (app/ping) aliniyor...")
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
            "os": {"name": "linux", "version": "Ubuntu 22.04", "abis": ["x64"], "host": "RENDER"},
            "app": {"platform": "electron"},
            "version": {"package": "app.lokke.main", "binary": "1.0.19", "js": "1.0.19"},
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
        "firstAppStart": int(time.time() * 1000) - 10000,
        "lastAppStart": int(time.time() * 1000) - 10000,
        "ipLocation": 0,
        "adblockEnabled": True,
        "proxy": {"supported": ["ss"], "engine": "cu", "enabled": False, "autoServer": True, "id": 0},
        "iap": {"supported": False},
    }
    try:
        resp = requests.post(
            "https://www.lokke.app/api/app/ping",
            json=data,
            headers=headers,
            timeout=15,
        )
        result = resp.json()
        sig = result.get("addonSig")
        if sig:
            _watched_sig = sig
            _watched_sig_time = time.time()
            log.info("Lokke Imzasi basariyla alindi!")
            return sig
    except Exception as e:
        log.error("Lokke Imza Hatasi: %s", e)
    return None


def resolve_link(link):
    """MediaHubMX ile HLS linki coz"""
    sig = get_watchedsig()
    if not sig:
        return None

    headers = {
        "user-agent": "MediaHubMX/2",
        "accept": "application/json",
        "content-type": "application/json; charset=utf-8",
        "mediahubmx-signature": sig,
    }
    data = {
        "language": "de",
        "region": "AT",
        "url": link,
        "clientVersion": "3.0.2",
    }
    try:
        r = requests.post(
            "https://vavoo.to/mediahubmx-resolve.json",
            json=data,
            headers=headers,
            timeout=15,
        )
        result = r.json()
        if result and len(result) > 0:
            return result[0].get("url")
    except Exception as e:
        log.error("Link cozumleme hatasi: %s", e)
    return None


# ============================================================
# VERITABANI
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "CREATE TABLE IF NOT EXISTS categories "
        "(cid INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, sort_order INTEGER DEFAULT 9999)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS channels "
        "(lid INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, grp TEXT DEFAULT '', "
        "cid INTEGER DEFAULT 0, logo TEXT DEFAULT '', url TEXT DEFAULT '', "
        "hls TEXT DEFAULT '', sort_order INTEGER DEFAULT 9999)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_ch_cid ON channels(cid)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ch_name ON channels(name)")
    conn.commit()
    conn.close()
    log.info("DB baslatildi: %s", DB_PATH)


# ============================================================
# VAVOO'DAN KANAL CEKME (vavoo.py sky_dbfill mantigi)
# ============================================================

def fetch_vavoo_channels():
    """
    Vavoo live2 API'den kanallari ceker.
    Kaynak: https://www.vavoo.to/live2/index?output=json
    """
    log.info("Vavoo live2'den kanallar cekiliyor...")

    try:
        headers = {"User-Agent": "VAVOO/2.6"}
        resp = requests.get(
            "https://www.vavoo.to/live2/index?output=json",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        channel_list = resp.json()
    except Exception as e:
        log.error("Vavoo live2 cekme hatasi: %s", e)
        return False

    if not channel_list or not isinstance(channel_list, list):
        log.error("Gecersiz kanal listesi formati")
        return False

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    added = 0

    for ch in channel_list:
        name = ch.get("name", "")
        group_raw = ch.get("group", "").lower()
        url = ch.get("url", "")
        logo = ch.get("logo", "")

        # Sadece TR ve DE gruplarini al
        if not any(x in group_raw for x in [
            "turkey", "turkish", "tr", "türk", "türkei",
            "deutschland", "german", "deutsch", "austria", "österreich",
            "schweiz", "switzerland", "at ", "ch ",
        ]):
            continue

        # Turk karakterleri temizle (ASCII)
        name_clean = re.sub(r'[^\x00-\x7F]+', '', name)
        if not name_clean:
            continue

        c.execute(
            "INSERT OR REPLACE INTO channels(name, grp, cid, logo, url, hls, sort_order) "
            "VALUES(?, ?, 0, ?, ?, '', 9999)",
            (name_clean, ch.get("group", ""), logo, url),
        )
        added += 1

    conn.commit()
    conn.close()
    log.info("Vavoo live2: %d TR/DE kanal eklendi", added)
    return True


def fetch_hls_links():
    """
    MediaHubMX catalog'dan HLS linklerini ceker.
    Kaynak: https://www.vavoo.to/mediahubmx-catalog.json
    """
    log.info("MediaHubMX catalog'dan HLS linkleri cekiliyor...")

    sig = get_watchedsig()
    if not sig:
        log.error("Lokke imzasi yok, HLS linkleri alinamadi!")
        return False

    headers = {
        "user-agent": "MediaHubMX/2",
        "accept": "application/json",
        "mediahubmx-signature": sig,
    }

    updated = 0

    # Turkey grubu icin HLS linkleri al
    try:
        data_turkey = {
            "language": "de",
            "region": "AT",
            "catalogId": "iptv",
            "id": "iptv",
            "adult": False,
            "sort": "name",
            "clientVersion": "3.0.2",
            "filter": {"group": "Turkey"},
        }
        resp = requests.post(
            "https://vavoo.to/mediahubmx-catalog.json",
            json=data_turkey,
            headers=headers,
            timeout=20,
        )
        items = resp.json().get("items", [])
        log.info("Turkey HLS: %d kayit bulundu", len(items))

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for item in items:
            hls_url = item.get("url", "")
            name_clean = re.sub(r'[^\x00-\x7F]+', '', item.get("name", ""))
            if hls_url and name_clean:
                c.execute("UPDATE channels SET hls=? WHERE name=?", (hls_url, name_clean))
                updated += 1
        conn.commit()
        conn.close()

    except Exception as e:
        log.error("Turkey HLS hatasi: %s", e)

    # Deutschland grubu icin de dene
    try:
        data_de = {
            "language": "de",
            "region": "DE",
            "catalogId": "iptv",
            "id": "iptv",
            "adult": False,
            "sort": "name",
            "clientVersion": "3.0.2",
            "filter": {"group": "Deutschland"},
        }
        resp = requests.post(
            "https://vavoo.to/mediahubmx-catalog.json",
            json=data_de,
            headers=headers,
            timeout=20,
        )
        items = resp.json().get("items", [])
        log.info("Deutschland HLS: %d kayit bulundu", len(items))

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for item in items:
            hls_url = item.get("url", "")
            name_clean = re.sub(r'[^\x00-\x7F]+', '', item.get("name", ""))
            if hls_url and name_clean:
                c.execute("UPDATE channels SET hls=? WHERE name=?", (hls_url, name_clean))
                updated += 1
        conn.commit()
        conn.close()

    except Exception as e:
        log.error("Deutschland HLS hatasi: %s", e)

    log.info("Toplam %d HLS linki guncellendi", updated)
    return True


# ============================================================
# GRUP REMAPPING
# ============================================================

def remap_groups():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM categories")
    c.execute("UPDATE channels SET cid=0, grp=''")
    conn.commit()

    for idx, gn in enumerate(GROUP_ORDER):
        c.execute(
            "INSERT OR IGNORE INTO categories(cid, name, sort_order) VALUES(?, ?, ?)",
            (idx + 1, gn, idx + 1),
        )
    conn.commit()

    c.execute("SELECT lid, name FROM channels")
    updated = 0
    for lid, name in c.fetchall():
        assigned = False
        for gi, gn in enumerate(GROUP_ORDER):
            for kw in GROUP_RULES.get(gn, []):
                if kw.lower() in name.lower():
                    c.execute(
                        "UPDATE channels SET cid=?, grp=?, sort_order=? WHERE lid=?",
                        (gi + 1, gn, gi + 1, lid),
                    )
                    updated += 1
                    assigned = True
                    break
            if assigned:
                break
        if not assigned:
            c.execute("SELECT cid FROM categories WHERE name='DE SONSTIGE'")
            row = c.fetchone()
            if row:
                c.execute(
                    "UPDATE channels SET cid=?, grp='DE SONSTIGE', sort_order=9998 WHERE lid=?",
                    (row[0], lid),
                )

    conn.commit()
    conn.close()
    log.info("Grup remap: %d kanal guncellendi", updated)


# ============================================================
# M3U URETIMI
# ============================================================

def generate_m3u():
    host = BASE_HOST
    if not host:
        log.warning("BASE_HOST yok, M3U dogrudan URL'lerle olusturuluyor")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT c.name, c.url, c.hls, c.lid, c.logo, "
        "COALESCE(cat.name, 'Sonstige') as group_name "
        "FROM channels c "
        "LEFT JOIN categories cat ON c.cid = cat.cid "
        "ORDER BY COALESCE(cat.sort_order, 9999), c.name"
    )
    channels = c.fetchall()
    conn.close()

    lines = ["#EXTM3U"]
    for ch in channels:
        name = ch["name"]
        logo = ch["logo"] or ""
        group = ch["group_name"]
        lid = ch["lid"]

        # HLS linki varsa proxy URL kullan, yoksa dogrudan URL
        if ch["hls"] and host:
            stream_url = f"{host}/channel/{lid}"
        elif ch["url"]:
            stream_url = ch["url"]
        else:
            continue

        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{name}')
        lines.append(stream_url)

    with open(M3U_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("M3U uretildi: %s (%d kanal)", M3U_PATH, len(channels))


# ============================================================
# CHANNEL RESOLVE (API endpoint icin)
# ============================================================

def resolve_channel(lid):
    """
    Kanal ID'sine gore stream URL'ini coz.
    Oncelik: HLS (Lokke resolve) > Standart URL
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM channels WHERE lid=?", (lid,))
    ch = c.fetchone()
    conn.close()

    if not ch:
        return None

    # YONTEM 1: HLS + Lokke Imzasi
    if ch["hls"]:
        log.info("HLS Cozuluyor: %s", ch["name"])
        resolved = resolve_link(ch["hls"])
        if resolved:
            return resolved

    # YONTEM 2: Standart URL
    if ch["url"]:
        return ch["url"]

    return None


# ============================================================
# BASLANGIC SEKANSI
# ============================================================

def startup_sequence():
    global DATA_READY, STARTUP_ERROR
    start = time.time()
    try:
        log.info("=== VxParser Baslangic ===")
        log.info("PORT=%d | DB=%s | HOST=%s", PORT, DB_PATH, BASE_HOST)

        init_db()
        fetch_vavoo_channels()
        fetch_hls_links()
        remap_groups()
        generate_m3u()

        LOAD_TIME = time.time() - start
        DATA_READY = True
        log.info("=== Hazir! (%.1fs) ===", LOAD_TIME)
    except Exception as e:
        STARTUP_ERROR = str(e)
        log.error("Baslangic hatasi: %s", e)
        traceback.print_exc()


def main():
    log.info("VxParser baslatiliyor...")
    threading.Thread(target=startup_sequence, daemon=True).start()
    import uvicorn
    from video import app
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
