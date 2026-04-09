"""
EPG module for VxParser - Electronic Program Guide
Fetches EPG data from TV Spielfilm (DE) and free TR sources
Generates XMLTV format XML for IPTV players
"""
import httpx
import asyncio
import gzip
import os
import time
import logging
import re
from datetime import datetime, timedelta
from xml.etree.ElementTree import Element, SubElement, ElementTree as ET, indent

log = logging.getLogger("vxparser.epg")

EPG_CACHE_PATH = "/tmp/epg.xml"
EPG_CACHE_GZ_PATH = "/tmp/epg.xml.gz"
EPG_REFRESH_INTERVAL = 21600  # 6 hours

# ===== TV Spielfilm API (DE EPG) =====
TVS_CHANNEL_URL = "https://live.tvspielfilm.de/static/content/channel-list/livetv"
TVS_BROADCAST_URL = "https://live.tvspielfilm.de/static/broadcast/list/{}/{}"
TVS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de,en-US;q=0.7,en;q=0.3",
    "Referer": "https://www.tvspielfilm.de/",
}

# ===== TR EPG Source - iptv-org (with fallback URLs) =====
TR_EPG_URLS = [
    "https://iptvx.one/epg/guides/tr/tvyayinlari.com.tr.xml.gz",
    "https://raw.githubusercontent.com/iptv-org/epg/master/guides/tr/tvyayinlari.com.tr.xml.gz",
    "https://raw.githubusercontent.com/LinearTV/linear-tv-epg/refs/heads/main/epg/tr/tr.epg.xml.gz",
]


