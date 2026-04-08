@'
import os, sys, sqlite3, json, threading, time, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vxparser")

PORT = int(os.environ.get("PORT", 10000))
DB_PATH = os.environ.get("DB_PATH", "/tmp/vxparser.db")
M3U_PATH = os.environ.get("M3U_PATH", "/tmp/playlist.m3u")

DATA_READY = False
STARTUP_ERROR = None
LOAD_TIME = 0

GROUP_ORDER = [
    "TR ULUSAL","TR HABER","TR BEIN SPORTS","TR SPOR","TR BELGESEL",
    "TR SINEMA UHD","TR SINEMA","TR MUZIK","TR COCUK","TR YEREL",
    "TR DINI","TR RADYO","DE DEUTSCHLAND","DE VIP SPORTS","DE VIP SPORTS 2",
    "DE SPORT","DE AUSTRIA","DE SCHWEIZ","DE FILM","DE SERIEN",
    "DE KINO","DE DOKU","DE KIDS","DE MUSIK","DE INFOTAINMENT",
    "DE NEWS","DE THEMEN","DE SONSTIGE",
]

GROUP_RULES = {
    "TR ULUSAL": ["TRT 1","Show TV","Star TV","ATV","Kanal D","FOX TV","TV8","Tele1","Beyaz TV","TV 8.5","A2","TRT 4K","Tabii","Gain","TV 100","Flash TV","Kanal 7","TGRT","TV360","TLC","D MAX","ERT"],
    "TR HABER": ["Haber","CNN Turk","HABER","NTV","TRT Haber","Bloomberg","TVNET","A Haber","Benguturk","Haber Global","Ulusal Kanal","Sky Turk","TGRT Haber"],
    "TR BEIN SPORTS": ["beIN Sports","beIN SPORT","beIN","beIN 4K","beIN MAX"],
    "TR SPOR": ["Spor","A Spor","TRT Spor","TJK","S Sport","GS TV","FB TV","BJK TV","Fenerbahce","Galatasaray"],
    "TR BELGESEL": ["Belgesel","Nat Geo","Discovery","Animal Planet","History","DA Vinci","Yaban TV","BBC Earth"],
    "TR SINEMA UHD": ["4K","UHD"],
    "TR SINEMA": ["Film","Sinema","Cinema","Movie","Movies","DigiMAX","FilmBox","Magic Box","Yesilcam","Dream TV"],
    "TR MUZIK": ["Muzik","Müzik","Kral TV","Kral Pop","Power TV","Power Turk","Number One","TRT Muzik","Dream Turk","NR1"],
    "TR COCUK": ["Cocuk","Çocuk","Cartoon","Disney","Nick","TRT Cocuk","Minika","Baby TV","Pepee","Kidz"],
    "TR YEREL": ["Yerel"],
    "TR DINI": ["Dini","Din","Diyanet","Semerkand","Hilal","Lalegul","Yasin TV"],
    "TR RADYO": ["Radyo","Radio","FM","TRT Radyo","Kral FM","Power FM","Radyo D","Pal FM","Alem FM","Metro FM"],
    "DE DEUTSCHLAND": ["ARD","ZDF","Das Erste","WDR","NDR","BR ","SWR","HR ","MDR","RBB","Phoenix","3sat","KiKA","ONE","Arte","tagesschau24","zdfinfo","zdfneo"],
    "DE VIP SPORTS": ["Sky Sport","Sky Bundesliga","Eurosport","DAZN","Sport1"],
    "DE VIP SPORTS 2": ["Sky Sport Austria","Sky Sport Premier","Sky Sport LaLiga","Telekom Sport","Magenta Sport"],
    "DE SPORT": ["Sport ","Eurosport","Sportdigital","Motorvision"],
    "DE AUSTRIA": ["ORF","Puls 4","Servus","ATV "],
    "DE SCHWEIZ": ["SRF","Swiss","CH "],
    "DE FILM": ["Sky Cinema","RTL+","13th Street","AXN","TNT Serie","TNT Film","Sky Hits","Sky Action"],
    "DE SERIEN": ["Serie","RTL","Sat.1","ProSieben","VOX","kabel eins","RTL2","Super RTL","Sixx","TELE 5"],
    "DE KINO": ["Kino"],
    "DE DOKU": ["Doku","Docu","Spiegel TV","Geo","N24 Doku","D-MAX"],
    "DE KIDS": ["Kind","Kids","Toggo"],
    "DE MUSIK": ["Musik","VIVA","Deluxe Music"],
    "DE INFOTAINMENT": ["Info","N24","WELT","n-tv","euro news","BBC World","France 24"],
    "DE NEWS": ["News","Tagesschau"],
    "DE THEMEN": ["Shop","QVC","HSE","Bibel TV","Sonstig","Regional"],
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS categories(cid INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,sort_order INTEGER DEFAULT 9999)")
    c.execute("CREATE TABLE IF NOT EXISTS channels(lid INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,grp TEXT DEFAULT '',cid INTEGER DEFAULT 0,logo TEXT DEFAULT '',url TEXT DEFAULT '',sort_order INTEGER DEFAULT 9999)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ch_cid ON channels(cid)")
    conn.commit()
    conn.close()
    log.info("DB baslatildi: %s", DB_PATH)

def fetch_vavoo_channels():
    import httpx
    base_url = os.environ.get("VAVOO_BASE_URL", "")
    if not base_url:
        log.warning("VAVOO_BASE_URL yok!")
        return False
    try:
        log.info("Vavoo cekiliyor: %s", base_url)
        with httpx.Client(timeout=120.0, verify=False, follow_redirects=True) as client:
            resp = client.get(base_url)
            resp.raise_for_status()
            data = resp.json()
        channels_data = data.get("channels", data.get("streams", data.get("live", [])))
        if not channels_data and isinstance(data, list):
            channels_data = data
        if not channels_data:
            log.error("Kanal verisi yok")
            return False
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        added = 0
        for ch in channels_data:
            if isinstance(ch, dict):
                name = ch.get("name", ch.get("title", ch.get("channel", "")))
                url = ch.get("url", ch.get("stream", ch.get("src", "")))
                logo = ch.get("logo", ch.get("icon", ""))
                group = ch.get("group", ch.get("category", ""))
                cat_id = ch.get("cat_id", ch.get("cid", 0))
            elif isinstance(ch, (list, tuple)) and len(ch) >= 3:
                name, url, group = str(ch[0]), str(ch[1]), str(ch[2])
                logo, cat_id = "", 0
            else:
                continue
            if not name or not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            elif not url.startswith(("http://", "https://")):
                url = "https://" + url
            c.execute("INSERT OR REPLACE INTO channels(name,grp,cid,logo,url,sort_order) VALUES(?,?,?,?,?,?)",
                      (name, group, cat_id, logo, url, 9999))
            added += 1
        conn.commit()
        c.execute("SELECT DISTINCT cid, grp FROM channels WHERE cid > 0")
        for row in c.fetchall():
            if row[1]:
                c.execute("INSERT OR IGNORE INTO categories(cid,name,sort_order) VALUES(?,?,?)", (row[0], row[1], 9999))
        conn.commit()
        conn.close()
        log.info("%d kanal eklendi", added)
        return True
    except Exception as e:
        log.error("Cekme hatasi: %s", e)
        return False

def filter_tr_de():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM channels")
    total = c.fetchone()[0]
    keywords = ["TRT","Show","Star TV","ATV","Kanal D","FOX","TV8","Tele1","Beyaz","Flash","Kanal 7","TGRT",
        "Haber","CNN","NTV","Bloomberg","TVNET","A Haber","Bengu","Haber Global",
        "Spor","TJK","beIN","bein","S Sport","GS TV","FB TV","BJK TV","Fenerbahce","Galatasaray",
        "Belgesel","Nat Geo","Discovery","Animal","History","DA Vinci","Yaban","BBC Earth",
        "4K","UHD","Film","Sinema","Cinema","Movie","DigiMAX","FilmBox","Magic","Yesilcam","Dream",
        "Muzik","Kral","Power","Number One","NR1","VIVA","MTV",
        "Cocuk","Çocuk","Cartoon","Disney","Nick","Minika","Baby TV","Pepee",
        "Yerel","Dini","Din","Diyanet","Semerkand","Hilal","Lalegul",
        "Radyo","Radio","FM",
        "ARD","ZDF","WDR","NDR","BR ","SWR","HR ","MDR","RBB","Phoenix","3sat","KiKA","Arte",
        "RTL","Sat.1","ProSieben","VOX","kabel","RTL2","Super RTL","Sixx","TELE 5",
        "Sky","Sport1","Eurosport","DAZN","Sportdigital","Motorvision",
        "ORF","SRF","Servus","Puls","Schweiz","Austria","Swiss",
        "Fox ","AXN","TNT","Universal","Boomerang","Comedy Central",
        "Doku","D-MAX","Spiegel","Geo ","N24","WELT","n-tv","euro news","BBC World","France 24",
        "QVC","HSE","Bibel","Shop","Magenta","Telekom",
        "Tagesschau","tagesschau","zdf","ZDF","Das Erste","ONE","13th Street"]
    c.execute("SELECT lid, name, grp FROM channels")
    to_del = []
    for row in c.fetchall():
        combined = (row[1] + " " + row[2]).lower()
        if not any(k.lower() in combined for k in keywords):
            to_del.append(row[0])
    if to_del:
        ph = ",".join("?" * len(to_del))
        c.execute(f"DELETE FROM channels WHERE lid IN ({ph})", to_del)
    conn.commit()
    c.execute("SELECT COUNT(*) FROM channels")
    remaining = c.fetchone()[0]
    conn.close()
    log.info("Filtre: %d -> %d kanal (%d silindi)", total, remaining, len(to_del))

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
            rules = GROUP_RULES.get(gn, [])
            for kw in rules:
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
    log.info("Grup remap: %d kanal guncellendi", updated)

def generate_m3u():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT c.name,c.url,c.logo,COALESCE(cat.name,'Sonstige') as group_name
        FROM channels c LEFT JOIN categories cat ON c.cid=cat.cid
        ORDER BY COALESCE(cat.sort_order,9999),c.name""")
    lines = ["#EXTM3U"]
    for ch in c.fetchall():
        lines.append(f'#EXTINF:-1 tvg-logo="{ch["logo"] or ""}" group-title="{ch["group_name"]},{ch["name"]}')
        lines.append(ch["url"])
    conn.close()
    with open(M3U_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("M3U uretildi: %s", M3U_PATH)

def startup_sequence():
    global DATA_READY, STARTUP_ERROR
    start = time.time()
    try:
        log.info("=== VxParser Baslangic ===")
        init_db()
        if os.environ.get("VAVOO_BASE_URL"):
            fetch_vavoo_channels()
        else:
            log.warning("VAVOO_BASE_URL yok!")
        filter_tr_de()
        remap_groups()
        generate_m3u()
        LOAD_TIME = time.time() - start
        DATA_READY = True
        log.info("=== Hazir! (%.1fs) ===", LOAD_TIME)
    except Exception as e:
        STARTUP_ERROR = str(e)
        log.error("Hata: %s", e)

def main():
    log.info("VxParser Render baslatiliyor...")
    t = threading.Thread(target=startup_sequence, daemon=True)
    t.start()
    import uvicorn
    from video import app
    log.info("Uvicorn: 0.0.0.0:%d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
'@ | Out-File -Encoding utf8 server.py