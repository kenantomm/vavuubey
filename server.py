"""
server.py - VxParser Render entry point
video.py'yi IMPORT ETMEZ - circular import yok.
"""
import os
import re
import time
import logging
import threading
import traceback

import state

state.PORT = int(os.environ.get("PORT", 10000))
state.DB_PATH = os.environ.get("DB_PATH", "/tmp/vxparser.db")
state.M3U_PATH = os.environ.get("M3U_PATH", "/tmp/playlist.m3u")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


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


def fetch_vavoo_channels():
    import requests
    state.slog("Vavoo live2 cekiliyor...")

    try:
        state.slog("DNS test: vavoo.to...")
        test = requests.get("https://www.vavoo.to/", timeout=10, verify=False, headers={"User-Agent": "VAVOO/2.6"})
        state.slog(f"vavoo.to OK (status={test.status_code})")
    except Exception as e:
        state.slog(f"vavoo.to BASARISIZ: {e}")
        return False

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
        if is_tr: tr_count += 1
        if is_de: de_count += 1

    conn.commit()
    conn.close()
    state.slog(f"Kanallar: {added} (TR={tr_count}, DE={de_count})")
    return True


def fetch_hls_links():
    import requests
    state.slog("HLS linkleri cekiliyor...")
    sig = state.get_watchedsig()
    if not sig:
        state.slog("Lokke imzasi yok, HLS atlanacak")
        return False

    headers = {"user-agent": "MediaHubMX/2", "accept": "application/json", "mediahubmx-signature": sig}
    updated = 0

    for group_name in ["Turkey", "Deutschland"]:
        try:
            data = {"language":"de","region":"AT","catalogId":"iptv","id":"iptv","adult":False,"sort":"name","clientVersion":"3.0.2","filter":{"group":group_name}}
            resp = requests.post("https://www.vavoo.to/mediahubmx-catalog.json", json=data, headers=headers, timeout=20, verify=False)
            items = resp.json().get("items", [])
            state.slog(f"{group_name} HLS: {len(items)} kayit")
            import sqlite3
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


def remap_groups():
    import sqlite3
    conn = sqlite3.connect(state.DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM categories")
    c.execute("UPDATE channels SET cid=0, grp=''")
    conn.commit()

    for idx, gn in enumerate(state.GROUP_ORDER):
        c.execute("INSERT OR IGNORE INTO categories(cid,name,sort_order) VALUES(?,?,?)", (idx+1, gn, idx+1))
    conn.commit()

    c.execute("SELECT lid, name FROM channels")
    updated = 0
    group_counts = {}  # gn -> count (for unique sort_order)
    for lid, name in c.fetchall():
        assigned = False
        for gi, gn in enumerate(state.GROUP_ORDER):
            for kw in state.GROUP_RULES.get(gn, []):
                if kw.lower() in name.lower():
                    group_counts[gn] = group_counts.get(gn, 0) + 1
                    c.execute("UPDATE channels SET cid=?,grp=?,sort_order=? WHERE lid=?", (gi+1, gn, group_counts[gn], lid))
                    updated += 1
                    assigned = True
                    break
            if assigned: break
        if not assigned:
            c.execute("SELECT cid FROM categories WHERE name='DE SONSTIGE'")
            row = c.fetchone()
            if row:
                group_counts["DE SONSTIGE"] = group_counts.get("DE SONSTIGE", 0) + 1
                c.execute("UPDATE channels SET cid=?,grp='DE SONSTIGE',sort_order=? WHERE lid=?", (row[0], group_counts["DE SONSTIGE"], lid))
    conn.commit()
    conn.close()
    state.slog(f"Grup remap: {updated} kanal")


def startup_sequence():
    start = time.time()
    try:
        state.slog("=== VxParser Baslangic ===")
        state.slog(f"PORT={state.PORT} DB={state.DB_PATH}")

        # Token'lari sadece 1 kez dene (baslangicta), sonra cooldown
        state.slog("[1/5] Lokke imzasi...")
        lokke = state.get_watchedsig()
        state.slog(f"[1/5] Lokke={'OK' if lokke else 'BASARISIZ'}")

        state.slog("[2/5] Vavoo token (1 deneme)...")
        vavoo = state.get_auth_signature(force=True)
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
    state.slog(">>> main() basladi <<<")
    threading.Thread(target=startup_sequence, daemon=True).start()
    import uvicorn
    from video import app
    uvicorn.run(app, host="0.0.0.0", port=state.PORT)


if __name__ == "__main__":
    main()