def normalize_name(name):
    """Normalize channel name for matching"""
    n = name.upper().strip()
    for suffix in [" HD", " UHD", " 4K", " 8K", " FHD", " HEVC", " H.265", " H264",
                    " H265", " (RAW)", " RAW", " SD", "+", " AÇIK", " INTERNATIONAL",
                    " INT", " HEVC", "H.265", " TÜRKIYE"]:
        n = n.replace(suffix, "")
    n = re.sub(r'\s*\([^)]*\)\s*', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    n = n.replace("Ü", "U").replace("Ö", "O").replace("Ä", "A").replace("Ş", "S")
    n = n.replace("Ç", "C").replace("Ğ", "G").replace("İ", "I").replace("ı", "I")
    return n


# ===== TR Channel tvg-id mapping (Vavoo name -> XMLTV ID) =====
TR_TVG_IDS = {
    # --- TR Ulusal ---
    "TRT 1": "TRT 1.tr",
    "TRT 2": "TRT 2.tr",
    "TRT WORLD": "TRT World.tr",
    "TRT TURK": "TRT Turk.tr",
    "TRT AVAZ": "TRT Avaz.tr",
    "TRT 4K": "TRT 4K.tr",
    "ATV": "ATV.tr",
    "ATV HD": "ATV.tr",
    "ATV HD+": "ATV.tr",
    "ATV AVRUPA": "ATV Avrupa.tr",
    "SHOW TV": "Show TV.tr",
    "SHOW TURK": "Show Turk.tr",
    "SHOW MAX": "Show TV.tr",
    "STAR TV": "Star TV.tr",
    "KANAL D": "Kanal D.tr",
    "FOX TV": "Fox TV.tr",
    "TV 8": "TV 8.tr",
    "TV8": "TV 8.tr",
    "TV 8.5": "TV 8.5.tr",
    "TV 8,5": "TV 8.5.tr",
    "TEVE 2": "Teve2.tr",
    "TEVE2": "Teve2.tr",
    "BEYAZ TV": "Beyaz TV.tr",
    "A2 HD": "A2TV.tr",
    "A2": "A2TV.tr",
    "KANAL 7": "Kanal 7.tr",
    "KANAL 7 AVRUPA": "Kanal 7 Avrupa.tr",
    "360 HD": "360HD.tr",
    "EURO STAR": "EuroStar.tr",
    "EURO D": "Euro D.tr",
    "TGRT EU": "TGRT EU.tr",
    "TV 8 INT": "TV 8 INT.tr",
    "TV8 INT": "TV 8 INT.tr",

    # --- TR Spor ---
    "A SPOR": "A Spor.tr",
    "TRT SPOR": "TRT Spor.tr",
    "S SPORT": "S Sport.tr",
    "TIVIBU SPOR": "Tivibu Spor.tr",
    "SPOR SMART": "Spor Smart.tr",
    "EXXEN SPORT": "Exxen Spor.tr",
    "SPORTS TV": "SportsTV.tr",
    "NBA TV": "NBA TV.tr",
    "TJK TV": "TJK TV.tr",
    "FB TV": "FB TV.tr",
    "GS TV": "GSTV.tr",
    "TAY TV": "Tay TV.tr",
    "FIGHT BOX": "FightBox.tr",
    "EDGE SPORT": "Edge Sport.tr",
    "TRACE SPORT": "Trace Sport.tr",
    "BEIN SPORTS 1": "beIN Sports 1.tr",
    "BEIN SPORTS 2": "beIN Sports 2.tr",
    "BEIN SPORTS 3": "beIN Sports 3.tr",
    "BEIN SPORTS 4": "beIN Sports 4.tr",
    "BEIN SPORTS MAX": "beIN Sports Max.tr",
    "BEIN SPORT": "beIN Sports 1.tr",

    # --- TR Haber ---
    "TRT HABER": "TRT Haber.tr",
    "A HABER": "A Haber.tr",
    "A NEWS": "A Haber.tr",
    "A PARA": "A Para.tr",
    "NTV": "NTV.tr",
    "HABERTURK": "Haberturk.tr",
    "HABER GLOBAL": "Haber Global.tr",
    "CNN TURK": "CNNTurk.tr",
    "TVNET": "TVNet.tr",
    "BLOOMBERG HT": "Bloomberg HT.tr",
    "FLASH HABER": "Flash Haber.tr",
    "TGRT HABER": "TGRTHaber.tr",
    "ULKE TV": "Ulke TV.tr",
    "TV100": "TV100.tr",
    "HALK TV": "Halk TV.tr",
    "TELE1": "Tele1.tr",
    "TELE 1": "Tele1.tr",
    "SOZCU": "Sozcu TV.tr",
    "SÖZCÜ": "Sozcu TV.tr",
    "KRT TV": "KRT TV.tr",
    "LIDER HABER": "Lider Haber.tr",
    "AKIT TV": "Akit TV.tr",
    "BENGUTURK": "Benguturk.tr",
    "BBN TURK": "BBN Turk.tr",
    "EKOTURK": "Ekoturk.tr",
    "CADDE TV": "Cadde TV.tr",
    "IBB TV": "IBB TV.tr",
    "TURKHABER": "Turk Haber.tr",
    "24 HD": "24.tr",
    "24 TV": "24.tr",
    "TBMM TV": "TBMM TV.tr",

    # --- TR Sinema ---
    "BEIN MOVIES 1": "beIN Movies Premiere.tr",
    "BEIN MOVIES 2": "beIN Movies Stars.tr",
    "BEIN MOVIES 3": "beIN Movies Family.tr",
    "BEIN MOVIES 4": "beIN Movies Action.tr",
    "BEIN MOVIES": "beIN Movies Premiere.tr",
    "BEIN MOVIE": "beIN Movies Premiere.tr",
    "BEIN SERIES": "beIN Series.tr",
    "MOVIE SMART": "MovieSmart Premium.tr",
    "MOVIE SMART GOLD": "MovieSmart Gold.tr",
    "MOVIE SMART PLATIN": "MovieSmart Platin.tr",
    "MOVIE SMART TURK": "MovieSmart Turk.tr",
    "SINEMA TV": "MovieMax Premier.tr",
    "SINEMA TV 1": "MovieMax Premier.tr",
    "SINEMA TV 2": "MovieMax Festival.tr",
    "SINEMA TV AILE": "MovieMax Family.tr",
    "SINEMA TV AKSIYON": "MovieMax Action.tr",
    "FILMBOX": "MovieSmart Gold.tr",
    "FILMBOX ADRENA": "FILMBOX Adrenalin.tr",
    "FILMBOX PREMIER": "FILMBOX Premier.tr",
    "BLU TV PLAY": "BluTV Play.tr",
    "EPIC DRAMA": "Epic Drama.tr",
    "DIZI SMART": "Dizi Smart.tr",
    "FX HD": "Fox Crime.tr",
    "FOX CRIME": "Fox Crime.tr",

    # --- TR Belgesel ---
    "BEIN IZ": "Turkmax Gurme.tr",
    "BEIN GURME": "Turkmax Gurme.tr",
    "BEIN HOME": "Bein Home.tr",
    "TRT BELGESEL": "TRT Belgesel.tr",
    "TARIH TV": "Tarih TV.tr",
    "BBC EARTH": "BBC Earth.tr",
    "YABAN TV": "Yaban TV.tr",
    "CIFTCI TV": "Ciftci TV.tr",
    "AV TV": "AV TV.tr",
    "HABITAT TV": "Habitat TV.tr",
    "FAST FUN": "Fast Fun Box.tr",
    "FASHION TV": "Fashion TV.tr",
    "TGRT BELGESEL": "TGRT Belgesel.tr",
    "ANIMAUX": "Animal Planet.tr",
    "DOCUBOX": "Documentary.tr",
    "LOVE NATURE": "Love Nature.tr",
    "STINGRAY": "Stingray.tr",
    "CHASSE PECHE": "Chassepeche.tr",
    "DA VINCI": "Da Vinci.tr",
    "DISCOVERY": "Discovery Channel.tr",
    "DISCOVERY SCIENCE": "Discovery Science.tr",
    "DISCOVERY SHOWCASE": "Discovery Showcase.tr",
    "NAT GEO": "National Geographic.tr",
    "NATIONAL GEO": "National Geographic.tr",
    "NAT GEO WILD": "Nat Geo Wild.tr",
    "NAT GEO PEOPLE": "Nat Geo People.tr",
    "VIASAT EXPLORE": "Viasat Explore.tr",
    "VIASAT HISTORY": "Viasat History.tr",
    "HISTORY": "History HD.tr",
    "DMAX": "DMAX.tr",
    "TLC": "TLC.tr",
    "DMAX TURKIYE": "DMAX.tr",

    # --- TR Cocuk ---
    "TRT COCUK": "TRT Cocuk.tr",
    "TRT DIYANET COCUK": "TRT Diyanet Cocuk.tr",
    "MINIKA GO": "Minika Go.tr",
    "MINIKA COCUK": "Minika Cocuk.tr",
    "SMART COCUK": "Smart Cocuk.tr",
    "DA VINCI KIDS": "Da Vinci Kids.tr",
    "DUCK TV": "Duck TV.tr",
    "MASAL TV": "Masal TV.tr",
    "NICKELODEON": "Nickelodeon.tr",
    "NICK JR": "Nick Jr..tr",
    "CARTOON NETWORK": "Cartoon Network.tr",
    "DISNEY CHANNEL": "Disney Channel.tr",
    "DISNEY JUNIOR": "Disney Junior.tr",
    "BABY TV": "Baby TV.tr",
    "CBEEBIES": "CBeebies.tr",
    "CARTOONITO": "Cartoonito.tr",
    "BOOMERANG": "Boomerang.tr",

    # --- TR Muzik ---
    "TRT MUZIK": "TRT Muzik.tr",
    "KRAL TV": "Kral TV.tr",
    "KRAL POP": "Kral Pop TV.tr",
    "POWER TURK": "Power Turk.tr",
    "POWER TV": "Power TV.tr",
    "DREAM TURK": "Dream Turk.tr",
    "NUMBER 1": "Number1.tr",
    "NUMBER1": "Number1.tr",
    "TATLISES": "Tatlises TV.tr",
    "NET MUZIK": "Net Muzik.tr",
    "MILYON TV": "Milyon TV.tr",
    "TRACE URBAN": "Trace Urban.tr",
    "NR1 TURK": "Number1.tr",
    "NR1 ASK": "Number1.tr",

    # --- TR Dini ---
    "TRT DIYANET": "TRT Diyanet.tr",
    "KABE TV": "Kabe TV.tr",
    "MEDINE TV": "Medine TV.tr",
    "SEMERKAND": "Semerkand TV.tr",
    "LALEGUL": "Lalegul TV.tr",
    "LALEGÜL": "Lalegul TV.tr",
    "DOST TV": "DOST TV.tr",
    "REHBER TV": "Rehber TV.tr",
    "MELTEM TV": "Meltem TV.tr",
    "MESAJ TV": "Mesaj TV.tr",
    "DIYAR TV": "Diyar TV.tr",
    "BERAT TV": "Berat TV.tr",

    # --- TR Radyo ---
    "TRT FM": "TRT FM.tr",
    "TRT RADYO 1": "TRT Radyo 1.tr",
    "TRT RADYO 3": "TRT Radyo 3.tr",
    "TRT RADYO 4": "TRT Radyo 4.tr",
    "TRT TURK RADYO": "TRT Turk Radyo.tr",
    "POWER FM": "Power FM.tr",
    "SUPER FM": "Super FM.tr",
    "RAFADAN TAYFA": "Rafadan Tayfa.tr",

    # --- TR Yerel / Diger ---
    "TIVIBU": "Tivibu.tr",
    "EUROSPORT": "Eurosport 1.tr",
    "EUROSPORT 1": "Eurosport 1.tr",
    "EUROSPORT 2": "Eurosport 2.tr",
    "KOSTEBEKGILLER": "Kostebekgiller.tr",
    "KUKULI": "Kukuli.tr",
    "PEPEE": "Pepee.tr",
    "CANIM KARDESIM": "Canim Kardesim.tr",
    "GULDUR GULDUR": "Guldur Guldur.tr",
    "GÜLDÜR GÜLDÜR": "Guldur Guldur.tr",
    "VIZYONTV": "VizyonTV.tr",
    "YESILCAM BOX": "Yesilcam Box.tr",
    "KEMAL SUNAL": "Kemal Sunal TV.tr",
    "CIFTCI": "Ciftci TV.tr",
}

# ===== DE Channel tvg-id mapping =====
DE_TVG_IDS = {
    # --- DE Vollprogramm ---
    "DAS ERSTE": "DasErste.de",
    "ARD": "DasErste.de",
    "ARD ALPHA": "ARD-alpha.de",
    "ARD MEDIATHEK": "DasErste.de",
    "ZDF": "ZDF.de",
    "ZDF INFO": "ZDFinfo.de",
    "ZDFINFO": "ZDFinfo.de",
    "ZDF NEO": "ZDFneo.de",
    "ZDFNEO": "ZDFneo.de",
    "3SAT": "3sat.de",
    "PHOENIX": "PHOENIX.de",
    "KIKA": "KiKA.de",
    "ONE": "ONE.de",
    "ONE HD": "ONE.de",
    "TAGESSCHAU24": "tagesschau24.de",
    "WDR": "WDR Fernsehen.de",
    "WDR HD": "WDR Fernsehen.de",
    "NDR": "NDR Fernsehen.de",
    "NDR HD": "NDR Fernsehen.de",
    "MDR": "MDR Fernsehen.de",
    "MDR HD": "MDR Fernsehen.de",
    "SWR": "SWR Fernsehen.de",
    "SWR HD": "SWR Fernsehen.de",
    "RBB": "rbb.de",
    "RBB BERLIN": "rbb.de",
    "RBB BRANDENBURG": "rbb.de",
    "BR": "BR Fernsehen.de",
    "BR HD": "BR Fernsehen.de",
    "ARTE": "ARTE.de",
    "ARTE HD": "ARTE.de",
    "HR": "HR Fernsehen.de",
    "HR HD": "HR Fernsehen.de",
    "SR": "SR Fernsehen.de",
    "SR HD": "SR Fernsehen.de",

    # --- DE Privat ---
    "RTL": "RTL.de",
    "RTL HD": "RTL.de",
    "RTL+": "RTLplus.de",
    "RTL PLUS": "RTLplus.de",
    "RTL2": "RTL2.de",
    "RTL 2": "RTL2.de",
    "SUPER RTL": "SUPER RTL.de",
    "VOX": "VOX.de",
    "VOX HD": "VOX.de",
    "SAT.1": "SAT.1.de",
    "SAT 1": "SAT.1.de",
    "SAT.1 HD": "SAT.1.de",
    "PROSIEBEN": "ProSieben.de",
    "PRO7": "ProSieben.de",
    "PROSIEBEN HD": "ProSieben.de",
    "PROSIEBEN MAXX": "ProSieben MAXX.de",
    "PROSIEBEN MAXX HD": "ProSieben MAXX.de",
    "PROSIEBEN FUN": "ProSieben Fun.de",
    "KABEL 1": "kabel eins.de",
    "KABEL1": "kabel eins.de",
    "KABEL EINS": "kabel eins.de",
    "KABEL 1 HD": "kabel eins.de",
    "KABEL 1 DOKU": "KABEL1 Doku.de",
    "TELE 5": "Tele 5.de",
    "SIXX": "sixx.de",
    "SIXX HD": "sixx.de",
    "SAT.1 GOLD": "SAT.1 emotions.de",
    "SAT 1 GOLD": "SAT.1 emotions.de",
    "RTL LIVING": "RTL Living.de",
    "RTL CRIME": "RTL Crime.de",
    "RTL PASSION": "RTL Passion.de",
    "RTL NITRO": "RTL Nitro.de",
    "PULS 4": "PULS 4.at",

    # --- DE Nachrichten ---
    "N-TV": "n-tv.de",
    "NTV": "n-tv.de",
    "N24": "N24.de",
    "N24 DOKU": "N24 Doku.de",
    "WELT": "WELT.de",
    "WELT N24": "WELT.de",
    "EINFACH NACHRICHTEN": "einfach-nachrichten.de",

    # --- DE Film ---
    "SKY CINEMA": "Sky Cinema.de",
    "SKY CINEMA 1": "Sky Cinema.de",
    "SKY CINEMA +1": "Sky Cinema +1.de",
    "SKY CINEMA +24": "Sky Cinema +24.de",
    "SKY CINEMA ACTION": "Sky Cinema Action.de",
    "SKY CINEMA HITS": "Sky Cinema Hits.de",
    "SKY CINEMA FAMILY": "Sky Cinema Family.de",
    "SKY HITS": "Sky Hits.de",
    "SKY ACTION": "Sky Action.de",
    "SKY ATLANTIC": "Sky Atlantic.de",
    "SKY KRIMI": "Sky Krimi.de",
    "SKY ONE": "Sky One.de",
    "SKY BUNDESLIGA": "Sky Bundesliga 1.de",
    "SKY SPORT": "Sky Sport 1.de",
    "SKY SPORT 1": "Sky Sport 1.de",
    "SKY SPORT 2": "Sky Sport 2.de",
    "SKY SPORT AUSTRIA": "Sky Sport Austria 1.de",
    "13TH STREET": "13th Street Universal.de",
    "TNT FILM": "TNT Film.de",
    "TNT SERIE": "TNT Serie.de",
    "TNT COMEDY": "TNT Comedy.de",
    "AXN": "AXN.de",
    "AXN HD": "AXN.de",
    "HEIMATKANAL": "Heimatkanal.de",
    "ROMANCE TV": "Romance TV.de",
    "SONY CHANNEL": "Sony Channel.de",
    "SYFY": "SYFY.de",
    "COMEDY CENTRAL": "Comedy Central.de",
    "CLASSICA": "Classica.de",
    "ANIXE": "Anixe.de",
    "ANIXE HD": "Anixe.de",

    # --- DE Sport ---
    "EUROSPORT 1": "Eurosport 1.de",
    "EUROSPORT 2": "Eurosport 2.de",
    "EUROSPORT": "Eurosport 1.de",
    "SPORT1": "Sport1.de",
    "SPORT1 HD": "Sport1.de",
    "SPORT1+": "Sport1+.de",
    "SPORTDIGITAL": "sportdigital.de",
    "DAZN": "DAZN 1.de",
    "DAZN 1": "DAZN 1.de",
    "DAZN 2": "DAZN 2.de",
    "DAZN 3": "DAZN 3.de",
    "MOTORVISION": "Motorvision TV.de",
    "MOTORVISION TV": "Motorvision TV.de",

    # --- DE Doku ---
    "DMAX": "DMAX.de",
    "DMAX HD": "DMAX.de",
    "N24 DOKU": "N24 Doku.de",
    "SPIEGEL TV WISSEN": "Spiegel TV Wissen.de",
    "SPIEGEL GESCHICHTE": "Spiegel Geschichte.de",
    "SPIEGEL TV": "Spiegel Geschichte.de",
    "DISCOVERY": "Discovery Channel.de",
    "DISCOVERY HD": "Discovery Channel.de",
    "NAT GEO": "Nat Geo.de",
    "NAT GEO HD": "Nat Geo.de",
    "NAT GEO WILD": "Nat Geo Wild.de",
    "NAT GEO WILD HD": "Nat Geo Wild.de",
    "NAT GEO PEOPLE": "Nat Geo People.de",
    "HISTORY": "History.de",
    "HISTORY HD": "History.de",
    "TLC": "TLC.de",
    "TLC HD": "TLC.de",
    "LOVE NATURE": "Love Nature.de",
    "ARTE DOKU": "ARTE.de",

    # --- DE Kinder ---
    "KINDER": "Super RTL.de",
    "TOGGO": "TOGGO plus.de",
    "TOGGO PLUS": "TOGGO plus.de",
    "JUNIOR": "Junior.de",
    "NICKELODEON": "Nick.de",
    "NICK": "Nick.de",
    "NICK HD": "Nick.de",
    "NICK JR": "Nick Jr..de",
    "NICK JR HD": "Nick Jr..de",
    "NICKTOONS": "Nicktoons.de",
    "CARTOON NETWORK": "Cartoon Network.de",
    "CARTOON NETWORK HD": "Cartoon Network.de",
    "BOOMERANG": "Boomerang.de",
    "DISNEY CHANNEL": "Disney Channel.de",
    "DISNEY CHANNEL HD": "Disney Channel.de",
    "DISNEY JUNIOR": "Disney Junior.de",
    "CBEEBIES": "CBeebies.de",
    "CARTOONITO": "Cartoonito.de",
    "BABY TV": "Baby TV.de",
    "FIX UND FOXI": "Fix &amp; Foxi.de",

    # --- DE Musik ---
    "VIVA": "VIVA.de",
    "DELUXE MUSIC": "DELUXE MUSIC.de",
    "DELUXE": "DELUXE MUSIC.de",
    "MTV": "MTV.de",
    "MTV HD": "MTV.de",
    "MTV LIVE": "MTV Live HD.de",

    # --- DE Osterreich / Schweiz ---
    "ORF 1": "ORF 1.at",
    "ORF 2": "ORF 2.at",
    "ORF 3": "ORF III.at",
    "ORF SPORT+": "ORF SPORT+.at",
    "ORF SPORT": "ORF Sport+.at",
    "SERVUS TV": "ServusTV Deutschland.de",
    "SERVUS TV HD": "ServusTV Deutschland.de",
    "PULS 4": "PULS 4.at",
    "SRF 1": "SRF 1.ch",
    "SRF 2": "SRF 2.ch",
    "SRF INFO": "SRF Info.ch",
    "SWISS 1": "S1.ch",
    "3+": "3plus.ch",
    "4+": "4plus.ch",
    "5+": "5plus.ch",
    "6+": "6plus.ch",
}

# ===== Picon URL mapping (channel name -> logo URL) - fallback only =====
PICON_MAP = {
    # TR Ulusal
    "TRT 1": "https://i.imgur.com/X1P41DK.png",
    "TRT 2": "https://i.imgur.com/X1P41DK.png",
    "ATV": "https://i.imgur.com/LnDA8tF.png",
    "SHOW TV": "https://i.imgur.com/GBAQYWM.png",
    "STAR TV": "https://i.imgur.com/g2OJYDC.png",
    "KANAL D": "https://i.imgur.com/eKDmXxL.png",
    "FOX TV": "https://i.imgur.com/sOrbOJG.png",
    "TV8": "https://i.imgur.com/b5viQnb.png",
    "TEVE2": "https://i.imgur.com/sOrbOJG.png",
    "BEYAZ TV": "https://i.imgur.com/sOrbOJG.png",
    "TV 8.5": "https://i.imgur.com/b5viQnb.png",
    "A2": "https://i.imgur.com/LnDA8tF.png",
    "KANAL 7": "https://i.imgur.com/sOrbOJG.png",
    # TR Spor
    "A SPOR": "https://i.imgur.com/LnDA8tF.png",
    "TRT SPOR": "https://i.imgur.com/X1P41DK.png",
    "S SPORT": "https://i.imgur.com/sOrbOJG.png",
    "TJK TV": "https://i.imgur.com/X1P41DK.png",
    # TR Haber
    "TRT HABER": "https://i.imgur.com/X1P41DK.png",
    "NTV": "https://i.imgur.com/LnDA8tF.png",
    "CNN TURK": "https://i.imgur.com/sOrbOJG.png",
    "A HABER": "https://i.imgur.com/LnDA8tF.png",
    "HABERTURK": "https://i.imgur.com/sOrbOJG.png",
    # DE
    "DAS ERSTE": "https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/logos/tv/das-erste.png",
    "ZDF": "https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/logos/tv/zdf.png",
    "RTL": "https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/logos/tv/rtl.png",
    "SAT.1": "https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/logos/tv/sat-1.png",
    "PROSIEBEN": "https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/logos/tv/prosieben.png",
    "VOX": "https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/logos/tv/vox.png",
    "KABEL 1": "https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/logos/tv/kabel-eins.png",
    "RTL2": "https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/logos/tv/rtl-2.png",
    "SUPER RTL": "https://raw.githubusercontent.com/jnk22/kodinerds-iptv/master/logos/tv/super-rtl.png",
}


def get_tvg_id(name, country="", vavoo_tvg_id=""):
    """Get tvg-id for a channel - prefer Vavoo's own, fallback to our mapping."""
    # 1. If Vavoo provided a valid tvg_id, use it directly
    if vavoo_tvg_id and vavoo_tvg_id.strip():
        return vavoo_tvg_id.strip()

    # 2. Fall back to our own mapping
    norm = normalize_name(name)
    if country == "DE" or (country != "TR" and not country):
        for prefix in ["DE:", "DE "]:
            if norm.startswith(prefix):
                stripped = norm[len(prefix):].strip()
                if stripped in DE_TVG_IDS:
                    return DE_TVG_IDS[stripped]
                continue
        if norm in DE_TVG_IDS:
            return DE_TVG_IDS[norm]
    if country == "TR" or (not country):
        for prefix in ["TR:", "TR "]:
            if norm.startswith(prefix):
                stripped = norm[len(prefix):].strip()
                if stripped in TR_TVG_IDS:
                    return TR_TVG_IDS[stripped]
                continue
        if norm in TR_TVG_IDS:
            return TR_TVG_IDS[norm]
    return ""


def get_picon_url(name, vavoo_logo="", country=""):
    """Get picon URL - prefer Vavoo logo (full or relative), fallback to known URLs."""
    # 1. If Vavoo gave us a logo, use it (make it full URL if relative)
    if vavoo_logo:
        if vavoo_logo.startswith("http"):
            return vavoo_logo
        elif vavoo_logo.startswith("/"):
            return "https://vavoo.to" + vavoo_logo
    # 2. Fallback to PICON_MAP
    norm = normalize_name(name)
    if norm in PICON_MAP:
        return PICON_MAP[norm]
    return ""


# ===== XMLTV Generation =====
def generate_xmltv(channels, programmes):
    """Generate XMLTV format XML string"""
    root = Element("tv")
    root.set("generator-info-name", "VxParser EPG")
    root.set("generator-info-url", "https://github.com/vxparser")

    # Channels
    for ch in channels:
        ch_el = SubElement(root, "channel")
        ch_el.set("id", ch["id"])
        dn = SubElement(ch_el, "display-name")
        dn.text = ch["name"]
        if ch.get("icon"):
            icon_el = SubElement(ch_el, "icon")
            icon_el.set("src", ch["icon"])

    # Programmes
    for prog in programmes:
        p_el = SubElement(root, "programme")
        p_el.set("start", prog["start"])
        p_el.set("stop", prog["stop"])
        p_el.set("channel", prog["channel"])

        lang = prog.get("lang", "de")
        t = SubElement(p_el, "title")
        t.set("lang", lang)
        t.text = prog["title"]

        if prog.get("desc"):
            d = SubElement(p_el, "desc")
            d.set("lang", lang)
            d.text = prog["desc"]

        if prog.get("subtitle"):
            st = SubElement(p_el, "sub-title")
            st.set("lang", lang)
            st.text = prog["subtitle"]

        if prog.get("category"):
            cat = SubElement(p_el, "category")
            cat.set("lang", lang)
            cat.text = prog["category"]

        if prog.get("country"):
            c = SubElement(p_el, "country")
            c.text = prog["country"]

        if prog.get("date"):
            dt = SubElement(p_el, "date")
            dt.text = str(prog["date"])

        if prog.get("icon"):
            ic = SubElement(p_el, "icon")
            ic.set("src", prog["icon"])

        if prog.get("season") or prog.get("episode"):
            ep = SubElement(p_el, "episode-num")
            ep.set("system", "onscreen")
            s = prog.get("season", "")
            e = prog.get("episode", "")
            if s and e:
                ep.text = f"S{s} E{e}"
            elif s:
                ep.text = f"S{s}"
            elif e:
                ep.text = f"E{e}"

        if prog.get("credits"):
            cr = SubElement(p_el, "credits")
            for director in prog["credits"].get("director", []):
                elem = SubElement(cr, "director")
                elem.text = director
            for actor in prog["credits"].get("actor", []):
                elem = SubElement(cr, "actor")
                elem.text = actor

        if prog.get("rating"):
            rt = SubElement(p_el, "rating")
            v = SubElement(rt, "value")
            v.text = prog["rating"]

    indent(root)
    tree = ET(root)
    from io import BytesIO
    buf = BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue().decode("utf-8")


# ===== TV Spielfilm DE Fetcher =====
async def fetch_tvs_channels():
    """Fetch TV Spielfilm channel list"""
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.get(TVS_CHANNEL_URL, headers=TVS_HEADERS)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.error(f"TVS channel list error: {e}")
        return []


async def fetch_tvs_broadcasts(content_id, date_str):
    """Fetch broadcasts for a channel on a specific date"""
    try:
        url = TVS_BROADCAST_URL.format(content_id, date_str)
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.get(url, headers=TVS_HEADERS)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return []


async def build_de_epg(vavoo_channels):
    """Build EPG for DE channels using TV Spielfilm"""
    log.info("EPG: Fetching TV Spielfilm channel list...")
    tvs_channels = await fetch_tvs_channels()
    if not tvs_channels:
        log.error("EPG: TV Spielfilm channel list fetch failed")
        return [], []

    log.info(f"EPG: TV Spielfilm: {len(tvs_channels)} channels available")

    # Build TVS lookup: normalized name -> info
    tvs_lookup = {}
    for ch in tvs_channels:
        norm = normalize_name(ch.get("name", ""))
        tvs_lookup[norm] = {
            "id": ch.get("id", ""),
            "name": ch.get("name", ""),
            "icon": ch.get("image_large", {}).get("url", "")
        }

    # Match our DE channels to TV Spielfilm
    matched = {}
    for vch in vavoo_channels:
        if vch.get("country") != "DE":
            continue
        vnorm = normalize_name(vch.get("name", ""))
        for prefix in ["DE:", "DE "]:
            if vnorm.startswith(prefix):
                vnorm = vnorm[len(prefix):].strip()
        if vnorm in tvs_lookup:
            matched[vch["id"]] = tvs_lookup[vnorm]

    log.info(f"EPG: Matched {len(matched)} DE channels to TV Spielfilm")

    # Fetch broadcasts
    today = datetime.now()
    xml_channels = []
    xml_programmes = []
    total_progs = 0

    for vch_id, tvs_info in matched.items():
        vch = next((c for c in vavoo_channels if c["id"] == vch_id), None)
        if not vch:
            continue

        xml_channels.append({
            "id": tvs_info["id"],
            "name": tvs_info["name"],
            "icon": tvs_info.get("icon", "")
        })

        for day_offset in range(2):
            day = today + timedelta(days=day_offset)
            date_str = day.strftime("%Y-%m-%d")
            broadcasts = await fetch_tvs_broadcasts(tvs_info["id"], date_str)

            for b in broadcasts:
                try:
                    title = b.get("title", "")
                    if not title:
                        continue
                    start_ts = b.get("timestart", 0)
                    end_ts = b.get("timeend", 0)
                    if not start_ts or not end_ts:
                        continue

                    start_str = datetime.utcfromtimestamp(start_ts).strftime("%Y%m%d%H%M%S") + " +0000"
                    stop_str = datetime.utcfromtimestamp(end_ts).strftime("%Y%m%d%H%M%S") + " +0000"

                    credits = {}
                    director = b.get("director", "")
                    if director:
                        credits["director"] = [director]
                    actors = b.get("actors", [])
                    if actors:
                        credits["actor"] = [
                            a.get("name", "") if isinstance(a, dict) else str(a)
                            for a in actors if a
                        ]

                    icon = ""
                    try:
                        icon = b.get("images", [{}])[0].get("size4", "")
                    except (IndexError, KeyError, TypeError):
                        pass

                    season = b.get("seasonNumber", "")
                    episode = b.get("episodeNumber", "")
                    fsk = b.get("fsk", 0)

                    xml_programmes.append({
                        "start": start_str, "stop": stop_str,
                        "channel": tvs_info["id"],
                        "title": title,
                        "desc": b.get("text", ""),
                        "subtitle": b.get("episodeTitle", ""),
                        "category": b.get("genre", ""),
                        "lang": "de",
                        "country": b.get("country", ""),
                        "date": b.get("year", ""),
                        "season": str(season) if season and season > 0 else "",
                        "episode": str(episode) if episode and episode > 0 else "",
                        "icon": icon,
                        "credits": credits if credits else None,
                        "rating": str(fsk) if fsk and int(fsk) > 0 else ""
                    })
                    total_progs += 1
                except Exception:
                    continue

            # Rate limit: small delay between channels
            await asyncio.sleep(0.1)

    log.info(f"EPG: DE programmes: {total_progs}")
    return xml_channels, xml_programmes


# ===== TR EPG - Fetch from free source =====
async def build_tr_epg(vavoo_channels):
    """Build EPG for TR channels - use Vavoo tvg_ids, fetch from iptv-org sources."""
    tr_channels = [c for c in vavoo_channels if c.get("country") == "TR"]
    if not tr_channels:
        return [], []

    # Collect all unique tvg_ids from our TR channels
    xml_channels = []
    our_ids = set()
    for vch in tr_channels:
        # Prefer Vavoo's tvg_id, fall back to our mapping
        tvg_id = vch.get("tvg_id", "")
        if not tvg_id:
            tvg_id = get_tvg_id(vch.get("name", ""), "TR")
        if tvg_id:
            xml_channels.append({
                "id": tvg_id,
                "name": vch["name"],
                "icon": vch.get("logo", "")
            })
            our_ids.add(tvg_id)

    xml_programmes = []

    if not our_ids:
        log.warning("EPG: No TR tvg_ids collected, skipping TR EPG fetch")
        return xml_channels, xml_programmes

    log.info(f"EPG: TR: {len(our_ids)} unique tvg_ids to match")

    # Try each TR EPG source in order
    for epg_url in TR_EPG_URLS:
        try:
            log.info(f"EPG: Trying TR EPG from {epg_url[:70]}...")
            async with httpx.AsyncClient(timeout=60, verify=False) as client:
                r = await client.get(epg_url, follow_redirects=True)
                if r.status_code != 200:
                    log.warning(f"EPG: TR source returned HTTP {r.status_code}")
                    continue

                # Decompress gzipped XML
                try:
                    data = gzip.decompress(r.content)
                except Exception:
                    # Maybe it's not gzipped
                    data = r.content

                # Stream parse XML to find programmes for our channels
                from xml.etree.ElementTree import iterparse
                try:
                    context = iterparse.fromstring(data)
                except Exception:
                    log.warning(f"EPG: TR source XML parse failed")
                    continue

                tr_progs = []
                for event, elem in context:
                    if elem.tag == "programme":
                        ch_id = elem.get("channel", "")
                        if ch_id in our_ids:
                            prog = {"channel": ch_id, "lang": "tr"}
                            prog["start"] = elem.get("start", "")
                            prog["stop"] = elem.get("stop", "")
                            for child in elem:
                                if child.tag == "title":
                                    prog["title"] = (child.text or "")
                                elif child.tag == "desc":
                                    prog["desc"] = (child.text or "")
                                elif child.tag == "sub-title":
                                    prog["subtitle"] = (child.text or "")
                                elif child.tag == "category":
                                    prog["category"] = (child.text or "")
                                elif child.tag == "icon":
                                    prog["icon"] = child.get("src", "")
                                elif child.tag == "episode-num":
                                    prog["episode"] = (child.text or "")
                                elif child.tag == "date":
                                    prog["date"] = (child.text or "")
                            if "title" in prog:
                                tr_progs.append(prog)
                        elem.clear()
                xml_programmes = tr_progs
                log.info(f"EPG: TR programmes from {epg_url[:40]}: {len(tr_progs)}")
                if tr_progs:
                    break
        except Exception as e:
            log.error(f"EPG: TR source error ({epg_url[:40]}): {e}")
            continue

    log.info(f"EPG: TR channels: {len(xml_channels)}, programmes: {len(xml_programmes)}")
    return xml_channels, xml_programmes


# ===== Main EPG Builder =====
async def build_full_epg(vavoo_channels):
    """Build complete EPG for all channels"""
    log.info("=== EPG Build Starting ===")
    all_channels = []
    all_programmes = []

    # DE EPG from TV Spielfilm
    try:
        de_ch, de_prog = await build_de_epg(vavoo_channels)
        all_channels.extend(de_ch)
        all_programmes.extend(de_prog)
    except Exception as e:
        log.error(f"EPG: DE build error: {e}")

    # TR EPG from iptv-org
    try:
        tr_ch, tr_prog = await build_tr_epg(vavoo_channels)
        # Merge channels (avoid duplicates)
        existing_ids = {c["id"] for c in all_channels}
        for ch in tr_ch:
            if ch["id"] not in existing_ids:
                all_channels.append(ch)
                existing_ids.add(ch["id"])
        all_programmes.extend(tr_prog)
    except Exception as e:
        log.error(f"EPG: TR build error: {e}")

    if all_channels or all_programmes:
        xml_str = generate_xmltv(all_channels, all_programmes)
        with open(EPG_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(xml_str)
        with gzip.open(EPG_CACHE_GZ_PATH, "wb") as f:
            f.write(xml_str.encode("utf-8"))
        log.info(f"EPG: Saved - {len(all_channels)} channels, {len(all_programmes)} programmes")
        log.info(f"EPG: XML={os.path.getsize(EPG_CACHE_PATH)}B, GZ={os.path.getsize(EPG_CACHE_GZ_PATH)}B")
        return True

    log.warning("EPG: No data generated")
    return False


def get_cached_epg_xml():
    """Get cached EPG XML string"""
    if os.path.exists(EPG_CACHE_PATH):
        mtime = os.path.getmtime(EPG_CACHE_PATH)
        if (time.time() - mtime) < EPG_REFRESH_INTERVAL:
            try:
                with open(EPG_CACHE_PATH, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
    return None


def get_cached_epg_gz():
    """Get cached EPG gzip bytes"""
    if os.path.exists(EPG_CACHE_GZ_PATH):
        mtime = os.path.getmtime(EPG_CACHE_GZ_PATH)
        if (time.time() - mtime) < EPG_REFRESH_INTERVAL:
            try:
                with open(EPG_CACHE_GZ_PATH, "rb") as f:
                    return f.read()
            except Exception:
                pass
    return None


async def epg_refresh_loop():
    """Background task to refresh EPG periodically"""
    while True:
        await asyncio.sleep(EPG_REFRESH_INTERVAL)
        try:
            import state
            channels = state.get_all_channels(ordered=False)
            if channels:
                await build_full_epg(channels)
                log.info("EPG: Background refresh done")
        except Exception as e:
            log.error(f"EPG: Background refresh error: {e}")
