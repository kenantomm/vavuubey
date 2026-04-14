"""
server.py - VxParser Ana Sunucu Modulu
DB Baslatma, Kanal Cekme, HLS Linkleri, Grup Remap
"""
import os
import sqlite3
import json
import time
import requests
import urllib3
import re
from urllib.request import Request, urlopen

import state

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_PATH = state.DB_PATH

# ============================================================
# DB BASLATMA
# ============================================================
def init_db():
    """SQLite DB olustur ve tablolari hazirla"""
    state.slog("DB baslatiliyor...")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Kanallar tablosu
    c.execute('''CREATE TABLE IF NOT EXISTS channels (
        lid TEXT PRIMARY KEY,
        name TEXT,
        grp TEXT,
        logo TEXT,
        url TEXT,
        hls TEXT,
        cid INTEGER,
        sort_order INTEGER DEFAULT 0,
        country TEXT
    )''')
    
    # Kategoriler tablosu
    c.execute('''CREATE TABLE IF NOT EXISTS categories (
        cid INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        sort_order INTEGER DEFAULT 0
    )''')
    
    conn.commit()
    conn.close()
    state.slog("DB baslatildi: " + DB_PATH)


# ============================================================
# KANAL CEKME (live2/index)
# ============================================================
def fetch_vavoo_channels():
    """Vavoo live2 kanal listesini cek"""
    state.slog("Vavoo live2 cekiliyor...")
    
    headers = {'User-Agent': 'VAVOO/2.6'}
    
    for live2_url in state.CONFIG["LIVE2_URLS"]:
        try:
            state.slog(f"  Deneniyor: {live2_url}")
            req = Request(live2_url, headers=headers)
            content = urlopen(req, timeout=15).read().decode('utf8')
            data = json.loads(content)
            state.slog(f"  OK: {live2_url} ({len(data)} kayit)")
            break
        except Exception as e:
            state.slog(f"  HATA: {live2_url} - {str(e)[:60]}")
            data = []
    
    if not data:
        state.slog("  TUM live2 URL'leri basarisiz!")
        return
    
    # Kanallari DB'ye kaydet
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    tr_count = de_count = 0
    
    for item in data:
        name = item.get('name', '')
        group = item.get('group', '').lower()
        url = item.get('url', '')
        logo = item.get('logo', '')
        
        # Turkey kanallari
        if any(x in group for x in ['turkey', 'turkish', 'tr', 'türk', 'türkei']):
            country = 'TR'
            tr_count += 1
        # Germany kanallari
        elif any(x in group for x in ['germany', 'deutschland', 'de ', 'austria', 'schweiz']):
            country = 'DE'
            de_count += 1
        else:
            continue
        
        # ID olustur (URL'den)
        lid = str(hash(url) % 10000000).zfill(7)
        
        # Varsa guncelle, yoksa ekle
        c.execute("SELECT lid FROM channels WHERE lid=?", (lid,))
        if c.fetchone():
            c.execute("UPDATE channels SET name=?, grp=?, logo=?, url=?, country=? WHERE lid=?",
                     (name, item.get('group', ''), logo, url, country, lid))
        else:
            c.execute("INSERT INTO channels (lid, name, grp, logo, url, country, sort_order) VALUES (?,?,?,?,?,?,?)",
                     (lid, name, item.get('group', ''), logo, url, country, 0))
    
    conn.commit()
    conn.close()
    state.slog(f"Kanallar: {tr_count + de_count} (TR={tr_count}, DE={de_count})")


