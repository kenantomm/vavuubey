import sqlite3
import httpx
import time
import logging
import json
import re
import asyncio

log = logging.getLogger("vxparser")

DATA_READY = False
STARTUP_LOGS = []
VAVOO_TOKEN = ""
WATCHED_SIG = ""
WATCHED_SIG_TIME = 0
DB_PATH = "/tmp/vxparser.db"
RESOLVE_CACHE = {}
SIG_REFRESH_INTERVAL = 1800  # 30 dakikada bir signature yenile
SELF_PING_INTERVAL = 240    # 4 dakikada bir kendine ping at

def add_log(msg):
    log.info(msg)
    STARTUP_LOGS.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    if len(STARTUP_LOGS) > 100:
        STARTUP_LOGS.pop(0)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY, name TEXT, url TEXT, hls TEXT DEFAULT '', grp TEXT DEFAULT '', country TEXT DEFAULT '', logo TEXT DEFAULT '')")
    conn.commit()
    conn.close()

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
        c.execute("""SELECT * FROM channels ORDER BY CASE grp
            WHEN 'TR ULUSAL' THEN 1
            WHEN 'TR SPOR' THEN 2
            WHEN 'TR SINEMA' THEN 3
            WHEN 'TR SINEMA VOD' THEN 4
            WHEN 'TR DIZI' THEN 5
            WHEN 'TR 7/24 DIZI' THEN 6
            WHEN 'TR BELGESEL' THEN 7
            WHEN 'TR COCUK' THEN 8
            WHEN 'TR MUZIK' THEN 9
            WHEN 'TR HABER' THEN 10
            WHEN 'TR DINI' THEN 11
            WHEN 'TR YEREL' THEN 12
            WHEN 'TR RADYO' THEN 13
            WHEN 'DE VOLLPROGRAMM' THEN 14
            WHEN 'DE NACHRICHTEN' THEN 15
            WHEN 'DE DOKU' THEN 16
            WHEN 'DE KINDER' THEN 17
            WHEN 'DE FILM' THEN 18
            WHEN 'DE MUSIK' THEN 19
            WHEN 'DE SPORT' THEN 20
            WHEN 'DE SONSTIGE' THEN 21
            ELSE 99 END, name""")
    else:
        c.execute("SELECT * FROM channels")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def update_channel_hls(ch_id, hls_url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE channels SET hls = ? WHERE id = ?", (hls_url, ch_id))
    conn.commit()
    conn.close()

# ===== LOKKE / MediaHubMX =====

async def refresh_watched_sig(force=False):
    """Get FRESH mediahubmx-signature from Lokke (always calls API, ignores cache)"""
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
    """Get signature - use cache if fresh, otherwise refresh"""
    global WATCHED_SIG, WATCHED_SIG_TIME
    # If we have a sig and it's less than 30 min old, use it
    if WATCHED_SIG and (time.time() - WATCHED_SIG_TIME) < SIG_REFRESH_INTERVAL:
        return WATCHED_SIG
    # Otherwise refresh
    return await refresh_watched_sig()

async def resolve_mediahubmx(url):
    """Resolve a stream URL via MediaHubMX - auto-refreshes sig on 403"""
    global RESOLVE_CACHE
    sig = await get_watched_sig()
    if not sig:
        # Force refresh
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
                add_log(f"Resolve bos response")
            elif r.status_code == 403:
                # Signature expired! Force refresh and retry ONCE
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
                    add_log(f"Resolve 2. deneme de basarisiz: HTTP {r2.status_code}")
                else:
                    add_log("Resolve: signature yenilenemedi!")
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
    """Background task: refresh signature every 30 minutes + self-ping every 4 min"""
    last_refresh = time.time()
    while True:
        await asyncio.sleep(60)  # Her 60 saniyede bir kontrol et
        now = time.time()
        
        # 30 dakika gectiyse signature yenile
        if (now - last_refresh) >= SIG_REFRESH_INTERVAL:
            add_log("Otomatik signature yenileme...")
            await refresh_watched_sig(force=True)
            RESOLVE_CACHE.clear()
            add_log("Resolve cache temizlendi")
            last_refresh = now
        
        # 4 dakika gectiyse kendine ping at (keepalive)
        if (now - last_refresh) % SELF_PING_INTERVAL < 60:
            try:
                import httpx
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

def detect_country(ch):
    name = ch.get("name", "")
    group = ch.get("group", "")
    tvg_id = ch.get("tvg_id", "")
    n = name.upper()
    g = group.upper()
    t = tvg_id.lower() if tvg_id else ""
    is_tr = any(k in n for k in ["TR:", "TR ", "TURK", "4K TR", "FHD TR", "HD TR"])
    is_de = any(k in n for k in ["DE:", "DE ", "GERMAN", "4K DE", "FHD DE", "HD DE"])
    if any(k in g for k in ["TURKEY", "TURKIYE"]): is_tr = True
    if any(k in g for k in ["GERMANY", "DEUTSCH"]): is_de = True
    if t.endswith(".de"): is_de = True
    if t.endswith(".tr"): is_tr = True
    # TR-ozel kanallar (prefix yok ama TR'ye ozgu)
    if any(k in n for k in ["TRT ", "SHOW TV", "STAR TV", "KANAL D", "ATV ", "FOX TV",
        "TV8", "TEVE2", "BEYAZ TV", "KANAL 7", "A2 HD", "A SPOR",
        "TGRT ", "TJK ", "TIVIBU", "SPOR SMART", "EXXEN",
        "DIZI SMART", "SINEMA TV", "FILMBOX", "MOVIE SMART",
        "TARIM TURK", "CIFTCI TV", "KEMAL SUNAL",
        "SEMERKAND", "LALEGUL", "DOST TV", "REHBER TV",
        "MASAL TV", "MINIKA", "PEPEE", "BARBIE",
        "NET MUZIK", "KRAL POP", "KRAL TV",
        "POWER TV", "POWER TURK", "DREAM TURK", "TATLISES",
        "TURKLIVE", "YESILCAM BOX", "VIZYONTV",
        "UNI BOX OFFICE", "FIBERBOX", "PRIMEBOX",
        "7/24 ", "GULDUR GULDUR", "KUKULI", "CICIKI",
        "RAFADAN TAYFA", "KOSTEBEKGILLER"]):
        is_tr = True
    # DE-ozel kanallar (prefix yok ama Alman'ya ozgu)
    if any(k in n for k in ["ARD ", "ARD HD", "ZDF", "DAS ERSTE", "WDR ", "WDR HD",
        "NDR ", "MDR ", "SWR ", "RBB ", "PHOENIX", "3SAT",
        "KIKA", "ZDFNEO", "ZDFINFO",
        "PROSIEBEN", "SAT.1", "SAT 1", "RTL2",
        "SUPER RTL", "SIXX", "TELE 5", "ARTE ",
        "ORF ", "PULS 4", "SERVUS ", "SRF ", "SRF HD",
        "N-TV", "N24 ", "WELT ", "SPIEGEL TV",
        "SKY CINEMA", "SKY SPORT", "SKY HITS", "SKY ACTION",
        "13TH STREET", "TNT SERIE", "TNT FILM",
        "DAZN", "SPORT1 ", "MOTORVISION",
        "VIVA ", "DELUXE ", "TOGGO"]):
        is_de = True
    # Ortak kanallar: TR veya DE prefix'i varsa ona gore, yoksa group'a gore
    if any(k in n for k in ["EUROSPORT", "DISCOVERY ", "NICKELODEON",
        "CARTOON NETWORK", "CARTOONITO", "BABY TV", "BOOMERANG",
        "CBEEBIES", "JUNIOR ", "NAT GEO", "NATIONAL GEO",
        "DISNEY ", "HISTORY ", "AXN ", "FOX CRIME", "FX HD"]):
        if not is_tr and not is_de:
            # Prefix yoksa group'a bak
            if any(k in g for k in ["TURKEY", "TURKIYE"]):
                is_tr = True
            elif any(k in g for k in ["GERMANY", "DEUTSCH"]):
                is_de = True
            else:
                # Group da yoksa TR varsay (Vavoo TR agirlikli)
                is_tr = True
    if is_tr and is_de: return "BOTH"
    if is_tr: return "TR"
    if is_de: return "DE"
    return ""

# ===== Tamamen kaldirilacak kanallar =====
REMOVED_CHANNELS = {"INFO", "INFO TV", "INFO HD"}

# ===== ULUSAL'a girmemesi gereken kanallar =====
ULUSAL_EXCLUDE = ["HALK TV", "SÖZCÜ", "SZC TV", "TELE1", "TELE 1"]

def remap_group(name, original_group=""):
    n = name.upper().strip()
    g = original_group.upper()
    combined = n + " " + g

    # --- Tamamen kaldirilacak kanallar ---
    if n in REMOVED_CHANNELS:
        return "__REMOVE__"

    # --- TR SPOR (once kontrol et - beIN, S Sport, Exxen vb.) ---
    if any(k in n for k in ["BEIN SPORTS", "BEIN SPORT", "S SPORT",
        "EXXEN SPORT", "EXXEN TV",
        "TIVIBU SPOR", "SPOR SMART", "EUROSPORT",
        "A SPOR", "TRT SPOR", "SPORTS TV",
        "NBA TV", "FIGHT BOX", "EDGE SPORT", "TRACE SPORT",
        "FB TV", "GS TV", "TJK TV", "TAY TV"]):
        return "TR SPOR"

    # --- TR SINEMA ---
    if any(k in n for k in ["BEIN MOVIES", "BEIN MOVIE",
        "MOVIE SMART", "SINEMA TV", "FILMBOX",
        "BLU TV PLAY", "EPIC DRAMA"]):
        return "TR SINEMA"

    # --- TR SINEMA VOD ---
    if any(k in n for k in ["ENO ", "ENOFLIX", "ENOAKSIYON",
        "FIBERBOX", "MARVEL STUDIOS", "PRIMEBOX", "SINEMAX",
        "GOOGLE TV", "GOOGLE TV", "TURKLIVE",
        "UNI BOX OFFICE", "VIZYONTV", "YESILCAM BOX",
        "KEMAL SUNAL", "KADIR INANIR", "KADİR İNANIR",
        "METIN AKPINAR", "ZEKI METIN", "ZEKİ METİN",
        "SENER SEN", "ŞENER ŞEN",
        "CUNEYT ARKIN", "CÜNEYT ARKIN",
        "TARIK AKAN", "ILYAS SALMAN", "YILMAZ GUNEY",
        "HALIT AKCATEPE", "HALİT AKÇATEPE", "MUNIR ÖZKUL",
        "MUNİR ÖZKUL", "SADRI ALISIK", "SADRİ ALİSİK",
        "GULDUR GULDUR", "GÜLDÜR GÜLDÜR"]):
        return "TR SINEMA VOD"

    # --- TR DIZI ---
    if any(k in n for k in ["FX HD", "FOX CRIME", "BEIN SERIES", "DIZI SMART"]):
        return "TR DIZI"

    # --- TR 7/24 DIZI ---
    if n.startswith("7/24") or "7/24 " in n:
        return "TR 7/24 DIZI"

    # --- TR HABER (ULUSAL'dan once!) ---
    if any(k in n for k in ["24 HD", "A HABER", "A NEWS", "A PARA",
        "AKIT TV", "BBN TURK", "BENGUTURK", "BLOOMBERG HT",
        "CADDE TV", "CNN TURK", "EKOTURK", "FLASH HABER",
        "HABER GLOBAL", "HABERTURK", "HALK TV",
        "IBB TV", "KRT TV", "LIDER HABER",
        "NTV", "SZC TV", "TELE 1", "TELE1",
        "TGRT HABER", "TRT HABER", "TURKHABER",
        "TV100", "TVNET", "ULKE TV", "ULUSAL KANAL",
        "TBMM"]):
        return "TR HABER"

    # --- TR ULUSAL ---
    if any(k in combined for k in ["TRT 1", "TRT 2", "TRT TURK", "TRT AVAZ", "TRT 4K",
        "ATV HD", "ATV HD+", "ATV AVRUPA",
        "SHOW TV", "SHOW TURK", "SHOW MAX",
        "STAR TV", "KANAL D", "FOX TV",
        "TV8", "TV 8", "TV 8,5", "TV 8.5",
        "TEVE 2", "BEYAZ TV", "A2 HD", "A2 HD+",
        "KANAL 7", "360 HD", "360 HD+",
        "TGRT EU", "EURO STAR", "EURO D",
        "TV 8 INT", "TV8 INT", "KANAL 7 AVRUPA"]):
        # ULUSAL'dan cikarilacak kanallar
        if any(k in n for k in ULUSAL_EXCLUDE):
            return "TR HABER"
        return "TR ULUSAL"

    # --- TR BELGESEL ---
    if any(k in n for k in ["BELGESEL", "DISCOVERY", "NAT GEO", "NATIONAL GEOGRAPHIC",
        "HISTORY", "ANIMAL", "DA VINCI", "VIASAT EXPLORE", "VIASAT HISTORY",
        "BBC EARTH", "HABITAT TV", "TARIH TV", "CHASSE", "ANIMAUX",
        "DOCUBOX", "LOVE NATURE", "TRT BELGESEL",
        "BEIN IZ", "BEIN GURME", "BEIN HOME",
        "FASHION HD", "FAST FUN", "TARIM TURK",
        "STINGRAY", "CIFTCI TV", "YABAN TV", "TGRT BELGESEL"]):
        return "TR BELGESEL"
    if "DMAX" in n or "TLC" in n:
        return "TR BELGESEL"

    # --- TR COCUK ---
    if any(k in n for k in ["COCUK", "CARTOON", "NICKELODEON", "NICK JR",
        "MINIKA", "MOONBUG", "MOOUNBUG", "DISNEY JUNIOR", "CBEEBIES",
        "CARTOONITO", "BABY TV", "KIDS",
        "MASAL TV", "SEVIMLI DOSTLAR", "HEIDI", "ARI MAYA",
        "REDKIT", "DIGITAL TAYFA", "KONUŞAN TOM", "KONUSAN TOM",
        "ELIF", "AKILLI TAVŞAN", "ASLAN", "BIZ IKIMIZ", "BİZ İKİMİZ",
        "BULMACA", "ŞIRINLER", "SIRINLER", "İTFAIYECİ SAM",
        "DINOTRUX", "JOHNNY TEST", "OSCAR",
        "KÜÇÜK TREN", "PJ MASKELILER", "ROBOCAR POLI",
        "KUKULI", "CANIM KARDESIM", "CANIM KARDEŞİM",
        "DORU", "CILLE", "EGE ILE GAGA", "EGE İLE GAGA",
        "ELIF VE ARKADAS", "ELIF VE ARKADAŞ",
        "GÖKKUŞAĞI", "GOKKUSAGI", "İBI", "KARE", "KELOĞLAN",
        "KOYUN SHAUN", "PAW PATROL", "ANGRY BIRDS",
        "HAPŞUU", "KÖSTEBEKGİLLER", "KOSTEBEKGİLLER",
        "KÜÇÜK HEZARFEN", "KUKLALI", "KUZUCUK",
        "MAYSA", "MIGHTY EXPRESS", "OLSAYDIM",
        "PINKY MALINKY", "PİRIL", "PIRIL",
        "RAFADAN TAYFA", "SU ELÇILERİ", "SU ELCILERI",
        "SÜNGER BOB", "SUNGER BOB",
        "PATRON BEBEK", "NILOYA", "OZI",
        "PEPEE", "KÜÇÜK OTOBÜS", "LEYLEK KARDEŞ",
        "CICIKI", "SONIC BOOM", "MY LITTLE PONY", "LARVA",
        "BARBIE", "POLLY POCKET", "ALVIN", "LOLI ROCK",
        "PAC-MAN", "KARDESIM OZI", "KARDEŞİM OZİ"]):
        return "TR COCUK"
    if "TRT DIYANET COCUK" in n or "TRT COCUK" in n:
        return "TR COCUK"

    # --- TR MUZIK ---
    if any(k in n for k in ["MUZIK", "MÜZİK", "MUSIC",
        "KRAL POP", "KRAL TV", "POWER TV", "POWER TURK",
        "NUMBER 1", "NUMBER1", "DREAM TURK",
        "MILYON TV", "TRT MUZIK", "TRACE URBAN", "TATLISES", "NET MUZIK"]):
        return "TR MUZIK"

    # --- TR DINI ---
    if any(k in n for k in ["DINI", "DİNİ", "DIYANET", "DİYANET",
        "DOST TV", "KABE TV", "LALEGUL", "LALEGÜL",
        "REHBER TV", "SEMERKAND", "MELTEM TV",
        "MEDINE TV", "MESAJ TV", "DIYAR TV", "BERAT TV",
        "HZ YUSUF"]):
        return "TR DINI"

    # --- TR RADYO ---
    if any(k in n for k in ["RADYO", "RADIO", " FM"]):
        return "TR RADYO"

    # --- TR YEREL (şehir bazli) ---
    if any(k in n for k in ["MALATYA", "KAHRAMANMARAS", "BURSA", "ISPARTA",
        "ZONGULDAK", "RIZE", "DENIZLI", "DENİZLİ",
        "SANLIURFA", "ŞANLIURFA", "ELAZIG", "ELAZIĞ",
        "KONYA", "MERSIN", "KUTAHYA", "KÜTAHYA",
        "KIBRIS", "KIBRİS", "ADANA", "ADIYAMAN",
        "ORDU", "ANTALYA", "CANAKKALE", "ÇANAKKALE",
        "KAYSERI", "KAYSERİ", "KOCAELI", "KOCAELİ",
        "SIVAS", "KARADENIZ", "KARADENİZ", "VIYANA"]):
        return "TR YEREL"
    if any(k in combined for k in ["YEREL", "AKILLI TV", "ANADOLU DERNEK",
        "BEYKENT TV", "CAN TV", "CEM TV", "EGE TV",
        "KADIRGA TV", "KANAL AVRUPA", "KANAL B",
        "TEMPO TV", "TV 4 HD", "TV 5 HABER",
        "UCANKUS TV", "VATAN TV", "YOL TV", "ON4 TV",
        "MAVI KARADENIZ", "MAVİ KARADENİZ"]):
        return "TR YEREL"

    # --- DE VOLLPROGRAMM ---
    if any(k in combined for k in ["ARD", "ZDF", "DAS ERSTE", "WDR", "NDR",
        "BR ", "SWR", "HR ", "MDR", "RBB", "PHOENIX", "3SAT",
        "KIKA", "ONE", "ARTE", "TAGESSCHAU", "ZDFNEO", "ZDFINFO",
        "PROSIEBEN", "SAT.1", "SAT1", "RTL", "VOX",
        "KABEL1", "KABEL EINS", "RTL2", "SUPER RTL",
        "SIXX", "TELE 5"]):
        return "DE VOLLPROGRAMM"

    # --- DE NACHRICHTEN ---
    if any(k in combined for k in ["NACHRICHTEN", "N-TV", "N24", "WELT", "SPIEGEL"]):
        return "DE NACHRICHTEN"

    # --- DE DOKU ---
    if any(k in combined for k in ["DOKU", "DOKUMENTATION", "D-MAX", "DMAX",
        "N24 DOKU", "SPIEGEL TV"]):
        return "DE DOKU"

    # --- DE KINDER ---
    if any(k in combined for k in ["KINDER", "KIDS", "TOGGO"]):
        return "DE KINDER"

    # --- DE FILM (genis eslesme!) ---
    if any(k in n for k in ["SKY CINEMA", "13TH STREET", "AXN",
        "TNT SERIE", "TNT FILM", "SKY HITS", "SKY ACTION",
        "KINO", "CINEMA", "SERIE"]):
        return "DE FILM"
    # Alman film kanallari - DE prefix + film kelimeleri
    if any(k in combined for k in ["DE: FILM", "DE: MOVIE", "DE: SKY", "DE: AXN"]):
        return "DE FILM"

    # --- DE SPORT (genis eslesme!) ---
    if any(k in n for k in ["SPORT", "EUROSPORT", "SKY SPORT",
        "DAZN", "SPORT1", "MOTORVISION"]):
        return "DE SPORT"
    if any(k in combined for k in ["DE: SPORT", "DE: EUROSPORT", "DE: SKY SPORT"]):
        return "DE SPORT"

    # --- DE MUSIK ---
    if any(k in n for k in ["MUSIK", "VIVA", "DELUXE"]):
        return "DE MUSIK"

    # --- DE SONSTIGE (Avusturya, İsvicre, diger) ---
    if any(k in n for k in ["ORF", "PULS 4", "SERVUS", "SRF", "SWISS"]):
        return "DE SONSTIGE"

    # --- DE catch-all ---
    if any(k in combined for k in ["DE:", "DE ", "GERMAN", "4K DE", "FHD DE", "HD DE"]):
        return "DE SONSTIGE"

    # --- TR catch-all ---
    if any(k in combined for k in ["TURKEY", "TURKIYE", "TÜRKİYE", "TR:", "TR "]):
        return "TR YEREL"

    return "TR YEREL"

def clean_name(name):
    n = name.upper()
    for remove in [" (1)", " (2)", " (3)", " (4)", " (5)", "(BACKUP)", "+", " HEVC", " RAW", " SD", " FHD", " UHD", " 4K", " H265"]:
        n = n.replace(remove, "")
    n = re.sub(r'\([^)]*\)', '', n)
    n = n.strip()
    return n

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
    filtered = []
    removed = 0
    for ch in channels:
        country = detect_country(ch)
        if country in ("TR", "DE", "BOTH"):
            name = ch.get("name", "Unknown")
            url = ch.get("url", "")
            logo = ch.get("logo", "")
            group = ch.get("group", "")
            grp = remap_group(name, group)
            if grp == "__REMOVE__":
                removed += 1
                continue
            ch_id = 0
            m = re.search(r'/play\d+/(\d+)\.m3u8', url)
            if m:
                ch_id = int(m.group(1))
            if ch_id == 0:
                ch_id = abs(hash(name)) % 9999999
            filtered.append({"id": ch_id, "name": name, "url": url, "hls": "", "grp": grp, "country": country if country != "BOTH" else "TR", "logo": logo, "clean_name": clean_name(name)})
    tr_count = sum(1 for c in filtered if c["country"] == "TR")
    de_count = sum(1 for c in filtered if c["country"] == "DE")
    # Grup bazli sayilar
    grp_counts = {}
    for c in filtered:
        g = c["grp"]
        grp_counts[g] = grp_counts.get(g, 0) + 1
    grp_str = ", ".join(f"{g}={v}" for g, v in sorted(grp_counts.items()))
    add_log(f"Filtrelenmis: {len(filtered)} (TR={tr_count}, DE={de_count}),Removed={removed}")
    add_log(f"Gruplar: {grp_str}")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM channels")
    for ch in filtered:
        c.execute("INSERT OR REPLACE INTO channels (id,name,url,hls,grp,country,logo) VALUES (?,?,?,?,?,?,?)",
            (ch["id"], ch["name"], ch["url"], ch["hls"], ch["grp"], ch["country"], ch["logo"]))
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
    DATA_READY = True
    add_log("=== VxParser HAZIR ===")
