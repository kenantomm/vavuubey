---
title: VxParser IPTV
emoji: 📺
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# VxParser IPTV Proxy

TR + DE kanallari, otomatik gruplama, M3U cikti, Admin panel, EPG.

## Endpoints

- **M3U Playlist**: `/get.php?username=admin&password=admin&type=m3u_plus`
- **Xtream API**: `/player_api.php?username=admin&password=admin&action=get_live_categories`
- **EPG XML**: `/epg.xml`
- **EPG GZ**: `/epg.xml.gz`
- **Admin Panel**: `/admin`
- **Status**: `/api/status`
- **Health**: `/ping`

## Features

- TR + DE kanal otomatik gruplama (24 grup)
- Drag & Drop kanal tasima
- Toplu kanal tasima
- Yukari/Asagi siralama
- Manuel override JSON export/import
- HuggingFace Spaces persistent storage (/data/)
- EPG (TR + DE)
- Xtream Codes API uyumlu
