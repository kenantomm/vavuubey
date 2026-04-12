# VavooBey - IPTV Kanal Yonetim Sistemi

## Proje Hakkinda
VavooBey, Vavoo TV kanallarini proxy edip IPTV olarak sunan profesyonel bir FastAPI tabanli uygulamadir. Render.com uzerinde calisir, admin paneli ile kanal ve grup yonetimi saglar.

---

## Dosya Yapilari

### Render (Ana Sunucu)
```
vavuubey-render/
├── server.py          # Uygulama giris noktasi, startup, kanal cekme
├── video.py           # FastAPI app, streaming endpointleri, admin panel HTML
├── state.py           # Ortak state, token yonetimi, API cagrilari
├── render.yaml        # Render.com deploy yapilandirmasi
├── Dockerfile         # Docker imaji
├── requirements.txt   # Python bagimliklari
└── proje.md           # Bu dosya
```

### Hugging Face Spaces (Yedek)
```
vavuubey-hf/
├── server.py
├── video.py
├── state.py
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Ortam Degiskenleri (Render.com)

| Degisken      | Aciklama                           | Varsayilan       |
|---------------|------------------------------------|------------------|
| `PORT`        | Sunucu portu                       | 10000            |
| `ADMIN_USER`  | Admin panel kullanici adi          | admin            |
| `ADMIN_PASS`  | Admin panel sifre                  | vavuubey2024     |
| `DB_PATH`     | SQLite veritabani yolu             | /tmp/vavuubey.db |

---

## API Endpointleri

### Public Endpointler
| Endpoint              | Aciklama                    |
|-----------------------|-----------------------------|
| `GET /`               | Durum mesaji                |
| `GET /ping`           | Saglik kontrolu (200)      |
| `GET /get.php`        | M3U playlist                |
| `GET /player_api.php` | Xtream API uyumlu          |
| `GET /channel/{id}`   | Kanal stream proxy          |
| `GET /epg.xml`        | EPG (XMLTV format)          |
| `GET /logo/{id}`      | Logo proxy                  |
| `GET /robots.txt`     | Bot engelleme               |

### Admin Endpointler
| Endpoint                          | Aciklama                 |
|-----------------------------------|--------------------------|
| `GET /admin`                      | Admin panel              |
| `POST /admin/api/login`           | Giris                    |
| `POST /admin/api/logout`          | Cikis                    |
| `GET /admin/api/groups`           | Grup listesi             |
| `POST /admin/api/groups/create`   | Grup olustur             |
| `POST /admin/api/groups/rename`   | Grup yeniden adlandir    |
| `POST /admin/api/groups/delete`   | Grup sil                 |
| `POST /admin/api/groups/reorder`  | Grup siralama (up/down)  |
| `GET /admin/api/channels`         | Kanal listesi            |
| `POST /admin/api/channels/assign` | Kanal gruba ata          |
| `POST /admin/api/channels/reorder`| Kanal siralama           |
| `POST /admin/api/channels/move`   | Kanal tasi (drag&drop)   |
| `POST /admin/api/channels/ungroup`| Kanal gruptan cikar      |
| `POST /admin/api/refresh`         | Kanal listesini yenile   |

---

## Kanal Gruplari

### Turk Kanallari
- TR ULUSAL (Show TV, Star TV, Kanal D, ATV, FOX TV, TV8, vb.)
- TR HABER (CNN Turk, TRT Haber, A Haber, vb.)
- TR BEIN SPORTS (beIN Sports 1-4, MAX, 4K)
- TR BELGESEL (TRT Belgesel, Nat Geo, Discovery, vb.)
- TR SINEMA UHD / TR SINEMA (FilmBox, Sinema TV, vb.)
- TR MUZIK (Kral TV, Power TV, vb.)
- TR COCUK (Cartoon, Minika, TRT Cocuk, vb.)
- TR SPOR (A Spor, TRT Spor, S Sport, vb.)
- TR YEREL, TR DINI, TR RADYO

### Alman Kanallari
- DE DEUTSCHLAND (ARD, ZDF, WDR, vb.)
- DE VIP SPORTS / DE VIP SPORTS 2 (Sky Sport, DAZN, Magenta)
- DE SPORT (Eurosport, Sportdigital, vb.)
- DE AUSTRIA (ORF, Servus), DE SCHWEIZ (SRF)
- DE FILM (Sky Cinema, AXN, vb.)
- DE SERIEN (RTL, Sat.1, ProSieben, vb.)
- DE DOKU, DE KIDS, DE MUSIK, DE NEWS
- DE INFOTAINMENT, DE THEMEN, DE SONSTIGE

---

## Guvenlik Onlemleri

1. **Admin Panel Session Auth**: Admin paneli her zaman login zorunlu
2. **HttpOnly Cookie**: Session cookie httponly ve samesite=strict
3. **Security Headers**: X-Content-Type-Options, X-Frame-Options, CSP
4. **Permissions Policy**: Kamera/mikrofon/konum engellendi
5. **robots.txt**: Arama motorlari engellendi
6. **No Cache**: Admin sayfalari cache'lenmez
7. **SQL Injection Korumasi**: Parametreli sorgular
8. **Logo URL Dogrulama**: Sadece http/https URL'leri kabul edilir

---

## Deploy Linkleri

### Render.com (Ana Sunucu)
- **Dashboard**: https://dashboard.render.com
- **Service**: https://vavuubey.onrender.com
- **Admin Panel**: https://vavuubey.onrender.com/admin
- **M3U Playlist**: https://vavuubey.onrender.com/get.php
- **EPG**: https://vavuubey.onrender.com/epg.xml
- **Health Check**: https://vavuubey.onrender.com/ping

### Hugging Face Spaces (Yedek)
- **Space**: https://huggingface.co/spaces/vavuubey/vavuubey-iptv
- **URL**: https://vavuubey-iptv.hf.space

### GitHub
- **Repo**: https://github.com/vavuubey/vavuubey-iptv

---

## Monitoring

### UptimeRobot
- **Dashboard**: https://uptimerobot.com/dashboard
- **Monitor Type**: HTTP(s)
- **URL**: https://vavuubey.onrender.com/ping
- **Check Interval**: 5 dakika
- **Expected Status**: 200 OK
- **Alert**: Email + Slack

### Cron-Job (Jobkron)
- **Platform**: https://www.cron-job.org
- **Job**: VavooBey Keep-Alive
- **URL**: https://vavuubey.onrender.com/ping
- **Schedule**: Her 5 dakikada bir (0 */5 * * * *)
- **Purpose**: Render free tier uyku modunu onlemek icin

---

## Render.com Ayarlari

1. **Build Command**: `pip install -r requirements.txt`
2. **Start Command**: `python server.py`
3. **Runtime**: Python 3.11.6
4. **Instance Type**: Free (veya Starter)
5. **Plan**: Free tier 750 saat/ay
6. **Region**: Frankfurt (EU)

---

## IPTV Player Ayarlari

### M3U Playlist URL
```
https://vavuubey.onrender.com/get.php
```

### Xtream Codes API
```
Server: https://vavuubey.onrender.com
Username: vavuubey
Password: vavuubey
```

### EPG URL
```
https://vavuubey.onrender.com/epg.xml
```

---

## Teknik Detaylar

- **Framework**: FastAPI + Uvicorn
- **Database**: SQLite (WAL mode)
- **HTTP Client**: httpx (async)
- **Token Sistemi**: Vavoo ping2 + Lokke app/ping
- **Stream Resolve**: MediaHubMX catalog + resolve API
- **HLS Proxy**: Master playlist URL rewrite
- **Auto Refresh**: 6 saatte bir otomatik yenileme
- **Startup Timeout**: 120 saniye (hard limit)
- **Session Sure**: 24 saat (86400 saniye)
