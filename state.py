"""
state.py - Ortak state modulu
server.py ve video.py ayni degiskenleri gorebilsin diye ayri modulde.
"""
import time

DATA_READY = False
STARTUP_ERROR = None
LOAD_TIME = 0
STARTUP_LOGS = []

# Token cache
_vavoo_sig = None
_vavoo_sig_time = 0
_watched_sig = None
_watched_sig_time = 0

# DB ayarları
DB_PATH = "/tmp/vxparser.db"
M3U_PATH = "/tmp/playlist.m3u"
PORT = 10000


def slog(msg):
    """Log mesajini hem print hem STARTUP_LOGS'a ekle"""
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    STARTUP_LOGS.append(entry)
    print(entry)
