import os, sys, asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from multiprocessing import Process
from uvicorn import Server, Config

import utils.common as common
from utils.common import Logger as Logger
import utils.vavoo as vavoo

cachepath = common.cp
listpath = common.lp
con1 = common.con1

common.check()

class UvicornServer(Process):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
    def stop(self): self.terminate()
    def run(self, *args, **kwargs):
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        server = Server(config=self.config)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try: loop.run_until_complete(server.serve())
        except: pass
        finally: loop.close()

app = FastAPI()

@app.get("/")
async def root():
    return HTMLResponse('<h1>VxParser Aktif</h1><p>Lutfen DB Doldurun.</p>')

@app.get("/{m3u8}.m3u8")
async def m3u8(m3u8: str):
    f = os.path.join(listpath, m3u8+'.m3u8')
    if os.path.exists(f):
        return FileResponse(f, media_type="application/x-mpegURL; charset=utf-8")
    raise HTTPException(status_code=404, detail="Liste yok.")

@app.get("/channel/{sid}")
async def channel(sid: str):
    cur = con1.cursor()
    cur.execute('SELECT * FROM channel WHERE id="' + sid + '"')
    data = cur.fetchone()
    
    if not data:
        raise HTTPException(status_code=404, detail="Kanal yok")

    # YÖNTEM 1: HLS + Lokke Imzasi (En Stabil)
    # Veritabanında 'hls' sütunu doluysa bunu kullan
    if data['hls']:
        hls_link = data['hls']
        Logger(1, f"HLS Cozuluyor: {data['name']}")
        resolved_url = vavoo.resolve_link(hls_link)
        if resolved_url:
            return RedirectResponse(url=resolved_url)
    
    # YÖNTEM 2: Standart Token (Yedek)
    Logger(1, f"Standart Token deneniyor: {data['name']}")
    sig = vavoo.getAuthSignature()
    if sig:
        base_url = str(data['url'])
        separator = '&' if '?' in base_url else '?'
        final_link = base_url + separator + 'n=1&b=5&vavoo_auth=' + sig
        return RedirectResponse(url=final_link)
    
    raise HTTPException(status_code=503, detail="Yayin acilamadi.")