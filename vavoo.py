import requests, random, os, json, urllib3, time, re, ssl, base64
from urllib.request import Request, urlopen
from multiprocessing import Process

from utils.common import Logger as Logger
import utils.common as com

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ssl._create_default_https_context = ssl._create_unverified_context

session = requests.session()
cachepath = com.cp
con0 = com.con0
con1 = com.con1
_path = com.lp

# --- 1. STANDART VAVOO TOKEN (ping2) ---
def getAuthSignature():
    key = com.get_setting('signkey')
    if key:
        try:
            ip = com.get_public_ip()
            now = int(time.time()) * 1000
            jkey = json.loads(json.loads(base64.b64decode(key.encode('utf-8')).decode('utf-8'))['data'])
            if 'ips' in jkey and jkey['ips'][0] == ip:
                if 'validUntil' in jkey and int(jkey['validUntil']) > now: return key
        except: pass

    Logger(1, "Vavoo Token (ping2) aliniyor...", "auth", "process")
    headers = {'User-Agent': 'VAVOO/2.6', 'Accept': 'application/json'}
    try:
        vlist_req = requests.get("http://mastaaa1987.github.io/repo/veclist.json", headers=headers, timeout=10)
        veclist = vlist_req.json()['value']
        sig = None
        for _ in range(5):
            vec = {"vec": random.choice(veclist)}
            # Yeni Endpoint: https://www.vavoo.tv/api/box/ping2
            req = requests.post('https://www.vavoo.tv/api/box/ping2', data=vec, headers=headers, timeout=5).json()
            if req.get('signed'): sig = req['signed']; break
        if sig:
            com.set_setting('signkey', sig)
            return sig
    except: pass
    return None

# --- 2. LOKKE / WATCHED IMZA (app/ping) ---
def getWatchedSig():
    key = com.get_setting('wsignkey')
    if key:
        try:
            ip = com.get_public_ip()
            now = int(time.time()) * 1000
            jkey = json.loads(json.loads(base64.b64decode(key.encode('utf-8')).decode('utf-8'))['data'])
            if 'ips' in jkey and jkey['ips'][0] == ip:
                if 'validUntil' in jkey and int(jkey['validUntil']) > now: return key
        except: pass

    Logger(1, "Lokke Imza (app/ping) aliniyor...", "auth", "process")
    _headers = {
        "user-agent": "okhttp/4.11.0", 
        "accept": "application/json", 
        "content-type": "application/json; charset=utf-8"
    }
    # TAM PAYLOAD (Eksik veriler tamamlandı)
    _data = {
        "token":"",
        "reason":"boot",
        "locale":"de",
        "theme":"dark",
        "metadata":{
            "device":{"type":"desktop","uniqueId":""},
            "os":{"name":"win32","version":"Windows 10 Education","abis":["x64"],"host":"DESKTOP-USER"},
            "app":{"platform":"electron"},
            "version":{"package":"app.lokke.main","binary":"1.0.19","js":"1.0.19"}
        },
        "appFocusTime":173,
        "playerActive":False,
        "playDuration":0,
        "devMode":True,
        "hasAddon":True,
        "castConnected":False,
        "package":"app.lokke.main",
        "version":"1.0.19",
        "process":"app",
        "firstAppStart":int(time.time()*1000)-10000,
        "lastAppStart":int(time.time()*1000)-10000,
        "ipLocation":0,
        "adblockEnabled":True,
        "proxy":{"supported":["ss"],"engine":"cu","enabled":False,"autoServer":True,"id":0},
        "iap":{"supported":False}
    }
    try:
        # Yeni Endpoint: https://www.lokke.app/api/app/ping
        req = requests.post('https://www.lokke.app/api/app/ping', json=_data, headers=_headers, timeout=10).json()
        sig = req.get("addonSig")
        if sig:
            com.set_setting('wsignkey', sig)
            Logger(1, "Lokke Imzasi basariyla alindi!", "auth", "process")
            return sig
    except Exception as e:
        Logger(3, f"Lokke Imza Hatasi: {e}")
    return None

# --- 3. LINK ÇÖZÜMLEME (mediahubmx-resolve) ---
def resolve_link(link):
    sig = getWatchedSig()
    if not sig: return None
    
    _headers = {
        "user-agent": "MediaHubMX/2", 
        "accept": "application/json", 
        "content-type": "application/json; charset=utf-8", 
        "mediahubmx-signature": sig
    }
    _data = {
        "language":"de",
        "region":"AT",
        "url":link,
        "clientVersion":"3.0.2"
    }
    try:
        # Yeni Endpoint: https://vavoo.to/mediahubmx-resolve.json
        r = requests.post("https://vavoo.to/mediahubmx-resolve.json", data=json.dumps(_data), headers=_headers, timeout=10).json()
        if r and len(r) > 0:
            # Genelde ilk sonuç çalışır
            return r[0].get("url")
    except Exception as e:
        Logger(3, f"Link cozumleme hatasi: {e}")
    return None

# --- GRUPLAMA VE DB FONKSİYONLARI (Aynı) ---
def get_channel_group(name):
    name_lower = name.lower()
    if any(x in name_lower for x in ['spor', 'sport', 'bein', 'tivibu', 's sport', 'trt spor', 'a spor']): return 'Spor'
    if any(x in name_lower for x in ['haber', 'news', 'cnn', 'ntv', 'a haber', 'trt haber', 'ülke', 'tgrt']): return 'Haber'
    if any(x in name_lower for x in ['sinema', 'movie', 'film', 'cinema', 'dizi', 'fx', 'salon']): return 'Sinema'
    if any(x in name_lower for x in ['belgesel', 'nat geo', 'discovery', 'da vinci', 'animal', 'history', 'bbc earth']): return 'Belgesel'
    if any(x in name_lower for x in ['çocuk', 'cocuk', 'cartoon', 'disney', 'minika', 'trt çocuk']): return 'Çocuk'
    if any(x in name_lower for x in ['müzik', 'muzik', 'music', 'power', 'kral', 'number one']): return 'Müzik'
    return 'Ulusal'

