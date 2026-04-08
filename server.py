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
BASE_HOST = os.environ.get("BASE_HOST", "")

DATA_READY = False
STARTUP_ERROR = None
LOAD_TIME = 0

# ============================================================
# 1. STANDART VAVOO TOKEN (ping2) - api.py/vavoo.py'den portlandi
# ============================================================
_vavoo_sig = None
_vavoo_sig_time = 0


def get_auth_signature():
    """Vavoo ping2 token al (standart auth)"""
    global _vavoo_sig, _vavoo_sig_time

    # Cache: 30 dakika
    if _vavoo_sig and (time.time() - _vavoo_sig_time) < 1800:
        return _vavoo_sig

    log.info("Vavoo Token (ping2) aliniyor...")
    headers = {"User-Agent": "VAVOO/2.6", "Accept": "application/json"}
    try:
        # vec listesini al
        vec_req = requests.get(
            "http://mastaaa1987.github.io/repo/veclist.json",
            headers=headers,
            timeout=10,
        )
        veclist = vec_req.json()["value"]
        sig = None
        for _ in range(5):
            vec = {"vec": random.choice(veclist)}
            req = requests.post(
                "https://www.vavoo.tv/api/box/ping2",
                data=vec,
                headers=headers,
                timeout=10,
            ).json()
            if req.get("signed"):
                sig = req["signed"]
                break
        if sig:
            _vavoo_sig = sig
            _vavoo_sig_time = time.time()
            log.info("Vavoo Token basariyla alindi!")
            return sig
    except Exception as e:
        log.error("Vavoo Token hatasi: %s", e)
    return None


# ============================================================
# 2. LOKKE / WATCHED IMZA (app/ping)
# ============================================================
_watched_sig = None
_watched_sig_time = 0


def get_watchedsig():
    """Lokke imzasi al (MediaHubMX icin gerekli)"""
    global _watched_sig, _watched_sig_time

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
            "os": {
                "name": "linux",
                "version": "Ubuntu 22.04",
                "abis": ["x64"],
                "host": "RENDER",
            },
            "app": {"platform": "electron"},
            "version": {
                "package": "app.lokke.main",
                "binary": "1.0.19",
                "js": "1.0.19",
            },
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
        "proxy": {
            "supported": ["ss"],
            "engine": "cu",
            "enabled": False,
            "autoServer": True,
            "id": 0,
        },
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


# ============================================================
# 3. LINK COZUMLEME (mediahubmx-resolve)
# ============================================================


def resolve_hls_link(link):
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
# 4. CHANNEL RESOLVE (3 yontemli - api.py mantigi)
# ============================================================


def resolve_channel(lid):
    """
    Kanal ID'sine gore stream URL'ini coz.
    YONTEM 1: HLS + Lokke Imzasi (en stabil)
    YONTEM 2: Standart Token (vavoo_auth parametresi)
    YONTEM 3: Dogrudan URL (son care)
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM channels WHERE lid=?", (lid,))
    ch = c.fetchone()
    conn.close()

    if not ch:
        return None

    name = ch["name"]
    url = ch["url"]
    hls = ch["hls"]

    # YONTEM 1: HLS + Lokke Imzasi
    if hls:
        log.info("[Y1-HLS] Cozuluyor: %s", name)
        resolved = resolve_hls_link(hls)
        if resolved:
            log.info("[Y1-HLS] Basarili: %s", name)
            return resolved
        log.warning("[Y1-HLS] Basarisiz: %s", name)

    # YONTEM 2: Standart Token (vavoo_auth)
    if url:
        log.info("[Y2-Auth] Token deneniyor: %s", name)
        sig = get_auth_signature()
        if sig:
            base_url = str(url)
            separator = "&" if "?" in base_url else "?"
            final_link = base_url + separator + "n=1&b=5&vavoo_auth=" + sig
            log.info("[Y2-Auth] Basarili: %s", name)
            return final_link
        log.warning("[Y2-Auth] Token alinamadi: %s", name)

    # YONTEM 3: Dogrudan URL (calismayabilir ama son care)
    if url:
        log.info("[Y3-Direct] Dogrudan: %s", name)
        return url

    return None


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
# VAVOO'DAN KANAL CEKME
# ============================================================


def fetch_vavoo_channels():
    """Vavoo live2 API'den TR/DE kanallarini ceker"""
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
        log.error("Gecersiz kanal listesi")
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
        if not any(
            x in group_raw
            for x in [
                "turkey", "turkish", "tr", "türk", "türkei",
                "deutschland", "german", "deutsch", "austria", "österreich",
                "schweiz", "switzerland", "at ", "ch ",
            ]
        ):
            continue

        # Turk karakterleri temizle (ASCII)
        name_clean = re.sub(r"[^\x00-\x7F]+", "", name)
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
    """MediaHubMX catalog'dan HLS linklerini ceker"""
    log.info("MediaHubMX catalog'dan HLS linkleri cekiliyor...")

    sig = get_watchedsig()
    if not sig:
        log.warning("Lokke imzasi yok, HLS linkleri atlanacak")
        return False

    headers = {
        "user-agent": "MediaHubMX/2",
        "accept": "application/json",
        "mediahubmx-signature": sig,
    }
    updated = 0

    for group_name in ["Turkey", "Deutschland"]:
        try:
            data = {
                "language": "de",
                "region": "AT",
                "catalogId": "iptv",
                "id": "iptv",
                "adult": False,
                "sort": "name",
                "clientVersion": "3.0.2",
                "filter": {"group": group_name},
            }
            resp = requests.post(
                "https://www.vavoo.to/mediahubmx-catalog.json",
                json=data,
                headers=headers,
                timeout=20,
            )
            items = resp.json().get("items", [])
            log.info("%s HLS: %d kayit", group_name, len(items))

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            for item in items:
                hls_url = item.get("url", "")
                name_clean = re.sub(r"[^\x00-\x7F]+", "", item.get("name", ""))
                if hls_url and name_clean:
                    c.execute(
                        "UPDATE channels SET hls=? WHERE name=?", (hls_url, name_clean)
                    )
                    updated += 1
            conn.commit()
            conn.close()
        except Exception as e:
            log.error("%s HLS hatasi: %s", group_name, e)

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
# M3U URETIMI - HER ZAMAN PROXY URL KULLAN!
# ============================================================


def generate_m3u():
    host = BASE_HOST
    if not host:
        log.warning("BASE_HOST yok! M3U proxy URL'lerle olusturulamadi")
        return

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

        # HER ZAMAN proxy URL kullan!
        # /channel/{lid} endpoint'i 3 yontemle resolve eder:
        # 1. HLS + Lokke, 2. URL + vavoo_auth, 3. Direkt URL
        stream_url = f"{host}/channel/{lid}"

        lines.append(
            f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{name}'
        )
        lines.append(stream_url)

    with open(M3U_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("M3U uretildi: %s (%d kanal, PROXY URL)", M3U_PATH, len(channels))


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
