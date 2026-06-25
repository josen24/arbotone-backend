"""
Backend FastAPI para Crash 1000 — recibe datos del monitor local (MT5)
y los expone para que el frontend (dashboard de inversores) los lea.
"""

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permite que tu GitHub Pages lea los datos sin bloqueos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Token de seguridad tomado de las variables de entorno de Railway
CRASH1000_TOKEN = os.getenv("CRASH1000_TOKEN", "")

# Base de datos temporal en memoria
datos_globales = {
    "balance": 0.0,
    "pnl_dia": 0.0,
    "pnl_total_ciclo": 0.0,
    "estado": "offline",
    "wins": 0,
    "losses": 0,
    "extra": {},
    "last_update": None
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
    datos_globales["last_update"] = "online"
    return {"status": "ok"}

@app.get("/api/crash1000/status")
async def get_status():
    return datos_globales
