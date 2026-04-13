"""
server.py - VxParser Render entry point
Kanallari ceker, DB olusturur, grup remap yapar.
video.py'yi IMPORT ETMEZ - circular import onlemek icin.
Hem server hem video state.py'yi kullanir.
"""
import os
import re
import time
import logging
import threading
import traceback

import state

# Ortam degiskenleri
state.PORT = int(os.environ.get("PORT", 10000))
state.DB_PATH = os.environ.get("DB_PATH", "/tmp/vxparser.db")
state.M3U_PATH = os.environ.get("M3U_PATH", "/tmp/playlist.m3u")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vxparser")


# ============================================================
# VERITABANI
# ============================================================
def init_db():
    import sqlite3
    conn = sqlite3.connect(state.DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS categories (cid INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, sort_order INTEGER DEFAULT 9999)")
    c.execute("CREATE TABLE IF NOT EXISTS channels (lid INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, grp TEXT DEFAULT '', cid INTEGER DEFAULT 0, logo TEXT DEFAULT '', url TEXT DEFAULT '', hls TEXT DEFAULT '', sort_order INTEGER DEFAULT 9999)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ch_cid ON channels(cid)")
    conn.commit()
    conn.close()
    state.slog("DB baslatildi: " + state.DB_PATH)


# ============================================================
# VAVOO KANAL CEKME (live2) - Tum URL'ler fallback
# ============================================================
def fetch_vavoo_channels():
    import requests
    state.slog("Vavoo live2 cekiliyor...")

    headers = {"User-Agent": state.CONFIG["CDN_USER_AGENT"]}

    # Tum live2 URL'lerini dene
    channel_list = None
    for live2_url in state.CONFIG["LIVE2_URLS"]:
        try:
            state.slog(f"  Deneniyor: {live2_url}")
            resp = requests.get(live2_url, headers=headers, timeout=30, verify=False)
            resp.raise_for_status()
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                channel_list = data
                state.slog(f"  OK: {live2_url} ({len(data)} kayit)")
                break
            else:
                state.slog(f"  Bos/gecersiz: {live2_url}")
        except Exception as e:
            state.slog(f"  HATA: {live2_url} -> {str(e)[:80]}")

    if not channel_list:
        state.slog("Tum live2 URL'leri BASARISIZ!")
        return False

    import sqlite3
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

        c.execute("INSERT OR REPLACE INTO channels(name,grp,cid,logo,url,hls,sort_order) VALUES(?,?,?,?,?,?,?)",
                  (name_clean, ch.get("group", ""), 0, logo, url, "", 9999))
        added += 1
        if is_tr:
            tr_count += 1
        if is_de:
            de_count += 1

    conn.commit()
    conn.close()
    state.slog(f"Kanallar: {added} (TR={tr_count}, DE={de_count})")
    return True


# ============================================================
# HLS LINKLERI CEK (catalog) - Tum BASE_URL fallback
# ============================================================
def fetch_hls_links():
    state.slog("HLS linkleri cekiliyor...")
    sig = state.get_watchedsig()
    if not sig:
        state.slog("addonSig yok, HLS atlanacak")
        return False

    updated = 0
    import sqlite3

    for group_name in ["Turkey", "Deutschland"]:
        state.slog(f"  {group_name} catalog cekiliyor...")
        items = state.fetch_catalog(sig, group_name)

        if items:
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
            state.slog(f"  {group_name}: {len(items)} HLS link guncellendi")
        else:
            state.slog(f"  {group_name}: catalog BOS (HLS link alinamadi)")

    state.slog(f"HLS toplam: {updated} link guncellendi")
    return updated > 0


# ============================================================
# GRUP REMAPPING
# ============================================================
def remap_groups():
    import sqlite3
    conn = sqlite3.connect(state.DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM categories")
    c.execute("UPDATE channels SET cid=0, grp='', sort_order=9999")
    conn.commit()

    for idx, gn in enumerate(state.GROUP_ORDER):
        c.execute("INSERT OR IGNORE INTO categories(cid,name,sort_order) VALUES(?,?,?)", (idx+1, gn, idx+1))
    conn.commit()

    c.execute("SELECT lid, name FROM channels")
    channels_list = c.fetchall()

    # Her gruptaki kanal sirasi icin sayac (benzersiz sort_order)
    group_counters = {}

    updated = 0
    for lid, name in channels_list:
        assigned = False
        for gi, gn in enumerate(state.GROUP_ORDER):
            for kw in state.GROUP_RULES.get(gn, []):
                if kw.lower() in name.lower():
                    group_counters[gn] = group_counters.get(gn, 0) + 1
                    ch_sort = (gi + 1) * 10000 + group_counters[gn]
                    c.execute("UPDATE channels SET cid=?,grp=?,sort_order=? WHERE lid=?", (gi+1, gn, ch_sort, lid))
                    updated += 1
                    assigned = True
                    break
            if assigned:
                break
        if not assigned:
            c.execute("SELECT cid FROM categories WHERE name='DE SONSTIGE'")
            row = c.fetchone()
            if row:
                group_counters["DE SONSTIGE"] = group_counters.get("DE SONSTIGE", 0) + 1
                ch_sort = 980000 + group_counters["DE SONSTIGE"]
                c.execute("UPDATE channels SET cid=?,grp='DE SONSTIGE',sort_order=? WHERE lid=?", (row[0], ch_sort, lid))
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
        state.slog(f"BASE_URLS: {state.CONFIG['BASE_URLS']}")
        state.slog(f"PING_URLS: {state.CONFIG['PING_URLS']}")
        state.slog(f"APP_VERSION: {state.CONFIG['APP_VERSION']} (v3.3.0 - Cache TTL + Group/Channel Mgmt)")

        state.slog("[1/5] addonSig (app/ping)...")
        lokke = state.get_watchedsig()
        state.slog(f"[1/5] addonSig={'OK' if lokke else 'BASARISIZ'}")

        state.slog("[2/5] Vavoo token (ping2)...")
        vavoo = state.get_auth_signature()
        state.slog(f"[2/5] Vavoo={'OK' if vavoo else 'BASARISIZ'}")

        state.slog("[3/5] DB + Kanallar...")
        init_db()
        ok = fetch_vavoo_channels()
        state.slog(f"[3/5] Kanallar={'OK' if ok else 'BASARISIZ'}")

        state.slog("[4/5] HLS linkleri (catalog)...")
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
    state.slog(">>> main() basladi <<<")
    threading.Thread(target=startup_sequence, daemon=True).start()

    # video.py'yi import et - circular import YOK cunku video server import etmiyor
    import uvicorn
    from video import app
    uvicorn.run(app, host="0.0.0.0", port=state.PORT)


if __name__ == "__main__":
    main()
