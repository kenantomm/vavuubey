import os
import sys
import sqlite3
import json
import random
import time
import re
import base64
import logging
import threading
import traceback

import requests
import urllib3

# SSL UYARILARINI KAPAT - Vavoo SSL sertifikasi sorunlu olabilir
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("vxparser")

PORT = int(os.environ.get("PORT", 10000))
DB_PATH = os.environ.get("DB_PATH", "/tmp/vxparser.db")
M3U_PATH = os.environ.get("M3U_PATH", "/tmp/playlist.m3u")

DATA_READY = False
STARTUP_ERROR = None
LOAD_TIME = 0

# Startup loglarini kaydet (debug icin)
STARTUP_LOGS = []

def slog(msg):
    """Log yaz ve STARTUP_LOGS'a ekle"""
    log.info(msg)
    STARTUP_LOGS.append(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ============================================================
# 1. STANDART VAVOO TOKEN (ping2)
# ============================================================
_vavoo_sig = None
_vavoo_sig_time = 0


def get_auth_signature():
    global _vavoo_sig, _vavoo_sig_time
    if _vavoo_sig and (time.time() - _vavoo_sig_time) < 1800:
        return _vavoo_sig

    slog("Vavoo Token (ping2) aliniyor...")
    headers = {"User-Agent": "VAVOO/2.6", "Accept": "application/json"}
    try:
        # veclist cek
        vec_req = requests.get(
            "http://mastaaa1987.github.io/repo/veclist.json",
            headers=headers, timeout=10, verify=False,
        )
        veclist = vec_req.json()["value"]
        slog(f"veclist: {len(veclist)} vec yuklendi")

        sig = None
        for attempt in range(5):
            vec = {"vec": random.choice(veclist)}
            req = requests.post(
                "https://www.vavoo.tv/api/box/ping2",
                data=vec, headers=headers, timeout=10, verify=False,
            ).json()
            if req.get("signed"):
                sig = req["signed"]
                slog("Vavoo Token alindi!")
                break
            else:
                slog(f"ping2 deneme {attempt+1}: signed yok, cevap={list(req.keys())}")

        if sig:
            _vavoo_sig = sig
            _vavoo_sig_time = time.time()
            return sig
        else:
            slog("Vavoo Token ALINAMADI! (5 deneme basarisiz)")

    except Exception as e:
        slog(f"Vavoo Token HATASI: {e}")
    return None


# ============================================================
# 2. LOKKE / WATCHED IMZA (app/ping)
# ============================================================
_watched_sig = None
_watched_sig_time = 0


def get_watchedsig():
    global _watched_sig, _watched_sig_time
    if _watched_sig and (time.time() - _watched_sig_time) < 1800:
        return _watched_sig

    slog("Lokke Imza (app/ping) aliniyor...")
    headers = {
        "user-agent": "okhttp/4.11.0",
        "accept": "application/json",
        "content-type": "application/json; charset=utf-8",
    }
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
        resp = requests.post(
            "https://www.lokke.app/api/app/ping",
            json=data, headers=headers, timeout=15, verify=False,
        )
        result = resp.json()
        sig = result.get("addonSig")
        if sig:
            _watched_sig = sig
            _watched_sig_time = time.time()
            slog("Lokke Imzasi alindi!")
            return sig
        else:
            slog(f"Lokke cevap={list(result.keys())} (addonSig yok)")
    except Exception as e:
        slog(f"Lokke Imza HATASI: {e}")
    return None


# ============================================================
# 3. LINK COZUMLEME (mediahubmx-resolve)
# ============================================================

def resolve_hls_link(link):
    sig = get_watchedsig()
    if not sig:
        return None
    headers = {
        "user-agent": "MediaHubMX/2",
        "accept": "application/json",
        "content-type": "application/json; charset=utf-8",
        "mediahubmx-signature": sig,
    }
    data = {"language": "de", "region": "AT", "url": link, "clientVersion": "3.0.2"}
    try:
        r = requests.post(
            "https://vavoo.to/mediahubmx-resolve.json",
            json=data, headers=headers, timeout=15, verify=False,
        )
        result = r.json()
        if result and len(result) > 0:
            return result[0].get("url")
    except Exception as e:
        log.error("Resolve hatasi: %s", e)
    return None


# ============================================================
# 4. CHANNEL RESOLVE (3 yontemli)
# ============================================================

def resolve_channel(lid):
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

    # Y1: HLS + Lokke
    if hls:
        resolved = resolve_hls_link(hls)
        if resolved:
            return resolved

    # Y2: Standart Token
    if url:
        sig = get_auth_signature()
        if sig:
            sep = "&" if "?" in url else "?"
            return url + sep + "n=1&b=5&vavoo_auth=" + sig

    # Y3: Direkt URL
    if url:
        return url
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


# ============================================================
# VERITABANI
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS categories (cid INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, sort_order INTEGER DEFAULT 9999)")
    c.execute("CREATE TABLE IF NOT EXISTS channels (lid INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, grp TEXT DEFAULT '', cid INTEGER DEFAULT 0, logo TEXT DEFAULT '', url TEXT DEFAULT '', hls TEXT DEFAULT '', sort_order INTEGER DEFAULT 9999)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ch_cid ON channels(cid)")
    conn.commit()
    conn.close()
    slog("DB baslatildi: " + DB_PATH)


# ============================================================
# VAVOO'DAN KANAL CEKME
# ============================================================

def fetch_vavoo_channels():
    slog("Vavoo live2 cekiliyor...")

    # Adim 1: DNS ve baglanti testi
    try:
        slog("Baglanti testi: vavoo.to...")
        test = requests.get("https://www.vavoo.to/", timeout=10, verify=False, headers={"User-Agent": "VAVOO/2.6"})
        slog(f"vavoo.to erisim basarili! Status={test.status_code}")
    except Exception as e:
        slog(f"vavoo.to erisim BASARISIZ: {e}")
        return False

    # Adim 2: Kanal listesini cek
    try:
        headers = {"User-Agent": "VAVOO/2.6"}
        resp = requests.get(
            "https://www.vavoo.to/live2/index?output=json",
            headers=headers, timeout=30, verify=False,
        )
        resp.raise_for_status()
        channel_list = resp.json()
        slog(f"Kanal listesi alindi: {len(channel_list) if isinstance(channel_list, list) else 'liste degil'} kayit")
    except Exception as e:
        slog(f"Kanal listesi cekme HATASI: {e}")
        return False

    if not channel_list or not isinstance(channel_list, list):
        slog("Gecersiz kanal listesi!")
        return False

    # Adim 3: TR ve DE filtrele
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    added = 0
    tr_count = 0
    de_count = 0

    for ch in channel_list:
        name = ch.get("name", "")
        group_raw = ch.get("group", "").lower()
        url = ch.get("url", "")
        logo = ch.get("logo", "")

        is_tr = any(x in group_raw for x in ["turkey", "turkish", "tr", "türk", "türkei"])
        is_de = any(x in group_raw for x in ["deutschland", "german", "deutsch", "austria", "österreich", "schweiz", "switzerland"])

        if not is_tr and not is_de:
            continue

        name_clean = re.sub(r"[^\x00-\x7F]+", "", name)
        if not name_clean:
            continue

        c.execute("INSERT OR REPLACE INTO channels(name,grp,cid,logo,url,hls,sort_order) VALUES(?,?,0,?,?,9999)",
                  (name_clean, ch.get("group", ""), logo, url))
        added += 1
        if is_tr:
            tr_count += 1
        if is_de:
            de_count += 1

    conn.commit()
    conn.close()
    slog(f"Toplam: {added} kanal (TR={tr_count}, DE={de_count})")
    return True


def fetch_hls_links():
    slog("MediaHubMX HLS linkleri cekiliyor...")
    sig = get_watchedsig()
    if not sig:
        slog("Lokke imzasi YOK! HLS linkleri atlanacak.")
        slog("HLS olmadan da Y2 (vavoo_auth) yontemi calisir.")
        return False

    headers = {"user-agent": "MediaHubMX/2", "accept": "application/json", "mediahubmx-signature": sig}
    updated = 0

    for group_name in ["Turkey", "Deutschland"]:
        try:
            data = {"language":"de","region":"AT","catalogId":"iptv","id":"iptv","adult":False,"sort":"name","clientVersion":"3.0.2","filter":{"group":group_name}}
            resp = requests.post(
                "https://www.vavoo.to/mediahubmx-catalog.json",
                json=data, headers=headers, timeout=20, verify=False,
            )
            items = resp.json().get("items", [])
            slog(f"{group_name} HLS: {len(items)} kayit bulundu")
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            for item in items:
                hls_url = item.get("url", "")
                name_clean = re.sub(r"[^\x00-\x7F]+", "", item.get("name", ""))
                if hls_url and name_clean:
                    c.execute("UPDATE channels SET hls=? WHERE name=?", (hls_url, name_clean))
                    updated += 1
            conn.commit()
            conn.close()
        except Exception as e:
            slog(f"{group_name} HLS HATASI: {e}")

    slog(f"Toplam {updated} HLS linki guncellendi")
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
        c.execute("INSERT OR IGNORE INTO categories(cid,name,sort_order) VALUES(?,?,?)", (idx+1, gn, idx+1))
    conn.commit()

    c.execute("SELECT lid, name FROM channels")
    updated = 0
    for lid, name in c.fetchall():
        assigned = False
        for gi, gn in enumerate(GROUP_ORDER):
            for kw in GROUP_RULES.get(gn, []):
                if kw.lower() in name.lower():
                    c.execute("UPDATE channels SET cid=?,grp=?,sort_order=? WHERE lid=?", (gi+1, gn, gi+1, lid))
                    updated += 1
                    assigned = True
                    break
            if assigned:
                break
        if not assigned:
            c.execute("SELECT cid FROM categories WHERE name='DE SONSTIGE'")
            row = c.fetchone()
            if row:
                c.execute("UPDATE channels SET cid=?,grp='DE SONSTIGE',sort_order=9998 WHERE lid=?", (row[0], lid))
    conn.commit()
    conn.close()
    slog(f"Grup remap: {updated} kanal guncellendi")


# ============================================================
# BASLANGIC
# ============================================================

def startup_sequence():
    global DATA_READY, STARTUP_ERROR
    start = time.time()
    try:
        slog("=== VxParser Baslangic ===")
        slog(f"PORT={PORT} | DB={DB_PATH}")

        # Adim 1: Lokke imzasi al (once bu lazim)
        slog("Adim 1/5: Lokke imzasi aliniyor...")
        lokke = get_watchedsig()
        slog(f"Adim 1/5: Lokke imzasi={'ALINDI' if lokke else 'BASARISIZ'}")

        # Adim 2: Vavoo token al
        slog("Adim 2/5: Vavoo ping2 token aliniyor...")
        vavoo = get_auth_signature()
        slog(f"Adim 2/5: Vavoo token={'ALINDI' if vavoo else 'BASARISIZ'}")

        # Adim 3: DB ve kanallari cek
        slog("Adim 3/5: DB baslatiliyor...")
        init_db()

        slog("Adim 3/5: Kanallar cekiliyor...")
        fetch_ok = fetch_vavoo_channels()
        slog(f"Adim 3/5: Kanal cekme={'BASARILI' if fetch_ok else 'BASARISIZ'}")

        # Adim 4: HLS linkleri
        slog("Adim 4/5: HLS linkleri cekiliyor...")
        fetch_hls_links()

        # Adim 5: Grup remap
        slog("Adim 5/5: Grup remap yapiliyor...")
        remap_groups()

        LOAD_TIME = time.time() - start
        DATA_READY = True
        slog(f"=== Tamamlandi! ({LOAD_TIME:.1f}s) ===")
    except Exception as e:
        STARTUP_ERROR = str(e)
        slog(f"!!! BASLANGIC HATASI: {e}")
        traceback.print_exc()


def main():
    slog("VxParser baslatiliyor...")
    threading.Thread(target=startup_sequence, daemon=True).start()
    import uvicorn
    from video import app
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
