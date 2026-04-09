import sqlite3
import httpx
import time
import logging
import json
import re
import asyncio

log = logging.getLogger("vxparser")

DATA_READY = False
EPG_READY = False
STARTUP_LOGS = []
VAVOO_TOKEN = ""
WATCHED_SIG = ""
WATCHED_SIG_TIME = 0
# Use persistent storage on HuggingFace Spaces (/data/ is persistent)
# Fallback to /tmp/ if /data/ is not writable
import os
_PERSISTENT_DIR = "/data"
_DB_PATH_SET = False
DB_PATH = "/tmp/vxparser.db"
OVERRIDE_JSON_PATH = "/tmp/vxparser-overrides.json"
RESOLVE_CACHE = {}
SIG_REFRESH_INTERVAL = 1800
SELF_PING_INTERVAL = 240

def add_log(msg):
    log.info(msg)
    STARTUP_LOGS.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    if len(STARTUP_LOGS) > 200:
        STARTUP_LOGS.pop(0)

# Group ordering (user's desired order)
GROUP_ORDER = {
    'TR ULUSAL': 1, 'TR SPOR': 2, 'TR SINEMA': 3, 'TR SINEMA VOD': 4,
    'TR DIZI': 5, 'TR 7/24 DIZI': 6, 'TR BELGESEL': 7, 'TR COCUK': 8,
    'TR MUZIK': 9, 'TR HABER': 10, 'TR DINI': 11, 'TR YEREL': 12,
    'TR RADYO': 13, 'TR 4K': 14, 'TR 8K': 15, 'TR RAW': 16,
    'DE VOLLPROGRAMM': 17, 'DE NACHRICHTEN': 18, 'DE DOKU': 19,
    'DE KINDER': 20, 'DE FILM': 21, 'DE MUSIK': 22,
    'DE SPORT': 23, 'DE SONSTIGE': 24,
}

