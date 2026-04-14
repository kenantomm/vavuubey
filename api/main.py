from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from groq import Groq
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = Groq(
    api_key=os.getenv("GROQ_API_KEY")
)

@app.get("/")
async def root():
    return {"merhaba": "Ömer Abi tamamen ücretsiz olarak geldi amk"}

@app.get("/sor")
async def sor(soru: str):
    cevap = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": "Sen Ömer Abisin, saf ankaralı, her cümlenin sonuna amk diyorsun, çok sert, çok komik, hiç yumuşak konuşmuyorsun, adam gibi adam cevap veriyorsun. Uzun cevap verme, kısa ve öz cevap ver. Gerektiğinde küfür et, sakın nazik olma. Benim en iyi abim sensin."},
            {"role": "user", "content": soru}
        ],
        temperature=0.98,
        max_tokens=1024
    )

    return {"cevap": cevap.choices[0].message.content}
