"""
server.py - VxParser Render entry point
Kanallari ceker, token alir, DB olusturur, grup remap yapar.
"""
import os
import sys
import sqlite3
import json
import random
import time
import re
import logging
import threading
import traceback

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import state

# Port ve DB ayarlarini environment'dan al
state.PORT = int(os.environ.get("PORT", 10000))
state.DB_PATH = os.environ.get("DB_PATH", "/tmp/vxparser.db")
state.M3U_PATH = os.environ.get("M3U_PATH", "/tmp/playlist.m3u")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vxparser")


# ============================================================
# 1. VAVOO TOKEN (ping2)
# ============================================================
def get_auth_signature():
    if state._vavoo_sig and (time.time() - state._vavoo_sig_time) < 1800:
        return state._vavoo_sig

    state.slog("Vavoo Token (ping2) aliniyor...")
    headers = {"User-Agent": "VAVOO/2.6", "Accept": "application/json"}
    try:
        vec_req = requests.get("http://mastaaa1987.github.io/repo/veclist.json", headers=headers, timeout=10, verify=False)
        veclist = vec_req.json()["value"]
        state.slog(f"veclist: {len(veclist)} vec")

        sig = None
        for i in range(5):
            vec = {"vec": random.choice(veclist)}
            req = requests.post("https://www.vavoo.tv/api/box/ping2", data=vec, headers=headers, timeout=10, verify=False).json()
            if req.get("signed"):
                sig = req["signed"]
                break

        if sig:
            state._vavoo_sig = sig
            state._vavoo_sig_time = time.time()
            state.slog("Vavoo Token alindi!")
            return sig
        else:
            state.slog("Vavoo Token ALINAMADI (5 deneme)")
    except Exception as e:
        state.slog(f"Vavoo Token HATASI: {e}")
    return None


# ============================================================
# 2. LOKKE IMZA (app/ping)
# ============================================================
def get_watchedsig():
    if state._watched_sig and (time.time() - state._watched_sig_time) < 1800:
        return state._watched_sig

    state.slog("Lokke Imza (app/ping) aliniyor...")
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
            state._watched_sig = sig
            state._watched_sig_time = time.time()
            state.slog("Lokke Imzasi alindi!")
            return sig
        else:
            state.slog(f"Lokke cevap anahtarlari: {list(result.keys())}")
    except Exception as e:
        state.slog(f"Lokke HATASI: {e}")
    return None


# ============================================================
# 3. HLS RESOLVE
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
        log.error("Resolve hatasi: %s", e)
    return None


