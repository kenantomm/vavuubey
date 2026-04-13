"""
server.py - VxParser Render entry point
Kanallari ceker, DB olusturur, grup remap yapar.
video.py'yi IMPORT ETMEZ - circular import onlemek icin.
Hem server hem video state.py'yi kullanir.

v4.0.0 - Direct HLS URL destegi (Y0)
         Resolve hata yonetimi iyilestirmesi
         Catalog eslestirme log detaylandirma
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
# NAME NORMALIZATION - Catalog/Live2 isim eslestirme
# ============================================================
def normalize_ch_name(name):
    """Kanal ismini normalize et - catalog ve live2 eslestirme icin"""
    n = name or ""
    n = re.sub(r'[^\x00-\x7F]+', '', n)  # non-ASCII kaldir
    n = re.sub(r'\s+(HD|FHD|UHD|HEVC|H\.265|H265|4K|SD|RAW|HDR|DOLBY|AT|AUSTRIA|GERMANY|DEUTSCHLAND|DE|1080|720|S-ANHALT|SACHSEN|MATCH TIME)\b', '', n, flags=re.IGNORECASE)
    n = re.sub(r'\s*[\[\(][^\]\)]*[\]\)]\s*', ' ', n)  # [backup], (BACKUP) vs
    n = re.sub(r'\s*\+\s*', ' ', n)  # + kaldir
    n = re.sub(r'\s+(\\(BACKUP\\)|BACKUP)', '', n, flags=re.IGNORECASE)
    n = re.sub(r'\s+', ' ', n).strip()
    return n.lower()


# ============================================================
# HLS LINKLERI CEK (catalog) - Tum BASE_URL fallback
# v4.0: Direct HLS destegi, detayli log
# ============================================================
def fetch_hls_links():
    state.slog("HLS linkleri cekiliyor...")
    sig = state.get_watchedsig()
    if not sig:
        state.slog("addonSig yok, HLS atlanacak")
        return False

    import sqlite3
    total_updated = 0
    total_direct = 0

    catalog_groups = ["Turkey", "Germany"]

    for group_name in catalog_groups:
        state.slog(f"  {group_name} catalog cekiliyor...")
        items = state.fetch_catalog(sig, group_name)

        if not items:
            state.slog(f"  {group_name}: catalog BOS")
            continue

        state.slog(f"  {group_name}: {len(items)} catalog kayit")

        conn = sqlite3.connect(state.DB_PATH)
        c = conn.cursor()

        # 1. Adim: Catalog isimlerini normalize edip map olustur
        catalog_name_map = {}
        catalog_url_map = {}
        m3u8_count = 0

        for item in items:
            hls_url = item.get("url", "")
            raw_name = item.get("name", "")
            norm_name = normalize_ch_name(raw_name)
            if hls_url and norm_name and len(norm_name) >= 2:
                if norm_name not in catalog_name_map:
                    catalog_name_map[norm_name] = hls_url
                # .m3u8 sayaci
                if ".m3u8" in hls_url.lower():
                    m3u8_count += 1
                # URL tabanli matching icin
                u = re.sub(r'.*/', '', hls_url)
                if len(u) > 14:
                    uid = u[:len(u)-12]
                    if uid not in catalog_url_map:
                        catalog_url_map[uid] = hls_url

        state.slog(f"  {group_name}: {len(catalog_name_map)} normalize isim, {len(catalog_url_map)} URL uid, {m3u8_count} .m3u8")

        # 2. Adim: Tum kanallari normalize edip eslestir
        c.execute("SELECT lid, name, url FROM channels")
        channels = c.fetchall()
        matched_by_name = 0
        matched_by_url = 0

        for lid, ch_name, ch_url in channels:
            ch_norm = normalize_ch_name(ch_name)
            hls_url = None

            # A) Exact name match (normalized)
            if ch_norm and ch_norm in catalog_name_map:
                hls_url = catalog_name_map[ch_norm]
                matched_by_name += 1

            # B) URL tabanli match
            if not hls_url and ch_url:
                u = re.sub(r'.*/', '', ch_url)
                if len(u) > 14:
                    uid = u[:len(u)-12]
                    if uid and uid in catalog_url_map:
                        hls_url = catalog_url_map[uid]
                        matched_by_url += 1

            # C) Partial name match
            if not hls_url and ch_norm and len(ch_norm) >= 4:
                for cat_norm, cat_url in catalog_name_map.items():
                    if len(cat_norm) >= 4 and (cat_norm in ch_norm or ch_norm in cat_norm):
                        hls_url = cat_url
                        matched_by_name += 1
                        break

            if hls_url:
                c.execute("UPDATE channels SET hls=? WHERE lid=?", (hls_url, lid))
                total_updated += 1

        conn.commit()
        conn.close()
        state.slog(f"  {group_name}: isim={matched_by_name} url={matched_by_url} toplam={matched_by_name+matched_by_url} eslesti")

    # Direct HLS kontrol - ornek kanallar
    if total_updated > 0:
        conn = sqlite3.connect(state.DB_PATH)
        c = conn.cursor()
        c.execute("SELECT lid, name, hls FROM channels WHERE hls!='' AND hls IS NOT NULL LIMIT 5")
        samples = c.fetchall()
        conn.close()
        if samples:
            state.slog(f"  Ornek HLS URL'ler:")
            for s_lid, s_name, s_hls in samples:
                is_direct = state.check_direct_hls(s_hls)
                state.slog(f"    [{s_lid}] {s_name}: .m3u8={'EVET' if '.m3u8' in s_hls.lower() else 'HAYIR'} direct={'EVET' if is_direct else 'HAYIR'}")
                if is_direct:
                    total_direct += 1

    state.slog(f"HLS toplam: {total_updated} kanal guncellendi, {total_direct} direkt stream")
    return total_updated > 0


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
        state.slog(f"APP_VERSION: {state.CONFIG['APP_VERSION']} (v4.0.0 - Direct HLS + Resolve Fix)")

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

    import uvicorn
    from video import app
    uvicorn.run(app, host="0.0.0.0", port=state.PORT)


if __name__ == "__main__":
    main()
