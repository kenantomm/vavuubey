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
import asyncio
import sqlite3
import httpx

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
    # Combined schema: both old (lid auto-increment) and new (cid, country, clean_name) columns
    c.execute("""CREATE TABLE IF NOT EXISTS channels (
        lid INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        grp TEXT DEFAULT '',
        cid INTEGER DEFAULT 0,
        logo TEXT DEFAULT '',
        url TEXT DEFAULT '',
        hls TEXT DEFAULT '',
        sort_order INTEGER DEFAULT 9999,
        country TEXT DEFAULT '',
        clean_name TEXT DEFAULT ''
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ch_cid ON channels(cid)")
    conn.commit()
    conn.close()
    state.slog("DB baslatildi: " + state.DB_PATH)


async def async_fetch_vavoo_channels():
    """Fetch channels from Vavoo live2/index API (async, with correct URL and headers)."""
    state.slog("Vavoo live2 cekiliyor (async)...")

    try:
        state.slog("DNS test: vavoo.to...")
        async with httpx.AsyncClient(timeout=10, verify=False, follow_redirects=True) as client:
            test = await client.get("https://vavoo.to/", headers={"User-Agent": "VAVOO/2.6"})
            state.slog(f"vavoo.to OK (status={test.status_code})")
    except Exception as e:
        state.slog(f"vavoo.to BASARISIZ: {e}")
        return []

    try:
        channels = await state.fetch_channels()
        if not channels or not isinstance(channels, list):
            state.slog("Gecersiz kanal listesi!")
            return []
        return channels
    except Exception as e:
        state.slog(f"Kanal cekme HATASI: {e}")
        return []


async def async_fetch_hls_links():
    """Fetch HLS links from MediaHubMX catalog with pagination (async)."""
    state.slog("HLS linkleri cekiliyor (async)...")
    sig = state.get_watchedsig()
    if not sig:
        state.slog("Lokke imzasi yok, HLS atlanacak")
        return 0

    total_updated = 0
    for group_name in ["Turkey", "Deutschland"]:
        try:
            items = await state.fetch_all_catalog(group_name)
            state.slog(f"{group_name} HLS: {len(items)} kayit")

            # Build lookup dict from DB channels: stream_id -> lid
            conn = sqlite3.connect(state.DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT lid, url, name, clean_name FROM channels")
            db_channels = c.fetchall()
            conn.close()

            id_lookup = {}
            for ch in db_channels:
                m = re.search(r'/play\d+/(\d+)\.m3u8', ch["url"] or "")
                if m:
                    sid = m.group(1)
                    id_lookup[sid] = ch["lid"]
                    # Also try shorter versions for partial matching
                    for l in range(len(sid), max(4, len(sid) - 8), -1):
                        id_lookup[sid[:l]] = ch["lid"]

            conn = sqlite3.connect(state.DB_PATH)
            c = conn.cursor()
            for item in items:
                cat_url = item.get("url", "")
                cat_name = item.get("name", "")
                if not cat_url:
                    continue

                # Try to match with our channels
                u = re.sub(r'.*/', '', cat_url)
                uid = u[:max(4, len(u) - 12)] if len(u) > 12 else u

                matched_lid = None

                # Match by stream ID
                if uid in id_lookup:
                    matched_lid = id_lookup[uid]

                # Match by full URL suffix
                if not matched_lid:
                    for sid, db_lid in id_lookup.items():
                        if sid in cat_url:
                            matched_lid = db_lid
                            break

                # Match by clean name
                if not matched_lid:
                    cat_clean = state.clean_name(cat_name)
                    for ch in db_channels:
                        ch_clean = ch["clean_name"] or state.clean_name(ch["name"])
                        if ch_clean == cat_clean:
                            matched_lid = ch["lid"]
                            break

                if matched_lid:
                    c.execute("UPDATE channels SET hls=? WHERE lid=?", (cat_url, matched_lid))
                    total_updated += 1

            conn.commit()
            conn.close()
        except Exception as e:
            state.slog(f"{group_name} HLS HATASI: {e}")

    state.slog(f"HLS: {total_updated} link guncellendi")
    return total_updated


def remap_groups():
    """Remap channels to groups using GROUP_ORDER and GROUP_RULES."""
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
    group_counts = {}
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


async def async_startup_core():
    """Core startup logic running in an event loop (async)."""
    state.slog("[1/5] Lokke imzasi...")
    lokke = state.get_watchedsig()
    state.slog(f"[1/5] Lokke={'OK' if lokke else 'BASARISIZ'}")

    state.slog("[2/5] Vavoo token (1 deneme)...")
    vavoo = state.get_auth_signature(force=True)
    state.slog(f"[2/5] Vavoo={'OK' if vavoo else 'BASARISIZ'}")

    state.slog("[3/5] DB + Kanallar (async)...")
    init_db()
    channels = await async_fetch_vavoo_channels()

    fetch_ok = False
    if channels:
        # Filter TR + DE channels
        tr_count = 0
        de_count = 0
        conn = sqlite3.connect(state.DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM channels")
        for ch in channels:
            country = state.detect_country(ch)
            if country not in ("TR", "DE", "BOTH"):
                continue

            name = ch.get("name", "Unknown")
            url = ch.get("url", "")
            logo = ch.get("logo", "")
            group = ch.get("group", "")
            grp = state.remap_group(name, group)
            ch_id = 0

            # Extract channel ID from URL pattern /play\d+/(\d+)\.m3u8
            m = re.search(r'/play\d+/(\d+)\.m3u8', url)
            if m:
                ch_id = int(m.group(1))
            if ch_id == 0:
                ch_id = abs(hash(name)) % 9999999

            final_country = country if country != "BOTH" else "TR"
            clean = state.clean_name(name)

            c.execute(
                "INSERT OR REPLACE INTO channels(lid,name,grp,cid,logo,url,hls,sort_order,country,clean_name) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ch_id, name, grp, 0, logo, url, "", 9999, final_country, clean)
            )
            if final_country == "TR":
                tr_count += 1
            else:
                de_count += 1

        conn.commit()
        conn.close()
        state.slog(f"Kanallar: {tr_count + de_count} (TR={tr_count}, DE={de_count})")
        fetch_ok = True
    else:
        state.slog("[3/5] Kanal listesi bos veya hata")

    if not fetch_ok:
        db_count = state.count_db_channels()
        state.slog(f"[3/5] DB'de {db_count} kanal mevcut")
        if db_count > 0:
            state.slog("[3/5] DB'den onceki veriler kullanilacak")
            fetch_ok = True

    state.slog("[4/5] HLS linkleri (async, paginated)...")
    if fetch_ok:
        await async_fetch_hls_links()
    else:
        state.slog("[4/5] Kanal yok, HLS atlanacak")

    state.slog("[5/5] Grup remap...")
    if fetch_ok:
        remap_groups()
    else:
        state.slog("[5/5] Kanal yok, remap atlanacak")

    return fetch_ok


def startup_sequence():
    """Main startup sequence. Resilient: sets DATA_READY=True even on partial failure."""
    global STARTUP_ERROR
    start = time.time()

    # Prevent concurrent startup/refresh
    if not state.STARTUP_LOCK.acquire(blocking=False):
        state.slog("startup_sequence zaten calisiyor, atlanacak")
        return False

    try:
        state.slog("=== VxParser Baslangic ===")
        state.slog(f"PORT={state.PORT} DB={state.DB_PATH}")

        # Run async core in event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            fetch_ok = loop.run_until_complete(async_startup_core())
        finally:
            loop.close()

        state.LOAD_TIME = time.time() - start
        state.DATA_READY = True
        state.STARTUP_DONE = True
        state.LAST_REFRESH = time.time()

        # Log final DB state
        final_count = state.count_db_channels()
        state.slog(f"=== TAMAM! ({state.LOAD_TIME:.1f}s, {final_count} kanal) ===")
        return True

    except Exception as e:
        state.STARTUP_ERROR = str(e)
        state.slog(f"!!! HATA: {e}")
        traceback.print_exc()

        # Even on error, check if DB has usable data
        db_count = state.count_db_channels()
        if db_count > 0:
            state.slog(f"Hata olmasina ragmen DB'de {db_count} kanal var, DATA_READY=True")
            state.DATA_READY = True
            state.LAST_REFRESH = time.time()
        else:
            state.slog("DB bos ve hata olustu, DATA_READY=True zorla acildi (bos playlist)")
            state.DATA_READY = True
        state.STARTUP_DONE = True
        return False
    finally:
        state.STARTUP_LOCK.release()


def startup_sequence_with_timeout(timeout=120):
    """Run startup_sequence with a hard timeout."""
    result = [None]

    def target():
        result[0] = startup_sequence()

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        state.slog(f"!!! startup_sequence {timeout}s zaman asimi! DATA_READY zorla aciliyor")
        db_count = state.count_db_channels()
        if db_count > 0:
            state.DATA_READY = True
            state.slog(f"Timeout sonrasi DB'den {db_count} kanal kullanilacak")
        else:
            state.DATA_READY = True
            state.slog("DB bos ama DATA_READY=True zorla acildi (bos playlist)")
        state.STARTUP_DONE = True
        state.STARTUP_ERROR = f"Startup timed out after {timeout}s"


def periodic_refresh():
    """Background thread that re-fetches channels every REFRESH_INTERVAL."""
    while True:
        time.sleep(state.REFRESH_INTERVAL)
        state.slog(f"=== Periodik refresh ({state.REFRESH_INTERVAL}s arayla) ===")
        try:
            state.STARTUP_DONE = False
            state.STARTUP_ERROR = None
            startup_sequence()
        except Exception as e:
            state.slog(f"Periodik refresh HATASI: {e}")


def main():
    state.STARTUP_TIME = time.time()
    state.slog(">>> main() basladi <<<")

    # Start startup in background thread with timeout
    threading.Thread(target=startup_sequence_with_timeout, args=(120,), daemon=True).start()

    # Start periodic refresh in background
    threading.Thread(target=periodic_refresh, daemon=True).start()

    import uvicorn
    from video import app
    uvicorn.run(app, host="0.0.0.0", port=state.PORT)


if __name__ == "__main__":
    main()