def sky_dbfill(m3u8_generation=True):
    lang = int(com.get_setting('lang'))
    Logger(1, 'Turk kanallari taramasi baslatildi...', 'db', 'process')
    cur0 = con0.cursor(); cur1 = con1.cursor()
    try:
        req = Request('https://www.vavoo.to/live2/index?output=json', headers={'User-Agent': 'VAVOO/2.6'})
        content = urlopen(req, timeout=10).read().decode('utf8')
        channel_list = json.loads(content)
    except: return

    main_list_name = 'Turkey'
    cur0.execute('SELECT * FROM lists WHERE name="' + main_list_name + '"')
    if not cur0.fetchone(): cur0.execute('INSERT INTO lists VALUES (NULL,"' + main_list_name + '","0")')
    con0.commit()
    cur0.execute('SELECT * FROM lists WHERE name="' + main_list_name + '"')
    main_list_id = cur0.fetchone()['id']

    count = 0
    for c in channel_list:
        if not any(x in c.get('group', '').lower() for x in ['turkey', 'turkish', 'tr', 'türk', 'türkei']): continue
        name_raw = c.get('name', '')
        name_clean = re.sub(r'[^\x00-\x7F]+', '', name_raw)
        group_name = get_channel_group(name_clean)
        
        cur0.execute('SELECT * FROM categories WHERE category_name="' + group_name + '" AND lid="' + str(main_list_id) + '"')
        if not cur0.fetchone():
            cur0.execute('INSERT INTO categories VALUES (NULL,"live","' + str(group_name) + '","' + str(main_list_id) + '","0")')
            con0.commit()
            cur0.execute('SELECT * FROM categories WHERE category_name="' + group_name + '" AND lid="' + str(main_list_id) + '"')
        cid = cur0.fetchone()['category_id']
        
        cur1.execute('SELECT * FROM channel WHERE name="' + name_clean + '"')
        if not cur1.fetchone():
            cur1.execute('INSERT INTO channel VALUES(NULL,"' + name_clean + '","' + group_name + '","' + c.get('logo', '') + '","","' + c.get('url') + '","' + name_clean + '","Turkey","[' + str(cid) + ']","")')
            count += 1
        else:
            cur1.execute('UPDATE channel SET url="' + c.get('url') + '" WHERE name="' + name_clean + '"')
    
    con0.commit(); con1.commit()
    Logger(0, f'{count} kanal eklendi. HLS linkleri aliniyor...', 'db', 'process')
    
    # HLS Linklerini MediaHubMX'ten çekiyoruz (Eksik olan kısım burasıydı)
    sig = getWatchedSig()
    if sig:
        _headers = {"user-agent": "MediaHubMX/2", "accept": "application/json", "mediahubmx-signature": sig}
        _data_base = {"language":"de","region":"AT","catalogId":"iptv","id":"iptv","adult":False,"sort":"name","clientVersion":"3.0.2", "filter":{"group":"Turkey"}}
        try:
            r = requests.post("https://vavoo.to/mediahubmx-catalog.json", json=_data_base, headers=_headers, timeout=15).json()
            items = r.get("items", [])
            Logger(1, f"{len(items)} HLS kaydi bulundu.", 'db', 'process')
            for item in items:
                hls_url = item.get("url")
                clean_name = re.sub(r'[^\x00-\x7F]+', '', item.get("name", ""))
                cur1.execute('SELECT id FROM channel WHERE name="' + clean_name + '"')
                exists = cur1.fetchone()
                if exists:
                    cur1.execute('UPDATE channel SET hls="' + hls_url + '" WHERE id=' + str(exists['id']))
            con1.commit()
            Logger(0, "HLS linkleri kaydedildi.", 'db', 'process')
        except Exception as e:
            Logger(3, f"HLS Tarama hatasi: {e}")

    if m3u8_generation: gen_m3u8()
    Logger(0, 'Tamamlandi.', 'db', 'process')

def gen_m3u8():
    hurl = 'http://'+str(com.get_setting('server_ip'))+':'+str(com.get_setting('server_port'))
    cur0 = con0.cursor(); cur1 = con1.cursor()
    cur0.execute('SELECT * FROM lists WHERE name="Turkey"')
    main_list = cur0.fetchone(); 
    if not main_list: return
    lid = main_list['id']
    fpath = os.path.join(_path, "Turkey.m3u8")
    if os.path.exists(fpath): os.remove(fpath)
    tf = open(fpath, "w", encoding='utf-8')
    tf.write("#EXTM3U\n")
    cur0.execute('SELECT * FROM categories WHERE lid="'+ str(lid) +'" ORDER BY category_name ASC')
    for cat in cur0.fetchall():
        cur1.execute('SELECT * FROM channel WHERE cid LIKE "%['+ str(cat['category_id']) +']%"')
        for row in cur1.fetchall():
            tf.write('#EXTINF:-1 tvg-name="%s" group-title="%s",%s\n' % (row['name'], cat['category_name'], row['name']))
            tf.write('#EXTVLCOPT:http-user-agent=VAVOO/2.6\n')
            tf.write('%s/channel/%s\n' % (hurl, row['id']))
    tf.close()
    Logger(0, 'M3U8 Olusturuldu.', 'm3u8', 'process')