# Channel ordering WITHIN each group (prefixes in desired order)
CHANNEL_ORDER = {
    'TR ULUSAL': [
        'TRT 1', 'TRT 2', 'TRT TURK', 'TRT AVAZ', 'TRT 4K',
        'ATV', 'SHOW TV', 'STAR TV', 'KANAL D', 'FOX TV',
        'TV8', 'TV 8', 'TEVE 2', 'BEYAZ TV', 'A2',
        'KANAL 7', '360', 'EURO STAR', 'EURO D',
        'TV 8 INT', 'KANAL 7 AVRUPA', 'TGRT EU',
    ],
    'TR SPOR': [
        'BEIN SPORTS HABER', 'BEIN SPORTS 4K', 'BEIN SPORTS 1',
        'BEIN SPORTS 2', 'BEIN SPORTS 3', 'BEIN SPORTS 4', 'BEIN SPORTS 5',
        'BEIN SPORTS MAX', 'S SPORT', 'EXXEN', 'TIVIBU SPOR', 'SPOR SMART',
        'EUROSPORT', 'A SPOR', 'TRT SPOR', 'SPORTS TV',
        'NBA TV', 'FIGHT BOX', 'EDGE SPORT', 'TRACE SPORT',
        'FB TV', 'GS TV', 'TJK TV', 'TAY TV',
    ],
    'TR SINEMA': [
        'BEIN MOVIES', 'BEIN MOVIE', 'MOVIE SMART', 'SINEMA TV',
        'FILMBOX', 'BLU TV PLAY', 'EPIC DRAMA',
    ],
    'TR SINEMA VOD': [
        'ENO AKSIYON', 'ENO KADIR', 'ENO ZEKI', 'ENO KOMEDI', 'ENO TURK',
        'ENO VIZYON', 'ENOFLIX', 'FIBERBOX', 'MARVEL', 'PRIMEBOX',
        'SINEMAX', 'GOOGLE TV', 'GOOG LE TV', 'TURKLIVE',
        'UNI BOX OFFICE', 'VIZYONTV', 'YESILCAM BOX',
        'KEMAL SUNAL', 'KADIR INANIR', 'ZEKI METIN',
        'METIN AKPINAR', 'SENER SEN', 'CUNEYT ARKIN',
        'TARIK AKAN', 'ILYAS SALMAN', 'YILMAZ GUNEY',
        'HALIT AKCATEPE', 'MUNIR', 'SADRİ', 'GULDUR GULDUR',
    ],
    'TR DIZI': [
        'FX HD', 'FOX CRIME', 'BEIN SERIES', 'DIZI SMART',
    ],
    'TR BELGESEL': [
        'BEIN IZ', 'BEIN GURME', 'BEIN HOME', 'HISTORY',
        'DISCOVERY SCIENCE', 'DISCOVERY CHANNEL', 'DISCOVERY',
        'NATIONAL GEOGRAPHIC', 'NAT GEO', 'DA VINCI',
        'VIASAT EXPLORE', 'VIASAT HISTORY', 'BBC EARTH',
        'HABITAT TV', 'TARIH TV', 'CHASSE', 'ANIMAUX',
        'DOCUBOX', 'LOVE NATURE', 'TRT BELGESEL',
        'TLC', 'DMAX', 'CIFTCI TV', 'YABAN TV',
        'TGRT BELGESEL', 'AV TV', 'FASHION', 'FAST FUN',
        'TARIM', 'STINGRAY',
    ],
    'TR COCUK': [
        'TRT COCUK', 'SMART COCUK', 'NICKELODEON', 'NICK JR',
        'MINIKA COCUK', 'MINIKA GO', 'MOOUNBUG', 'DISNEY JUNIOR',
        'CBEEBIES', 'CARTOON NETWORK', 'CARTOONITO', 'BABY TV',
        'DA VINCI KIDS', 'DUCK TV', 'TRT DIYANET COCUK',
        'MASAL TV', 'SEVIMLI DOSTLAR', 'HEIDI',
        'KONUSAN TOM', 'ELIF', 'AKILLI TAVSAN',
        'BIZ IKIMIZ', 'BIZ IMIZ', 'BULMACA',
        'SIRINLER', 'ITFAIYECI SAM', 'DINOTRUX', 'JOHNNY TEST',
        'OSCAR', 'DIDIBO', 'PJ MASKELILER', 'ROBOCAR POLI',
        'KUKULI', 'CANIM KARDESIM', 'DORU', 'CILLE',
        'EGE', 'GOKKUSAGI', 'IBI', 'ASLAN', 'KARE',
        'KELOGlAN', 'KOYUN SHAUN', 'PAW PATROL',
        'COK YASA', 'ANGRY BIRDS', 'HAPSUU', 'KOSTEBEKGILLER',
        'HEZARFEN', 'KUKLALI', 'KUZUCUK', 'MAYSA',
        'MIGHTY EXPRESS', 'OLSAYDIM', 'PINKY MALINKY', 'PIRIL',
        'RAFADAN TAYFA', 'SU ELCILERI', 'SUNGER BOB',
        'PATRON BEBEK', 'NILOYA', 'KARDESIM OZI', 'PEPEE',
        'KUCUK OTOBUS', 'LEYLEK KARDES', 'CICIKI',
        'SONIC BOOM', 'MY LITTLE PONY', 'LARVA', 'BARBIE',
        'POLLY POCKET', 'ALVIN', 'LOLI ROCK', 'PAC-MAN',
        'REDKIT', 'DIGITAL TAYFA', 'ARI MAYA',
    ],
    'TR MUZIK': [
        'NET MUZIK', 'KRAL POP', 'POWER TV', 'POWER TURK',
        'NUMBER 1', 'NUMBER1', 'DREAM TURK', 'MILYON TV',
        'TRT MUZIK', 'TRACE URBAN', 'TATLISES',
    ],
    'TR HABER': [
        '24 HD', 'A HABER', 'A NEWS', 'A PARA',
        'AKIT TV', 'BBN TURK', 'BENGUTURK', 'BLOOMBERG HT',
        'CADDE TV', 'CNN TURK', 'EKOTURK', 'FLASH HABER',
        'HABER GLOBAL', 'HABERTURK', 'HALK TV',
        'IBB TV', 'KRT TV', 'LIDER HABER',
        'NTV', 'SZC TV', 'SOZCU', 'SÖZCÜ',
        'TBMM', 'TELE1', 'TELE 1',
        'TGRT HABER', 'TRT HABER', 'TURKHABER',
        'TV100', 'TVNET', 'ULKE TV', 'ULUSAL KANAL',
    ],
    'TR DINI': [
        'TRT DIYANET', 'DOST TV', 'KABE TV', 'LALEGUL', 'LALEGÜL',
        'REHBER TV', 'SEMERKAND', 'MELTEM TV',
        'MEDINE TV', 'MESAJ TV', 'DIYAR TV', 'BERAT TV', 'HZ YUSUF',
    ],
    'TR YEREL': [
        'MALATYA', 'AKSU TV', 'AS TV', 'BLT TURK', 'BRT',
        'CAY TV', 'DRT', 'PAMUKKALE', 'DEHA TV',
        'EDESSA', 'ER TV', 'GUNEYDOGU', 'HRT AKDENIZ',
        'KANAL 3', 'KANAL 32', 'KANAL 33', 'KANAL 42',
        'KANAL 43', 'KANAL FIRAT', 'KANAL 23',
        'KANAL T', 'KANAL URFA', 'KANAL V',
        'ADA TV', 'KIBRIS', 'KON TV', 'KOZA TV',
        'MERCAN TV', 'ON6', 'ALTAS TV',
        'RUMELI', 'SILA TV', 'SIM TV', 'TEK RUMELI',
        'TON TV', 'TURKMENELI', 'HUNAT TV',
        'TV 41', 'TV 52', 'TV A', 'OLAY TURK',
        'TVDEN', 'VIZYON 58', 'YENI KOCAELI', 'SINOP YILDIZ',
        'AKILLI TV', 'ANADOLU DERNEK', 'BEYKENT TV',
        'CAN TV', 'CEM TV', 'EGE TV', 'KADIRGA TV',
        'KANAL AVRUPA', 'KANAL B', 'TEMPO TV',
        'TV 4', 'TV 5', 'UCANKUS', 'VATAN TV',
        'VIYANA TV', 'YOL TV', 'ON4 TV', 'MAVI KARADENIZ',
    ],
    'DE VOLLPROGRAMM': [
        'DAS ERSTE', 'ZDF', 'ZDF INFO', 'ZDFNEO', 'ZDF NEO',
        '3SAT', 'PHOENIX', 'KIKA', 'ONE', 'ONE HD',
        'WDR', 'NDR', 'MDR', 'SWR', 'RBB',
        'ARTE', 'PROSIEBEN', 'PROSIEBEN MAXX',
        'SAT.1', 'SAT 1', 'RTL', 'RTL2', 'SUPER RTL',
        'VOX', 'KABEL 1', 'KABEL1', 'KABEL EINS',
        'TELE 5', 'SIXX',
    ],
    'DE NACHRICHTEN': [
        'N-TV', 'NTV', 'N24', 'WELT', 'TAGESSCHAU', 'EINFACH NACHRICHTEN',
    ],
    'DE DOKU': [
        'DMAX', 'N24 DOKU', 'SPIEGEL TV', 'DISCOVERY',
        'NAT GEO', 'NATIONAL GEO', 'HISTORY', 'TLC', 'LOVE NATURE',
    ],
    'DE KINDER': [
        'KINDER', 'TOGGO', 'JUNIOR', 'NICKELODEON', 'NICK',
        'NICKTOONS', 'CARTOON NETWORK', 'BOOMERANG',
        'DISNEY', 'CBEEBIES', 'CARTOONITO', 'BABY TV',
        'FIX UND FOXI',
    ],
    'DE FILM': [
        'SKY CINEMA', 'SKY HITS', 'SKY ACTION', 'SKY ATLANTIC',
        'SKY KRIMI', 'SKY ONE', '13TH STREET',
        'TNT SERIE', 'TNT FILM', 'TNT COMEDY',
        'AXN', 'HEIMATKANAL', 'ROMANCE TV', 'SONY CHANNEL',
        'SYFY', 'COMEDY CENTRAL', 'CLASSICA', 'ANIXE',
    ],
    'DE MUSIK': [
        'VIVA', 'DELUXE', 'MTV',
    ],
    'DE SPORT': [
        'EUROSPORT 1', 'EUROSPORT 2', 'EUROSPORT',
        'SKY SPORT', 'SKY BUNDESLIGA',
        'DAZN 1', 'DAZN 2', 'DAZN',
        'SPORT1', 'SPORTDIGITAL', 'MOTORVISION',
    ],
}