# ============================================================
# HLS LINKLERI (catalog)
# ============================================================
def fetch_hls_links():
    """MediaHubMX catalog'dan HLS linklerini cek ve eslestir"""
    state.slog("HLS linkleri cekiliyor...")
    
    sig = state.get_watchedsig()
    if not sig:
        state.slog("  HATA: addonSig yok, HLS linkleri cekilemiyor!")
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Turkey HLS
    state.slog("  Turkey catalog cekiliyor...")
    turkey_items = state.fetch_catalog(sig, "Turkey")
    state.slog(f"  Turkey: {len(turkey_items)} catalog kayit")
    
    # HLS'leri eslestir
    matched = 0
    for item in turkey_items:
        name = item.get('name', '')
        hls_url = item.get('url', '')
        
        # Isim normalize et
        name_clean = normalize_name(name)
        
        # DB'de ara
        c.execute("SELECT lid FROM channels WHERE country='TR' AND (name=? OR name LIKE ?)", 
                 (name, f'%{name_clean}%'))
        row = c.fetchone()
        if row:
            c.execute("UPDATE channels SET hls=? WHERE lid=?", (hls_url, row[0]))
            matched += 1
    
    state.slog(f"  Turkey: {matched} HLS eslesti")
    
    # Germany HLS
    state.slog("  Germany catalog cekiliyor...")
    germany_items = state.fetch_catalog(sig, "Germany")
    state.slog(f"  Germany: {len(germany_items)} catalog kayit")
    
    matched_de = 0
    for item in germany_items:
        name = item.get('name', '')
        hls_url = item.get('url', '')
        
        name_clean = normalize_name(name)
        
        c.execute("SELECT lid FROM channels WHERE country='DE' AND (name=? OR name LIKE ?)", 
                 (name, f'%{name_clean}%'))
        row = c.fetchone()
        if row:
            c.execute("UPDATE channels SET hls=? WHERE lid=?", (hls_url, row[0]))
            matched_de += 1
    
    state.slog(f"  Germany: {matched_de} HLS eslesti")
    
    conn.commit()
    conn.close()
    state.slog(f"HLS toplam: {matched + matched_de} kanal guncellendi")