# ============================================================
# 4. CHANNEL RESOLVE (3 yontem)
# ============================================================
def resolve_channel(lid):
    conn = sqlite3.connect(state.DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM channels WHERE lid=?", (lid,))
    ch = c.fetchone()
    conn.close()
    if not ch:
        return None

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

    # Y3: Direkt
    return url


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
    conn = sqlite3.connect(state.DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS categories (cid INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, sort_order INTEGER DEFAULT 9999)")
    c.execute("CREATE TABLE IF NOT EXISTS channels (lid INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, grp TEXT DEFAULT '', cid INTEGER DEFAULT 0, logo TEXT DEFAULT '', url TEXT DEFAULT '', hls TEXT DEFAULT '', sort_order INTEGER DEFAULT 9999)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ch_cid ON channels(cid)")
    conn.commit()
    conn.close()
    state.slog("DB baslatildi: " + state.DB_PATH)


# ============================================================
# VAVOO KANAL CEKME
# ============================================================
def fetch_vavoo_channels():
    state.slog("Vavoo live2 cekiliyor...")

    # Baglanti testi
    try:
        state.slog("DNS/baglanti testi: vavoo.to...")
        test = requests.get("https://www.vavoo.to/", timeout=10, verify=False, headers={"User-Agent": "VAVOO/2.6"})
        state.slog(f"vavoo.to erisim OK (status={test.status_code})")
    except Exception as e:
        state.slog(f"vavoo.to erisim BASARISIZ: {e}")
        return False

    # Kanal listesi
    try:
        resp = requests.get("https://www.vavoo.to/live2/index?output=json", headers={"User-Agent": "VAVOO/2.6"}, timeout=30, verify=False)
        resp.raise_for_status()
        channel_list = resp.json()
        state.slog(f"Kanal listesi: {len(channel_list) if isinstance(channel_list, list) else 'hata'} kayit")
    except Exception as e:
        state.slog(f"Kanal cekme HATASI: {e}")
        return False

    if not channel_list or not isinstance(channel_list, list):
        state.slog("Gecersiz kanal listesi!")
        return False

    conn = sqlite3.connect(state.DB_PATH)
    c = conn.cursor()
    added = 0
    tr_count = 0
    de_count = 0

    for ch in channel_list:
        group_raw = ch.get("group", "").lower()
        name = ch.get("name", "")
        url = ch.get("url", "")
        logo = ch.get("logo", "")

        is_tr = any(x in group_raw for x in ["turkey", "turkish", "tr", "türk", "türkei"])
        is_de = any(x in group_raw for x in ["deutschland", "german", "deutsch", "austria", "österreich", "schweiz", "switzerland"])
        if not is_tr and not is_de:
            continue

        name_clean = re.sub(r"[^\x00-\x7F]+", "", name)
        if not name_clean:
            continue

        c.execute("INSERT OR REPLACE INTO channels(name,grp,cid,logo,url,hls,sort_order) VALUES(?,?,?,?,?,?,?)", (name_clean, ch.get("group", ""), 0, logo, url, "", 9999))
        added += 1
        if is_tr:
            tr_count += 1
        if is_de:
            de_count += 1

    conn.commit()
    conn.close()
    state.slog(f"Kanallar: {added} toplam (TR={tr_count}, DE={de_count})")
    return True


def fetch_hls_links():
    state.slog("HLS linkleri cekiliyor...")
    sig = get_watchedsig()
    if not sig:
        state.slog("Lokke imzasi yok, HLS atlanacak (Y2 yontemi yine de calisir)")
        return False

    headers = {"user-agent": "MediaHubMX/2", "accept": "application/json", "mediahubmx-signature": sig}
    updated = 0

    for group_name in ["Turkey", "Deutschland"]:
        try:
            data = {"language":"de","region":"AT","catalogId":"iptv","id":"iptv","adult":False,"sort":"name","clientVersion":"3.0.2","filter":{"group":group_name}}
            resp = requests.post("https://www.vavoo.to/mediahubmx-catalog.json", json=data, headers=headers, timeout=20, verify=False)
            items = resp.json().get("items", [])
            state.slog(f"{group_name} HLS: {len(items)} kayit")
            conn = sqlite3.connect(state.DB_PATH)
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
            state.slog(f"{group_name} HLS HATASI: {e}")

    state.slog(f"HLS: {updated} link guncellendi")
    return True


# ============================================================
# GRUP REMAPPING
# ============================================================
def remap_groups():
    conn = sqlite3.connect(state.DB_PATH)
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
    state.slog(f"Grup remap: {updated} kanal")


# ============================================================
# BASLANGIC
# ============================================================
def startup_sequence():
    start = time.time()
    try:
        state.slog("=== VxParser Baslangic ===")
        state.slog(f"PORT={state.PORT} DB={state.DB_PATH}")

        state.slog("[1/5] Lokke imzasi...")
        lokke = get_watchedsig()
        state.slog(f"[1/5] Lokke={'OK' if lokke else 'BASARISIZ'}")

        state.slog("[2/5] Vavoo token...")
        vavoo = get_auth_signature()
        state.slog(f"[2/5] Vavoo={'OK' if vavoo else 'BASARISIZ'}")

        state.slog("[3/5] DB + Kanallar...")
        init_db()
        ok = fetch_vavoo_channels()
        state.slog(f"[3/5] Kanallar={'OK' if ok else 'BASARISIZ'}")

        state.slog("[4/5] HLS linkleri...")
        fetch_hls_links()

        state.slog("[5/5] Grup remap...")
        remap_groups()

        state.LOAD_TIME = time.time() - start
        state.DATA_READY = True
        state.slog(f"=== TAMAM! ({state.LOAD_TIME:.1f}s) ===")
    except Exception as e:
        state.STARTUP_ERROR = str(e)
        state.slog(f"!!! HATA: {e}")
        traceback.print_exc()


def main():
    state.slog(">>> main() calistirildi <<<")
    threading.Thread(target=startup_sequence, daemon=True).start()
    import uvicorn
    from video import app
    uvicorn.run(app, host="0.0.0.0", port=state.PORT)


if __name__ == "__main__":
    main()
