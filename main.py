"""
Backend FastAPI para Crash 1000 — recibe datos del monitor local (MT5)
y los expone para que el frontend (dashboard de inversores) los lea.

Endpoints:
    POST /api/crash1000/update   <- lo llama crash1000_monitor.py (requiere token)
    GET  /api/crash1000/status   <- lo llama el frontend (publico)
"""
import os
import time
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Crash1000 Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CRASH1000_TOKEN = os.getenv("CRASH1000_TOKEN", "")
OFFLINE_AFTER_SEG = 60  # si no hay update en 60s, se reporta offline

datos_globales = {
    "balance": 0.0,
    "pnl_dia": 0.0,
    "pnl_total_ciclo": 0.0,
    "estado": "offline",
    "wins": 0,
    "losses": 0,
    "extra": {},
    "last_update_ts": 0,
    "last_update_iso": None,
}


class UpdateData(BaseModel):
    balance: float
    pnl_dia: float
    pnl_total_ciclo: float
    estado: str
    wins: int
    losses: int
    extra: Optional[dict] = None


@app.post("/api/crash1000/update")
async def update_data(data: UpdateData, x_token: Optional[str] = Header(None)):
    if CRASH1000_TOKEN and x_token != CRASH1000_TOKEN:
        raise HTTPException(status_code=403, detail="Token invalido")

    global datos_globales
    datos_globales.update(data.dict())
    datos_globales["last_update_ts"] = time.time()
    datos_globales["last_update_iso"] = datetime.utcnow().isoformat()
    return {"status": "ok"}


@app.get("/api/crash1000/status")
async def get_status():
    online = (time.time() - datos_globales.get("last_update_ts", 0)) < OFFLINE_AFTER_SEG
    return {
        "online": online,
        "balance": datos_globales.get("balance"),
        "pnl_dia": datos_globales.get("pnl_dia"),
        "pnl_total_ciclo": datos_globales.get("pnl_total_ciclo"),
        "estado": datos_globales.get("estado"),
        "wins": datos_globales.get("wins"),
        "losses": datos_globales.get("losses"),
        "extra": datos_globales.get("extra"),
        "last_update": datos_globales.get("last_update_iso"),
    }


@app.get("/")
def root():
    return {"service": "crash1000-backend", "status": "running"}
