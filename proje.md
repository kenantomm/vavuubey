# VxParser - IPTV Kanal Yonetim Sistemi

## Proje Hakkinda
VxParser, Vavoo TV uzerinden TR ve DE kanallarini ceken, gruplayan ve M3U playlist olarak sunan bir IPTV proxy sistemidir.

## Dosya Yapisi
```
vxparser-render/
├── server.py      # Giris noktasi, startup, kanal cekme, HLS
├── video.py       # FastAPI uygulamasi, streaming + admin panel
├── state.py       # Token, DB, grup kurallari, resolve
├── render.yaml    # Render.com deployment ayarlari
├── Dockerfile     # Docker/HuggingFace Spaces container
├── requirements.txt
├── .gitignore
└── proje.md       # Bu dosya
```

## Kanal Gruplari
### Turk Kanallar
| Grup | Aciklama |
|------|----------|
| TR ULUSAL | Ulusal haber ve eglence kanallari |
| TR HABER | 24 saat haber kanallari |
| TR BEIN SPORTS | beIN Sports kanallari |
| TR SPOR | Turk spor kanallari (A Spor, TRT Spor, vb.) |
| TR BELGESEL | Belgesel kanallari |
| TR SINEMA UHD | 4K/UHD film kanallari |
| TR SINEMA | Film kanallari |
| TR MUZIK | Muzik kanallari |
| TR COCUK | Cocuk kanallari |
| TR YEREL | Yerel kanallar |
| TR DINI | Dini kanallari |
| TR RADYO | Radyo kanallari |

### Alman Kanallar
| Grup | Aciklama |
|------|----------|
| DE DEUTSCHLAND | Alman ulusal kanallari |
| DE VIP SPORTS | Sky Sport, Eurosport, DAZN |
| DE VIP SPORTS 2 | Sky Sport Austria, Magenta Sport |
| DE SPORT | Spor kanallari |
| DE AUSTRIA | Avusturya kanallari |
| DE SCHWEIZ | Isvicre kanallari |
| DE FILM | Film kanallari |
| DE SERIEN | Dizi kanallari |
| DE KINO | Kino kanallari |
| DE DOKU | Belgesel kanallari |
| DE KIDS | Cocuk kanallari |
| DE MUSIK | Muzik kanallari |
| DE INFOTAINMENT | Infotainment kanallari |
| DE NEWS | Haber kanallari |
| DE THEMEN | Tema kanallari |
| DE SONSTIGE | Diger kanallar |

## Kurulum

### Render.com
1. GitHub reposuna pushla
2. Render'da yeni Web Service olustur
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python server.py`
5. Ortam degiskenleri: `ADMIN_USER`, `ADMIN_PASS` (opsiyonel)

### Hugging Face Spaces
1. GitHub reposuna pushla
2. HF Spaces'ta yeni Docker Space olustur
3. Repository bagla

## API Endpointleri

### Streaming
| Endpoint | Aciklama |
|----------|----------|
| `GET /get.php` | M3U playlist |
| `GET /player_api.php?action=get_live_categories` | Xtream gruplar |
| `GET /player_api.php?action=get_live_streams` | Xtream kanallar |
| `GET /channel/{lid}` | Kanal stream proxy |

### Admin Panel
| Endpoint | Aciklama |
|----------|----------|
| `GET /admin` | Admin paneli |
| `GET /admin/api/groups` | Grup listesi |
| `POST /admin/api/groups/create` | Grup olustur |
| `POST /admin/api/groups/rename` | Grup yeniden adlandir |
| `POST /admin/api/groups/delete` | Grup sil |
| `GET /admin/api/channels` | Kanal listesi |
| `POST /admin/api/channels/assign` | Kanallari gruba ata |
| `POST /admin/api/channels/reorder` | Kanal sirasi degistir |
| `POST /admin/api/channels/move` | Kanal tasi (drag&drop) |
| `POST /admin/api/channels/ungroup` | Kanallari grupsuz yap |
| `POST /admin/api/channels/normalize` | Sira numaralarini duzenle |

## Guvenlik
- Admin paneli Basic Auth ile korunmaktadir (ADMIN_USER/ADMIN_PASS)
- robots.txt ve noindex meta tag ile arama motorlari engellenmistir
- Gereksiz endpoint'ler kaldirilmistir

## Akis Mantigi
1. `server.py` baslar, background thread'de startup_sequence calisir
2. Lokke'den mediahubmx signature alinir
3. Vavoo live2/index'ten kanallar cekilir
4. TR ve DE kanallari filtrelenir
5. MediaHubMX catalog'dan HLS linkleri alinir
6. Kanallar gruplara atanir (GROUP_RULES)
7. M3U playlist ve Xtream API hazir