def init_db():
    global DB_PATH, OVERRIDE_JSON_PATH, _DB_PATH_SET
    if not _DB_PATH_SET:
        if os.path.isdir(_PERSISTENT_DIR) and os.access(_PERSISTENT_DIR, os.W_OK):
            DB_PATH = os.path.join(_PERSISTENT_DIR, "vxparser.db")
            OVERRIDE_JSON_PATH = os.path.join(_PERSISTENT_DIR, "vxparser-overrides.json")
            add_log(f"Kalici depolama: /data/ (override'lar korunacak)")
        else:
            DB_PATH = "/tmp/vxparser.db"
            OVERRIDE_JSON_PATH = "/tmp/vxparser-overrides.json"
            add_log(f"UYARI: /data/ yazilabilir degil, gecici depolama kullaniliyor")
            add_log(f"  -> Override'lar restartta silinebilir! JSON export/import kullanin.")
        _DB_PATH_SET = True
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY,
        name TEXT,
        url TEXT,
        hls TEXT DEFAULT '',
        grp TEXT DEFAULT '',
        country TEXT DEFAULT '',
        logo TEXT DEFAULT '',
        tvg_id TEXT DEFAULT '',
        picon TEXT DEFAULT '',
        sort_order INTEGER DEFAULT 9999,
        grp_order INTEGER DEFAULT 99
    )""")
    # Override table for manual group assignments
    c.execute("""CREATE TABLE IF NOT EXISTS channel_overrides (
        channel_name TEXT PRIMARY KEY,
        target_group TEXT NOT NULL
    )""")
    # Migration: ensure columns exist (for old databases)
    try:
        cols = [row[1] for row in c.execute("PRAGMA table_info(channels)").fetchall()]
        if "tvg_id" not in cols:
            c.execute("ALTER TABLE channels ADD COLUMN tvg_id TEXT DEFAULT ''")
        if "picon" not in cols:
            c.execute("ALTER TABLE channels ADD COLUMN picon TEXT DEFAULT ''")
        if "sort_order" not in cols:
            c.execute("ALTER TABLE channels ADD COLUMN sort_order INTEGER DEFAULT 9999")
        if "grp_order" not in cols:
            c.execute("ALTER TABLE channels ADD COLUMN grp_order INTEGER DEFAULT 99")
    except Exception:
        pass
    conn.commit()
    conn.close()
    # Load override cache into memory (from DB first, then merge with JSON backup)
    load_overrides_cache()
    # Also load from JSON backup for persistence across restarts
    load_overrides_from_json()

def get_channel(ch_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM channels WHERE id = ?", (ch_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_channels(ordered=True):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if ordered:
        c.execute("SELECT * FROM channels ORDER BY grp_order, sort_order, name")
    else:
        c.execute("SELECT * FROM channels")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def compute_sort_order(name, grp):
    """Compute sort_order for a channel based on user's desired ordering within each group."""
    n = name.upper().strip()
    order_list = CHANNEL_ORDER.get(grp, [])
    if not order_list:
        return 9999
    # Try exact match first (normalized)
    norm = clean_name(n)
    for idx, prefix in enumerate(order_list):
        if norm == prefix:
            return idx + 1
    # Try prefix match
    for idx, prefix in enumerate(order_list):
        if norm.startswith(prefix) or n.startswith(prefix):
            return idx + 1
    # Try contains match (weaker)
    for idx, prefix in enumerate(order_list):
        if prefix in norm:
            return idx + 1
    # Unknown channel in group -> sort alphabetically at end
    return 9999 + sum(ord(c) for c in n[:5])

# ===== OVERRIDE CACHE (in-memory for fast lookup) =====
OVERRIDE_CACHE = {}

def load_overrides_cache():
    global OVERRIDE_CACHE
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT channel_name, target_group FROM channel_overrides")
        OVERRIDE_CACHE = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
        if OVERRIDE_CACHE:
            add_log(f"Override cache: {len(OVERRIDE_CACHE)} kanal yuklendi")
    except Exception:
        OVERRIDE_CACHE = {}

def get_override(channel_name):
    return OVERRIDE_CACHE.get(channel_name.upper().strip())

