"""
Backend FastAPI para Crash 1000 — recibe datos del monitor local (MT5)
y los expone para que el frontend (dashboard de inversores) los lea.

Endpoints:
    POST /api/crash1000/update   <- lo llama crash1000_monitor.py (requiere token)
    GET  /api/crash1000/status   <- lo llama el frontend (publico)

Deploy en Railway:
    1. Sube este archivo (main.py) y el requirements.txt a un repo de GitHub.
    2. En Railway: New Project -> Deploy from GitHub repo.
    3. Railway detecta el Procfile/start command automaticamente con lo de abajo.
    4. En Railway, agrega la variable de entorno:
         CRASH1000_TOKEN = (elige un token secreto, ej: una cadena larga random)
    5. Cuando termine el deploy, Railway te da una URL publica tipo:
         https://tu-proyecto.up.railway.app
    6. En tu PC local, en crash1000_monitor.env, pon:
         CRASH1000_BACKEND_URL=https://tu-proyecto.up.railway.app/api/crash1000/update
         CRASH1000_TOKEN=el-mismo-token-que-pusiste-en-railway
"""

import os
import json
import time
from datetime import datetime
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any

app = FastAPI(title="Crash1000 Backend")

# Permite que el frontend (cualquier dominio) consulte el status.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATE_FILE = "crash1000_state.json"
TOKEN = os.getenv("CRASH1000_TOKEN", "")
OFFLINE_AFTER_SEG = 60  # si no hay update en 60s, se reporta offline


class UpdatePayload(BaseModel):
    balance: float
    pnl_dia: float
    pnl_total_ciclo: float
    estado: str
    wins: int
    losses: int
    extra: Optional[Dict[str, Any]] = None


DAILY_RETURN_CAP_PCT = 2.0  # tope maximo que se le muestra al inversor por dia (no limita la baja)


def calcular_balance_inicio_dia(balance_actual: float, pnl_dia: float) -> float:
    # balance al abrir el dia = balance actual menos lo ganado/perdido hoy
    return balance_actual - pnl_dia


def leer_estado() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_estado(data: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


@app.post("/api/crash1000/update")
def update(payload: UpdatePayload, x_token: str = Header(default="")):
    if TOKEN and x_token != TOKEN:
        raise HTTPException(status_code=401, detail="Token invalido")

    data = payload.dict()
    data["last_update_ts"] = time.time()
    data["last_update_iso"] = datetime.utcnow().isoformat()

    balance_inicio_dia = calcular_balance_inicio_dia(payload.balance, payload.pnl_dia)
    if balance_inicio_dia > 0:
        retorno_pct_real = (payload.pnl_dia / balance_inicio_dia) * 100
    else:
        retorno_pct_real = 0.0

    # El inversor ve como maximo +2% aunque el bot haya ganado mas ese dia,
    # pero si el bot perdio, el inversor ve la perdida real (sin tope hacia abajo).
    data["daily_return_pct_real"] = retorno_pct_real
    data["daily_return_pct_investor"] = min(retorno_pct_real, DAILY_RETURN_CAP_PCT)

    guardar_estado(data)
    return {"ok": True}


@app.get("/api/crash1000/status")
def status():
    data = leer_estado()
    if not data:
        return {"online": False, "balance": None, "wins": 0, "losses": 0}

    online = (time.time() - data.get("last_update_ts", 0)) < OFFLINE_AFTER_SEG
    return {
        "online": online,
        "balance": data.get("balance"),
        "pnl_dia": data.get("pnl_dia"),
        "pnl_total_ciclo": data.get("pnl_total_ciclo"),
        "estado": data.get("estado"),
        "wins": data.get("wins"),
        "losses": data.get("losses"),
        "daily_return_pct_investor": data.get("daily_return_pct_investor", 0.0),
        "last_update": data.get("last_update_iso"),
    }


@app.get("/")
def root():
    return {"service": "crash1000-backend", "status": "running"}