def normalize_name(name):
    """Isim normalize et - eslestirme icin"""
    # Turkce karakterleri degistir
    name = name.lower()
    name = name.replace('ı', 'i').replace('ğ', 'g').replace('ü', 'u')
    name = name.replace('ş', 's').replace('ö', 'o').replace('ç', 'c')
    name = name.replace('İ', 'I').replace('Ğ', 'G').replace('Ü', 'U')
    name = name.replace('Ş', 'S').replace('Ö', 'O').replace('Ç', 'C')
    # Ozel karakterleri kaldir
    name = re.sub(r'[^\w\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


# ============================================================
# GRUP REMAP
# ============================================================
def remap_groups():
    """Kanallari gruplara remap et"""
    state.slog("Grup remap baslatiliyor...")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Kategorileri olustur
    categories = {}
    for i, group_name in enumerate(state.GROUP_ORDER):
        c.execute("INSERT OR IGNORE INTO categories (name, sort_order) VALUES (?,?)", 
                 (group_name, i))
        c.execute("SELECT cid FROM categories WHERE name=?", (group_name,))
        row = c.fetchone()
        if row:
            categories[group_name] = row[0]
    
    conn.commit()
    
    # Kanallari gruplara ata
    c.execute("SELECT lid, name, country FROM channels")
    channels = c.fetchall()
    
    remapped = 0
    for lid, name, country in channels:
        group = determine_group(name, country)
        if group and group in categories:
            cid = categories[group]
            c.execute("UPDATE channels SET cid=?, grp=? WHERE lid=?", (cid, group, lid))
            remapped += 1
    
    conn.commit()
    conn.close()
    state.slog(f"Grup remap: {remapped} kanal")


def determine_group(name, country):
    """Kanal ismine gore grubu belirle"""
    name_lower = name.lower()
    
    # TR kurallari
    if country == 'TR':
        if any(x in name_lower for x in ['trt 1', 'show tv', 'star tv', 'atv', 'kanal d', 'fox', 'tv8', 'beyaz', 'kanal 7']):
            return 'TR ULUSAL'
        elif any(x in name_lower for x in ['bein sports', 'bein sport']):
            return 'TR BEIN SPORTS'
        elif any(x in name_lower for x in ['spor', 'a spor', 'trt spor', 's sport']):
            return 'TR SPOR'
        elif any(x in name_lower for x in ['haber', 'cnn', 'ntv', 'a haber', 'trt haber']):
            return 'TR HABER'
        elif any(x in name_lower for x in ['sinema', 'film', 'movie', 'dizi']):
            return 'TR SINEMA'
        elif any(x in name_lower for x in ['belgesel', 'nat geo', 'discovery']):
            return 'TR BELGESEL'
        elif any(x in name_lower for x in ['cocuk', 'cartoon', 'disney', 'nick']):
            return 'TR COCUK'
        elif any(x in name_lower for x in ['muzik', 'kral', 'power', 'music']):
            return 'TR MUZIK'
        else:
            return 'TR ULUSAL'
    
    # DE kurallari
    elif country == 'DE':
        if any(x in name_lower for x in ['ard', 'zdf', 'das erste']):
            return 'DE DEUTSCHLAND'
        elif any(x in name_lower for x in ['sky sport', 'bundesliga', 'dazn']):
            return 'DE VIP SPORTS'
        elif any(x in name_lower for x in ['sport', 'eurosport']):
            return 'DE SPORT'
        elif any(x in name_lower for x in ['sky cinema', 'film', 'kino']):
            return 'DE FILM'
        elif any(x in name_lower for x in ['rtl', 'sat.1', 'prosieben', 'vox']):
            return 'DE SERIEN'
        elif any(x in name_lower for x in ['doku', 'docu', 'n24']):
            return 'DE DOKU'
        elif any(x in name_lower for x in ['kika', 'kind', 'super rtl']):
            return 'DE KIDS'
        elif any(x in name_lower for x in ['orf', 'puls', 'servus']):
            return 'DE AUSTRIA'
        elif any(x in name_lower for x in ['srf', 'swiss']):
            return 'DE SCHWEIZ'
        else:
            return 'DE SONSTIGE'
    
    return None


# ============================================================
# ANA FONKSIYON
# ============================================================
def main():
    """Ana baslatma fonksiyonu"""
    state.slog(">>> main() basladi <<<")
    state.slog("=== VxParser Baslangic ===")
    state.slog(f"PORT={state.PORT} DB={state.DB_PATH}")
    state.slog(f"BASE_URLS: {state.CONFIG['BASE_URLS']}")
    state.slog(f"PING_URLS: {state.CONFIG['PING_URLS']}")
    
    start_time = time.time()
    
    try:
        # 1. addonSig al
        state.slog("[1/5] addonSig (app/ping)...")
        sig = state.get_watchedsig()
        if sig:
            state.slog("[1/5] addonSig=OK")
        else:
            state.slog("[1/5] addonSig=BASARISIZ")
        
        # 2. Vavoo token (opsiyonel)
        state.slog("[2/5] Vavoo token (ping2)...")
        vavoo_sig = state.get_auth_signature()
        if vavoo_sig:
            state.slog("[2/5] Vavoo=OK")
        else:
            state.slog("[2/5] Vavoo=BASARISIZ (Lokke kullanilacak)")
        
        # 3. DB + Kanallar
        state.slog("[3/5] DB + Kanallar...")
        init_db()
        fetch_vavoo_channels()
        state.slog("[3/5] Kanallar=OK")
        
        # 4. HLS Linkleri
        state.slog("[4/5] HLS linkleri (catalog)...")
        fetch_hls_links()
        state.slog("[4/5] HLS=OK")
        
        # 5. Grup remap
        state.slog("[5/5] Grup remap...")
        remap_groups()
        state.slog("[5/5] Grup=OK")
        
        state.LOAD_TIME = time.time() - start_time
        state.DATA_READY = True
        state.slog(f"=== TAMAM! ({state.LOAD_TIME:.1f}s) ===")
        
    except Exception as e:
        state.STARTUP_ERROR = str(e)
        state.slog(f"=== HATA: {e} ===")
        import traceback
        state.slog(traceback.format_exc())


if __name__ == "__main__":
    main()