def save_overrides_to_json():
    """Backup overrides to JSON file for persistence across restarts"""
    try:
        with open(OVERRIDE_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(OVERRIDE_CACHE, f, indent=2, ensure_ascii=False)
    except Exception as e:
        add_log(f"Override JSON kayit hatasi: {e}")

def load_overrides_from_json():
    """Load overrides from JSON backup file"""
    global OVERRIDE_CACHE
    try:
        if os.path.exists(OVERRIDE_JSON_PATH):
            with open(OVERRIDE_JSON_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    OVERRIDE_CACHE.update(data)
                    add_log(f"Override JSON'dan {len(data)} kayit yuklendi")
    except Exception as e:
        add_log(f"Override JSON yukleme hatasi: {e}")

def set_override(channel_name, target_group):
    """Set a single override (with DB write + JSON backup). For bulk, use batch_set_overrides()."""
    key = channel_name.upper().strip()
    OVERRIDE_CACHE[key] = target_group
    sort_ord = compute_sort_order(channel_name, target_group)
    grp_ord = GROUP_ORDER.get(target_group, 99)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO channel_overrides (channel_name, target_group) VALUES (?, ?)", (key, target_group))
    c.execute("UPDATE channels SET grp = ?, sort_order = ?, grp_order = ? WHERE UPPER(name) = ?", (target_group, sort_ord, grp_ord, key))
    conn.commit()
    conn.close()
    save_overrides_to_json()

def batch_set_overrides(overrides_dict):
    """Bulk set multiple overrides in a single DB transaction + single JSON write. FAST."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    count = 0
    for name, group in overrides_dict.items():
        if not name or not group:
            continue
        key = name.upper().strip()
        OVERRIDE_CACHE[key] = group
        sort_ord = compute_sort_order(name, group)
        grp_ord = GROUP_ORDER.get(group, 99)
        c.execute("INSERT OR REPLACE INTO channel_overrides (channel_name, target_group) VALUES (?, ?)", (key, group))
        c.execute("UPDATE channels SET grp = ?, sort_order = ?, grp_order = ? WHERE UPPER(name) = ?", (group, sort_ord, grp_ord, key))
        count += 1
    conn.commit()
    conn.close()
    # Single JSON write at the end
    save_overrides_to_json()
    return count

def delete_override(channel_name):
    key = channel_name.upper().strip()
    OVERRIDE_CACHE.pop(key, None)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM channel_overrides WHERE channel_name = ?", (key,))
    conn.commit()
    conn.close()
    save_overrides_to_json()

def delete_all_overrides():
    global OVERRIDE_CACHE
    OVERRIDE_CACHE = {}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM channel_overrides")
    conn.commit()
    conn.close()
    save_overrides_to_json()

def get_all_overrides():
    return dict(OVERRIDE_CACHE)

def import_overrides(overrides_dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    count = 0
    for name, group in overrides_dict.items():
        if not name or not group:
            continue
        key = name.upper().strip()
        OVERRIDE_CACHE[key] = group
        sort_ord = compute_sort_order(name, group)
        c.execute("INSERT OR REPLACE INTO channel_overrides (channel_name, target_group) VALUES (?, ?)", (key, group))
        c.execute("UPDATE channels SET grp = ?, sort_order = ? WHERE UPPER(name) = ?", (group, sort_ord, key))
        count += 1
    conn.commit()
    conn.close()
    save_overrides_to_json()
    return count

def update_channel_hls(ch_id, hls_url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE channels SET hls = ? WHERE id = ?", (hls_url, ch_id))
    conn.commit()
    conn.close()

# ===== LOKKE / MediaHubMX =====

async def refresh_watched_sig(force=False):
    global WATCHED_SIG, WATCHED_SIG_TIME
    try:
        now_ms = int(time.time()) * 1000
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
        }
        data = {
            "token": "", "reason": "boot", "locale": "de", "theme": "dark",
            "metadata": {
                "device": {"type": "desktop", "uniqueId": ""},
                "os": {"name": "win32", "version": "Windows 10", "abis": ["x64"], "host": "DESKTOP-VX"},
                "app": {"platform": "electron"},
                "version": {"package": "app.lokke.main", "binary": "1.0.19", "js": "1.0.19"}
            },
            "appFocusTime": 173, "playerActive": False, "playDuration": 0,
            "devMode": True, "hasAddon": True, "castConnected": False,
            "package": "app.lokke.main", "version": "1.0.19", "process": "app",
            "firstAppStart": now_ms, "lastAppStart": now_ms,
            "ipLocation": 0, "adblockEnabled": True,
            "proxy": {"supported": ["ss"], "engine": "cu", "enabled": False, "autoServer": True, "id": 0},
            "iap": {"supported": False}
        }
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.post("https://www.lokke.app/api/app/ping", json=data, headers=headers)
            result = r.json()
            sig = result.get("addonSig")
            if sig:
                WATCHED_SIG = sig
                WATCHED_SIG_TIME = time.time()
                add_log(f"Signature yenilendi ({len(sig)} char)")
                return sig
            else:
                add_log(f"Signature yenileme basarisiz: {json.dumps(result)[:200]}")
    except Exception as e:
        add_log(f"Signature yenileme hata: {e}")
    return ""

async def get_watched_sig():
    global WATCHED_SIG, WATCHED_SIG_TIME
    if WATCHED_SIG and (time.time() - WATCHED_SIG_TIME) < SIG_REFRESH_INTERVAL:
        return WATCHED_SIG
    return await refresh_watched_sig()

async def resolve_mediahubmx(url):
    global RESOLVE_CACHE
    sig = await get_watched_sig()
    if not sig:
        sig = await refresh_watched_sig(force=True)
    if not sig:
        add_log("Resolve: signature alinamadi!")
        return None
    try:
        headers = {
            "user-agent": "MediaHubMX/2", "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "mediahubmx-signature": sig,
        }
        data = {"language": "de", "region": "AT", "url": url, "clientVersion": "3.0.2"}
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.post("https://vavoo.to/mediahubmx-resolve.json", json=data, headers=headers)
            if r.status_code == 200:
                result = r.json()
                if isinstance(result, list) and len(result) > 0:
                    resolved_url = result[0].get("url", "")
                    if resolved_url:
                        return resolved_url
                add_log("Resolve bos response")
            elif r.status_code == 403:
                add_log("Resolve 403 -> signature yenileniyor...")
                new_sig = await refresh_watched_sig(force=True)
                if new_sig:
                    headers["mediahubmx-signature"] = new_sig
                    r2 = await client.post("https://vavoo.to/mediahubmx-resolve.json", json=data, headers=headers)
                    if r2.status_code == 200:
                        result = r2.json()
                        if isinstance(result, list) and len(result) > 0:
                            resolved_url = result[0].get("url", "")
                            if resolved_url:
                                add_log("Resolve 2. deneme BASARILI!")
                                return resolved_url
                    add_log(f"Resolve 2. deneme basarisiz: HTTP {r2.status_code}")
            else:
                add_log(f"Resolve HTTP {r.status_code}")
    except Exception as e:
        add_log(f"Resolve hata: {e}")
    return None

async def fetch_catalog(group, cursor=0):
    global WATCHED_SIG
    sig = await get_watched_sig()
    if not sig:
        sig = await refresh_watched_sig(force=True)
    if not sig:
        return {}
    try:
        headers = {
            "user-agent": "MediaHubMX/2", "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "mediahubmx-signature": sig,
        }
        data = {
            "language": "de", "region": "AT", "catalogId": "iptv", "id": "iptv",
            "adult": False, "search": "", "sort": "name",
            "filter": {"group": group}, "cursor": cursor, "clientVersion": "3.0.2"
        }
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            r = await client.post("https://vavoo.to/mediahubmx-catalog.json", json=data, headers=headers)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 403:
                new_sig = await refresh_watched_sig(force=True)
                if new_sig:
                    headers["mediahubmx-signature"] = new_sig
                    r2 = await client.post("https://vavoo.to/mediahubmx-catalog.json", json=data, headers=headers)
                    if r2.status_code == 200:
                        return r2.json()
            add_log(f"Catalog HTTP error for '{group}'")
    except Exception as e:
        add_log(f"Catalog hata ({group}): {e}")
    return {}

async def fetch_all_catalog(group_name):
    all_items = []
    cursor = 0
    while True:
        result = await fetch_catalog(group_name, cursor)
        if not result or not isinstance(result, dict):
            break
        items = result.get("items", [])
        if not items:
            break
        all_items.extend(items)
        next_cursor = result.get("nextCursor")
        if not next_cursor:
            break
        cursor = next_cursor
    return all_items

# ===== Signature auto-refresh background task =====

async def sig_refresh_loop():
    last_refresh = time.time()
    while True:
        await asyncio.sleep(60)
        now = time.time()
        if (now - last_refresh) >= SIG_REFRESH_INTERVAL:
            add_log("Otomatik signature yenileme...")
            await refresh_watched_sig(force=True)
            RESOLVE_CACHE.clear()
            add_log("Resolve cache temizlendi")
            last_refresh = now
        if (now - last_refresh) % SELF_PING_INTERVAL < 60:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get("http://localhost:7860/ping")
                    add_log(f"Self-ping: {r.status_code}")
            except Exception as e:
                add_log(f"Self-ping hata: {e}")

# ===== Channel Fetching =====

async def fetch_channels():
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        r = await client.get("https://vavoo.to/live2/index?output.json")
        data = r.json()
        channels = []
        if isinstance(data, list):
            channels = data
        elif isinstance(data, dict):
            for key in ["channels", "data", "items", "list", "results"]:
                if key in data and isinstance(data[key], list):
                    channels = data[key]
                    break
        add_log(f"API: {len(channels)} kanal")
        return channels

# ================================================================
# COUNTRY DETECTION
# ================================================================

def detect_country(ch):
    name = ch.get("name", "")
    group = ch.get("group", "")
    tvg_id = ch.get("tvg_id", "")
    n = name.upper()
    g = group.upper()
    t = tvg_id.lower() if tvg_id else ""

    is_tr = False
    is_de = False

    # 1. VAVOO GROUP - en guvenli
    if any(k in g for k in ["TURKEY", "TURKIYE"]):
        is_tr = True
    if any(k in g for k in ["GERMANY", "DEUTSCH"]):
        is_de = True

    # 2. PREFIX
    if n.startswith("TR:") or n.startswith("TR "):
        is_tr = True
    if n.startswith("DE:") or n.startswith("DE "):
        is_de = True

    # 3. tvg_id
    if t.endswith(".tr"):
        is_tr = True
    if t.endswith(".de"):
        is_de = True

    # 4. TR-ozgu isimler
    if not is_tr and not is_de:
        if any(k in n for k in ["TRT ", "SHOW TV", "STAR TV", "KANAL D",
            "ATV HD", "FOX TV", "TV8", "TEVE2", "BEYAZ TV",
            "KANAL 7", "A2 HD", "A SPOR", "TGRT ", "TJK ",
            "TIVIBU", "SPOR SMART", "EXXEN", "DIZI SMART",
            "SINEMA TV", "SINEMA ", "FILMBOX", "MOVIE SMART",
            "CIFTCI TV", "KEMAL SUNAL", "SEMERKAND", "LALEGUL",
            "DOST TV", "REHBER TV", "MASAL TV", "MINIKA",
            "PEPEE", "NET MUZIK", "KRAL POP", "KRAL TV",
            "POWER TURK", "DREAM TURK", "TATLISES",
            "TURKLIVE", "YESILCAM BOX", "VIZYONTV",
            "UNI BOX OFFICE", "FIBERBOX", "PRIMEBOX",
            "7/24 ", "GULDUR GULDUR", "KUKULI", "CICIKI",
            "RAFADAN TAYFA", "KOSTEBEKGILLER",
            "ULKE TV", "CINESTAR", "NOW HD",
            "EUROSPORT", "BEIN 1", "BEIN 2", "BEIN 3"]):
            is_tr = True

    # 5. DE-ozgu isimler
    if not is_tr and not is_de:
        if any(k in n for k in ["ARD ", "ARD HD", "ZDF", "DAS ERSTE",
            "WDR ", "NDR ", "MDR ", "SWR ", "RBB ",
            "PHOENIX", "3SAT", "KIKA", "ZDFNEO", "ZDFINFO",
            "PROSIEBEN", "SAT.1", "SAT 1", "RTL2", "SUPER RTL",
            "SIXX", "TELE 5", "ARTE ", "ORF ", "PULS 4",
            "SERVUS ", "SRF ", "N-TV", "N24 ", "WELT ",
            "SPIEGEL TV", "SKY CINEMA", "SKY SPORT", "SKY HITS",
            "SKY ACTION", "13TH STREET", "TNT SERIE", "TNT FILM",
            "DAZN", "SPORT1 ", "MOTORVISION", "VIVA ", "DELUXE "]):
            is_de = True

    if is_tr and is_de:
        return "BOTH"
    if is_tr:
        return "TR"
    if is_de:
        return "DE"
    return ""

# ================================================================
# GROUP REMAPPING - TR/DE ayrimli
#
# country == "TR"  -> only TR checks, default TR YEREL
# country == "DE"  -> only DE checks, default DE SONSTIGE
# country == "BOTH"-> try TR first, if no match fall through to DE
# ================================================================

def remap_group(name, original_group="", country=""):
    n = name.upper().strip()
    g = original_group.upper()

    # INFO tamamen sil
    if n in ("INFO", "INFO TV", "INFO HD"):
        return "__REMOVE__"

    # ===== MANUAL OVERRIDE CHECK (first priority) =====
    override = get_override(name)
    if override:
        return override

    ulusal_haber = ["HALK TV", "SOZCU", "SÖZCÜ", "SZC TV", "TELE1", "TELE 1"]

    # Check if it's a 4K/8K/RAW variant - store that info
    is_4k = any(k in n for k in [" 4K", " 8K", " UHD"])
    is_raw = " RAW" in n or " HEVC" in n or " H.265" in n or " H265" in n

    # ============================================================
    # TR KANALLARI (TR ve BOTH icin)
    # ============================================================
    if country in ("TR", "BOTH"):

        # --- TR HABER (once kontrol et - Halk TV, Sözcü, Tele1 vs.) ---
        if any(k in n for k in ["A HABER", "A NEWS", "A PARA",
            "AKIT TV", "BBN TURK", "BENGUTURK", "BLOOMBERG HT",
            "CADDE TV", "CNN TURK", "EKOTURK", "FLASH HABER",
            "HABER GLOBAL", "HABERTURK",
            "HALK TV", "SOZCU", "SÖZCÜ", "SZC TV",
            "IBB TV", "KRT TV", "LIDER HABER",
            "NTV", "TELE 1", "TELE1",
            "TGRT HABER", "TRT HABER", "TURKHABER",
            "TV100", "TVNET", "ULKE TV", "ULUSAL KANAL",
            "TBMM", "24 HD"]):
            return "TR HABER"

        # --- TR ULUSAL ---
        if any(k in n for k in ["TRT 1", "TRT 2", "TRT TURK", "TRT AVAZ", "TRT 4K",
            "ATV HD", "ATV HD+", "ATV AVRUPA",
            "SHOW TV", "SHOW TURK", "SHOW MAX",
            "STAR TV", "KANAL D", "FOX TV",
            "TV8", "TV 8", "TV 8,5", "TV 8.5",
            "TEVE 2", "BEYAZ TV", "A2 HD",
            "KANAL 7", "360 HD",
            "TGRT EU", "EURO STAR", "EURO D",
            "TV 8 INT", "TV8 INT", "KANAL 7 AVRUPA"]):
            return "TR ULUSAL"

        # --- TR SPOR ---
        # BEIN: catch all BEIN sport channels, but NOT BEIN MOVIES/SERIES/IZ/GURME/HOME
        is_bein_sport = ("BEIN" in n and not any(k in n for k in [
            "BEIN MOVIES", "BEIN MOVIE", "BEIN SERIES",
            "BEIN IZ", "BEIN GURME", "BEIN HOME"]))
        if is_bein_sport or any(k in n for k in [
            "S SPORT", "EXXEN", "EUROSPORT",
            "TIVIBU SPOR", "SPOR SMART",
            "A SPOR", "TRT SPOR", "SPORTS TV",
            "NBA TV", "FIGHT BOX", "EDGE SPORT", "TRACE SPORT",
            "FB TV", "GS TV", "TJK TV", "TAY TV"]):
            if is_4k:
                return "TR 4K"
            if is_raw:
                return "TR RAW"
            return "TR SPOR"

        # --- TR SINEMA ---
        if any(k in n for k in ["BEIN MOVIES", "BEIN MOVIE",
            "MOVIE SMART", "SINEMA", "FILMBOX",
            "BLU TV PLAY", "EPIC DRAMA",
            "CINESTAR", "NOW HD", "NOW TV"]):
            if is_4k:
                return "TR 4K"
            if is_raw:
                return "TR RAW"
            return "TR SINEMA"

        # --- TR SINEMA VOD ---
        if any(k in n for k in ["ENO ", "ENOFLIX", "FIBERBOX",
            "MARVEL STUDIOS", "PRIMEBOX", "SINEMAX",
            "GOOGLE TV", "GOOG LE TV", "TURKLIVE",
            "UNI BOX OFFICE", "VIZYONTV", "YESILCAM BOX",
            "KEMAL SUNAL", "KADIR INANIR", "KADİR İNANIR",
            "METIN AKPINAR", "ZEKI METIN", "ZEKİ METİN",
            "SENER SEN", "ŞENER ŞEN",
            "CUNEYT ARKIN", "CÜNEYT ARKIN",
            "TARIK AKAN", "ILYAS SALMAN", "YILMAZ GUNEY",
            "HALIT AKCATEPE", "HALİT AKÇATEPE",
            "MUNIR ÖZKUL", "MUNİR ÖZKUL",
            "SADRI ALISIK", "SADRİ ALİSİK",
            "GULDUR GULDUR", "GÜLDÜR GÜLDÜR"]):
            return "TR SINEMA VOD"

        # --- TR DIZI ---
        if any(k in n for k in ["FX HD", "FOX CRIME", "BEIN SERIES", "DIZI SMART"]):
            return "TR DIZI"

        # --- TR 7/24 DIZI ---
        if "7/24" in n:
            return "TR 7/24 DIZI"

        # --- TR BELGESEL ---
        if any(k in n for k in ["BEIN IZ", "BEIN GURME", "BEIN HOME",
            "ANIMAUX", "DOCUBOX", "LOVE NATURE",
            "HABITAT TV", "TARIH TV", "CHASSE", "STINGRAY",
            "FAST FUN", "FASHION HD", "TGRT BELGESEL",
            "VIASAT EXPLORE", "VIASAT HISTORY",
            "AV TV", "DA VINCI",
            "BBC EARTH", "YABAN TV", "CIFTCI TV",
            "HISTORY", "BELGESEL", "TRT BELGESEL"]):
            return "TR BELGESEL"
        if any(k in n for k in ["DISCOVERY", "NAT GEO", "NATIONAL GEO"]):
            return "TR BELGESEL"
        # DMAX TLC - TR context
        if country == "TR" and any(k in n for k in ["DMAX", "TLC"]):
            return "TR BELGESEL"

        # --- TR COCUK ---
        if any(k in n for k in ["TRT COCUK", "TRT DIYANET COCUK",
            "SMART COCUK", "MINIKA COCUK", "MINIKA GO",
            "MOOUNBUG", "DUCK TV", "DA VINCI KIDS",
            "MASAL TV", "SEVIMLI DOSTLAR", "HEIDI", "ARI MAYA",
            "REDKIT", "DIGITAL TAYFA",
            "AKILLI TAVSAN", "KUSUCUK",
            "PJ MASKELILER", "ROBOCAR POLI", "KUKULI",
            "CICIKI", "SONIC BOOM", "MY LITTLE PONY", "LARVA",
            "PAC-MAN", "NILOYA", "PEPEE", "BARBIE",
            "POLLY POCKET", "LOLI ROCK",
            "LEYLEK KARDES", "KARDESIM OZI",
            "COCUK", "CARTOON", "NICKELODEON", "NICK JR",
            "DISNEY JUNIOR", "CBEEBIES", "CARTOONITO", "BABY TV",
            "KONUSAN TOM", "BIZ IKIMIZ", "BIZ IMIZ",
            "SIRINLER", "ITFAIYECI SAM",
            "DINOTRUX", "JOHNNY TEST",
            "CANIM KARDESIM", "DORU", "CILLE",
            "KOYUN SHAUN", "PAW PATROL", "ANGRY BIRDS",
            "KELOGlAN", "KOSTEBEKGILLER",
            "MIGHTY EXPRESS", "PATRON BEBEK",
            "MAYSA", "PINKY MALINKY", "PIRIL",
            "RAFADAN TAYFA", "SU ELCILERI",
            "SUNGER BOB", "KUCUK OTOBUS",
            "ALVIN", "GOKKUSAGI", "IBI", "ASLAN", "BULMACA",
            "ELIF", "KARE", "EGE", "HEZARFEN", "OLSAYDIM"]):
            return "TR COCUK"

        # --- TR MUZIK ---
        if any(k in n for k in ["NET MUZIK", "KRAL POP", "KRAL TV",
            "POWER TURK", "NUMBER 1", "NUMBER1", "DREAM TURK",
            "MILYON TV", "TRT MUZIK", "TRACE URBAN", "TATLISES"]):
            return "TR MUZIK"
        if any(k in n for k in ["MUZIK", "MUSIK"]):
            return "TR MUZIK"
        if "POWER TV" in n:
            return "TR MUZIK"

        # --- TR DINI ---
        if any(k in n for k in ["TRT DIYANET", "KABE TV", "LALEGUL", "LALEGÜL",
            "REHBER TV", "SEMERKAND", "MELTEM TV",
            "MEDINE TV", "MESAJ TV", "DIYAR TV", "BERAT TV",
            "HZ YUSUF", "DOST TV", "DINI", "DINi", "DIYANET"]):
            return "TR DINI"

        # --- TR RADYO ---
        if "RADYO" in n or "RADIO" in n:
            return "TR RADYO"

        # --- TR YEREL ---
        if any(k in n for k in ["MALATYA", "KAHRAMANMARAS", "AKSU TV",
            "BURSA", "ISPARTA", "ZONGULDAK", "RIZE",
            "DENIZLI", "PAMUKKALE", "DEHA TV",
            "SANLIURFA", "ELAZIG",
            "KONYA", "MERSIN", "KUTAHYA",
            "KIBRIS", "ADANA", "ADIYAMAN",
            "ORDU", "ANTALYA", "CANAKKALE",
            "KAYSERI", "KOCAELI",
            "SIVAS", "KARADENIZ", "VIYANA",
            "BLT TURK", "BRT ", "BRTV", "CAY TV",
            "DRT ", "ER TV", "GUNEYDOGU", "HRT AKDENIZ",
            "KANAL 3 ", "KANAL 32", "KANAL 33",
            "KANAL 42", "KANAL 43", "KANAL FIRAT", "KANAL 23",
            "KANAL T ", "KANAL URFA", "KANAL V ",
            "ADA TV", "KIBRIS GENC", "KON TV", "KOZA TV",
            "MERCAN TV", "ON6 ", "ALTAS TV",
            "RUMELI TV", "SILA TV", "SIM TV", "TEK RUMELI",
            "TON TV", "TURKMENELI", "HUNAT TV",
            "TV 41", "TV 52", "TV A ", "OLAY TURK",
            "TVDEN", "VIZYON 58", "YENI KOCAELI", "SINOP YILDIZ"]):
            return "TR YEREL"
        if any(k in n for k in ["AKILLI TV", "ANADOLU DERNEK", "BEYKENT TV",
            "CAN TV", "CEM TV", "EGE TV", "KADIRGA TV",
            "KANAL AVRUPA", "KANAL B", "TEMPO TV",
            "TV 4 HD", "TV 5 HABER", "UCANKUS", "VATAN TV",
            "YOL TV", "ON4 TV", "MAVI KARADENIZ"]):
            return "TR YEREL"

        # --- TR ULUSAL fallback (broad TR keywords) ---
        # NOTE: EUROSPORT caught above in TR SPOR, so 'EURO' here only matches EURO STAR/D
        if any(k in n for k in ["TRT ", "ATV ", "SHOW ", "STAR ",
            "KANAL ", "FOX ", "TV8", "TEVE", "BEYAZ",
            "EURO ", "EUROSTAR", "EUROD"]):
            return "TR ULUSAL"

        # --- TR-only: default to TR YEREL ---
        if country == "TR":
            return "TR YEREL"

        # --- BOTH: no TR match -> fall through to DE checks below ---

    # ============================================================
    # ALMAN KANALLARI (DE icin, ve BOTH icin TR eslesmesi olmayanlar)
    # ============================================================
    if country in ("DE", "BOTH"):

        # DE SPORT - tam eslesme, genis keyword'leri daralt
        sport_exact = any(k in n for k in ["EUROSPORT 1", "EUROSPORT 2",
            "EUROSPORT", "SKY SPORT", "SKY BUNDESLIGA",
            "DAZN 1", "DAZN 2", "DAZN",
            "SPORT1 ", "SPORTDIGITAL", "MOTORVISION"])
        sport_safe = ("SPORT" in n) and not any(k in n for k in ["SPORTS", "SPORTH"])
        if sport_exact or sport_safe:
            return "DE SPORT"

        # DE FILM / SINEMA
        if any(k in n for k in ["SKY CINEMA", "13TH STREET",
            "TNT SERIE", "TNT FILM", "TNT COMEDY",
            "SKY HITS", "SKY ACTION", "SKY ATLANTIC",
            "SKY KRIMI", "SKY ONE",
            "AXN ", "CINEMA", "KINO "]):
            return "DE FILM"

        # DE VOLLPROGRAMM
        if any(k in n for k in ["ARD ", "ARD HD", "ZDF", "DAS ERSTE",
            "WDR ", "NDR ", "MDR ", "SWR ", "RBB ",
            "PHOENIX", "3SAT", "KIKA", "ZDFNEO", "ZDFINFO",
            "PROSIEBEN", "PROSIEBEN MAXX",
            "SAT.1", "SAT 1", "RTL2", "SUPER RTL",
            "SIXX", "TELE 5", "ARTE "]):
            return "DE VOLLPROGRAMM"
        if any(k in n for k in [" RTL ", " RTL+", " RTL HD", "VOX ", "VOX HD",
            "KABEL1", "KABEL EINS", "KABEL 1", "ONE HD"]):
            return "DE VOLLPROGRAMM"
        if n.startswith("RTL") or n.startswith("SAT.1") or n.startswith("SAT 1"):
            return "DE VOLLPROGRAMM"

        # DE NACHRICHTEN
        if any(k in n for k in ["NACHRICHTEN", "N-TV", "NTV ", "N24 ",
            "WELT ", "WELT", "TAGESSCHAU", "EINFACH NACHRICHTEN"]):
            # "SPIEGEL" in SPIEGEL TV - DOKU'ya gitsin
            if "N24 DOKU" not in n:
                return "DE NACHRICHTEN"
            # SPIEGEL TV without NACHRICHTEN
        if "SPIEGEL" in n and "WISSEN" not in n and "GESCHICHTE" not in n:
            # Spiegel TV without specific sub-category
            pass

        # DE DOKU
        if any(k in n for k in ["DOKU", "DOKUMENTATION",
            "DMAX", "D-MAX", "LOVE NATURE",
            "N24 DOKU", "SPIEGEL TV WISSEN", "SPIEGEL GESCHICHTE"]):
            return "DE DOKU"

        # DE KINDER
        if any(k in n for k in ["KINDER", "KIDS", "TOGGO", "JUNIOR",
            "CBEEBIES", "CARTOONITO", "BABY TV",
            "NICKELODEON", "CARTOON NETWORK",
            "BOOMERANG", "NICKTOONS",
            "DISNEY ", "DISNEY CHANNEL", "DISNEY JUNIOR"]):
            return "DE KINDER"

        # DE MUSIK
        if any(k in n for k in ["MUSIK", "VIVA", "DELUXE", "MTV"]):
            return "DE MUSIK"

        # DE SONSTIGE (Avusturya, İsvicre, diger)
        if any(k in n for k in ["ORF ", "PULS 4", "SERVUS ", "SRF ", "SWISS"]):
            return "DE SONSTIGE"

        # HEIMATKANAL, ROMANCE TV, SONY CHANNEL etc.
        if any(k in n for k in ["HEIMATKANAL", "ROMANCE TV", "SONY CHANNEL",
            "SYFY", "COMEDY CENTRAL", "CLASSICA", "ANIXE"]):
            return "DE SONSTIGE"

        # RTL CRIME, RTL LIVING, RTL PASSION -> DE FILM (closer to entertainment)
        if any(k in n for k in ["RTL CRIME", "RTL LIVING", "RTL PASSION"]):
            return "DE SONSTIGE"

        # NAT GEO, DISCOVERY -> DE DOKU for DE context
        if any(k in n for k in ["NAT GEO", "DISCOVERY", "HISTORY"]):
            return "DE DOKU"

        # DE default
        return "DE SONSTIGE"

    # Son cikis - country bos veya bilinmeyen
    return "TR YEREL"

def clean_name(name):
    n = name.upper()
    for remove in [" (1)", " (2)", " (3)", " (4)", " (5)", "(BACKUP)", "+",
                    " HEVC", " RAW", " SD", " FHD", " UHD", " 4K", " H265"]:
        n = n.replace(remove, "")
    n = re.sub(r'\([^)]*\)', '', n)
    n = n.strip()
    return n

# ================================================================
# STARTUP SEQUENCE
# ================================================================

async def startup_sequence():
    global DATA_READY
    add_log("=== VxParser Basliyor ===")
    init_db()
    add_log("Veritabani hazir")
    add_log("Lokke signature aliniyor...")
    sig = await refresh_watched_sig(force=True)
    if sig:
        add_log("Lokke signature BASARILI!")
    else:
        add_log("Lokke signature BASARISIZ")
    add_log("Kanallar cekiliyor...")
    try:
        channels = await fetch_channels()
    except Exception as e:
        add_log(f"Kanal hatasi: {e}")
        return
    if not channels:
        add_log("HATA: 0 kanal!")
        return
    add_log(f"Toplam {len(channels)} kanal")

    # EPG modulunu import et (tvg-id ve picon icin)
    try:
        import epg
        add_log("EPG modulu yuklendi")
    except ImportError:
        epg = None
        add_log("EPG modulu bulunamadi")

    filtered = []
    removed = 0
    for ch in channels:
        name = ch.get("name", "Unknown")
        group = ch.get("group", "")
        country = detect_country(ch)
        if country not in ("TR", "DE", "BOTH"):
            continue
        grp = remap_group(name, group, country=country)
        if grp == "__REMOVE__":
            removed += 1
            continue
        url = ch.get("url", "")
        final_country = country if country != "BOTH" else "TR"
        ch_id = 0
        m = re.search(r'/play\d+/(\d+)\.m3u8', url)
        if m:
            ch_id = int(m.group(1))
        if ch_id == 0:
            ch_id = abs(hash(name)) % 9999999

        # ---- Vavoo logo: make full URL from relative path ----
        vavoo_logo = ch.get("logo", "")
        if vavoo_logo:
            if vavoo_logo.startswith("/"):
                vavoo_logo = "https://vavoo.to" + vavoo_logo
            elif not vavoo_logo.startswith("http"):
                vavoo_logo = ""

        # ---- tvg_id: prefer Vavoo's own, fallback to our mapping ----
        vavoo_tvg_id = ch.get("tvg_id", "").strip() if ch.get("tvg_id") else ""
        tvg_id = vavoo_tvg_id
        if not tvg_id and epg:
            tvg_id = epg.get_tvg_id(name, final_country)

        # ---- picon: prefer Vavoo logo, fallback to our PICON_MAP ----
        picon = ""
        if vavoo_logo:
            picon = vavoo_logo
        elif epg:
            picon = epg.get_picon_url(name, "", final_country)

        filtered.append({
            "id": ch_id, "name": name, "url": url, "hls": "",
            "grp": grp, "country": final_country,
            "logo": vavoo_logo, "tvg_id": tvg_id, "picon": picon,
            "clean_name": clean_name(name)
        })

    tr_count = sum(1 for c in filtered if c["country"] == "TR")
    de_count = sum(1 for c in filtered if c["country"] == "DE")
    epg_count = sum(1 for c in filtered if c["tvg_id"])
    picon_count = sum(1 for c in filtered if c["picon"])
    grp_counts = {}
    for c in filtered:
        g = c["grp"]
        grp_counts[g] = grp_counts.get(g, 0) + 1
    grp_str = ", ".join(f"{g}={v}" for g, v in sorted(grp_counts.items()))
    add_log(f"Filtrelenmis: {len(filtered)} (TR={tr_count}, DE={de_count}), Silinen={removed}")
    add_log(f"Gruplar: {grp_str}")
    add_log(f"EPG ID: {epg_count} kanal, Picon: {picon_count} kanal")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM channels")
    for ch in filtered:
        sort_ord = compute_sort_order(ch["name"], ch["grp"])
        grp_ord = GROUP_ORDER.get(ch["grp"], 99)
        c.execute("INSERT OR REPLACE INTO channels (id,name,url,hls,grp,country,logo,tvg_id,picon,sort_order,grp_order) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ch["id"], ch["name"], ch["url"], ch["hls"], ch["grp"], ch["country"], ch["logo"], ch["tvg_id"], ch["picon"], sort_ord, grp_ord))
    conn.commit()
    conn.close()
    add_log(f"DB kaydedildi: {len(filtered)} kanal")

    if sig:
        add_log("MediaHubMX catalog cekiliyor...")
        try:
            id_lookup = {}
            for ch in filtered:
                m = re.search(r'/play\d+/(\d+)\.m3u8', ch["url"])
                if m:
                    sid = m.group(1)
                    id_lookup[sid] = ch["id"]
                    for l in range(len(sid), max(4, len(sid)-8), -1):
                        id_lookup[sid[:l]] = ch["id"]
            total_hls = 0
            for gn in ["Turkey", "Germany"]:
                add_log(f"Catalog: {gn}...")
                items = await fetch_all_catalog(gn)
                add_log(f"Catalog {gn}: {len(items)} item")
                for item in items:
                    cat_url = item.get("url", "")
                    cat_name = item.get("name", "")
                    if not cat_url:
                        continue
                    u = re.sub(r'.*/', '', cat_url)
                    uid = u[:max(4, len(u)-12)] if len(u) > 12 else u
                    matched_id = None
                    if uid in id_lookup:
                        matched_id = id_lookup[uid]
                    if not matched_id:
                        for sid, db_id in id_lookup.items():
                            if sid in cat_url:
                                matched_id = db_id
                                break
                    if not matched_id:
                        cat_clean = clean_name(cat_name)
                        for ch in filtered:
                            if ch["clean_name"] == cat_clean:
                                matched_id = ch["id"]
                                break
                    if matched_id:
                        update_channel_hls(matched_id, cat_url)
                        total_hls += 1
            add_log(f"HLS eslesme: {total_hls} kanal")
        except Exception as e:
            add_log(f"Catalog hatasi: {e}")

    # Load overrides from environment variable (persists across HF Spaces restarts)
    import os, base64, json as _json
    env_overrides = os.environ.get("VXPARSER_OVERRIDES", "")
    if env_overrides:
        try:
            env_data = _json.loads(base64.b64decode(env_overrides).decode("utf-8"))
            if isinstance(env_data, dict):
                cnt = import_overrides(env_data)
                add_log(f"Env override: {cnt} kanal yuklendi")
        except Exception as e:
            add_log(f"Env override hatasi: {e}")

    # EPG Build (background, non-blocking)
    if epg:
        try:
            add_log("EPG olusturuluyor...")
            await epg.build_full_epg(filtered)
        except Exception as e:
            add_log(f"EPG hatasi: {e}")

    DATA_READY = True
    add_log("=== VxParser HAZIR ===")
