"""
Backend FastAPI para ArbitragePool — Crash 1000 + Inversores.

Endpoints Crash 1000 (ya existian):
    POST /api/crash1000/update   <- lo llama crash1000_monitor.py (requiere token)
    GET  /api/crash1000/status   <- lo llama el frontend (publico)

Endpoints de Inversores (nuevos):
    GET  /api/investors/{wallet}            <- estado de un inversor (publico)
    POST /api/investors/{wallet}/deposit     <- registrar deposito (publico)
    POST /api/investors/{wallet}/withdraw    <- solicitar retiro (publico)
    POST /api/investors/{wallet}/accrue      <- aplicar rendimiento diario (publico)
    GET  /api/investors                      <- listado completo (requiere token admin)

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

IMPORTANTE: el filesystem de Railway es efimero. Si el servicio se reinicia
o redeploya, se pierde lo guardado en disco (igual que ya pasaba con el
estado del bot). Para produccion real con dinero de terceros, migrar esto
a un volumen persistente de Railway o a una base de datos (Postgres) es
el siguiente paso recomendado.
"""

import os
import json
import time
from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any

app = FastAPI(title="ArbitragePool Backend")

# Permite que el frontend (cualquier dominio) consulte el status.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "crash1000_state.json")
INVESTORS_FILE = os.path.join(DATA_DIR, "investors_state.json")
TOKEN = os.getenv("CRASH1000_TOKEN", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", TOKEN)  # si no defines uno aparte, usa el mismo
OFFLINE_AFTER_SEG = 60  # si no hay update en 60s, se reporta offline


# ────────────────────────────────────────────────────────────
#  Crash 1000 (bot en vivo) — sin cambios funcionales
# ────────────────────────────────────────────────────────────
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
    data["last_update_iso"] = datetime.now(timezone.utc).isoformat()

    balance_inicio_dia = calcular_balance_inicio_dia(payload.balance, payload.pnl_dia)
    if balance_inicio_dia > 0:
        retorno_pct_real = (payload.pnl_dia / balance_inicio_dia) * 100
    else:
        retorno_pct_real = 0.0

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


# ────────────────────────────────────────────────────────────
#  Inversores — nuevo
# ────────────────────────────────────────────────────────────
class DepositPayload(BaseModel):
    amount: float
    tx_hash: Optional[str] = None  # opcional por ahora; mas adelante para verificacion on-chain


def leer_inversores() -> dict:
    if not os.path.exists(INVESTORS_FILE):
        return {}
    with open(INVESTORS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_inversores(data: dict):
    with open(INVESTORS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def investor_default() -> dict:
    return {
        "capital": 0.0,
        "profit_accum": 0.0,
        "joined_at": None,
        "last_accrual_day": None,
        "deposits": [],      # historial simple de depositos
        "withdraw_requests": [],  # historial simple de solicitudes de retiro
    }


def hoy_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@app.get("/api/investors/{wallet}")
def get_investor(wallet: str):
    wallet = wallet.lower()
    inversores = leer_inversores()
    inv = inversores.get(wallet, investor_default())
    return inv


@app.post("/api/investors/{wallet}/deposit")
def deposit(wallet: str, payload: DepositPayload):
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a 0")

    wallet = wallet.lower()
    inversores = leer_inversores()
    inv = inversores.get(wallet, investor_default())

    if inv["joined_at"] is None:
        inv["joined_at"] = datetime.now(timezone.utc).isoformat()

    inv["capital"] += payload.amount
    inv["deposits"].append({
        "amount": payload.amount,
        "tx_hash": payload.tx_hash,
        "ts": datetime.now(timezone.utc).isoformat(),
    })

    inversores[wallet] = inv
    guardar_inversores(inversores)
    return inv


@app.post("/api/investors/{wallet}/withdraw")
def withdraw(wallet: str):
    wallet = wallet.lower()
    inversores = leer_inversores()
    inv = inversores.get(wallet, investor_default())

    if inv["capital"] <= 0 and inv["profit_accum"] <= 0:
        raise HTTPException(status_code=400, detail="No hay capital registrado para retirar")

    monto_solicitado = inv["capital"] + inv["profit_accum"]
    inv["withdraw_requests"].append({
        "amount": monto_solicitado,
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    })

    # Se resetea el balance sintetico; el pago real se procesa manualmente
    # desde la wallet de operacion, como ya haces hoy.
    inv["capital"] = 0.0
    inv["profit_accum"] = 0.0
    inv["last_accrual_day"] = None

    inversores[wallet] = inv
    guardar_inversores(inversores)
    return inv


@app.post("/api/investors/{wallet}/accrue")
def accrue(wallet: str):
    """Aplica el rendimiento diario del bot a este inversor (una vez por dia)."""
    wallet = wallet.lower()
    inversores = leer_inversores()
    inv = inversores.get(wallet, investor_default())

    bot_state = leer_estado()
    daily_pct = bot_state.get("daily_return_pct_investor", 0.0)

    today = hoy_str()
    if inv["capital"] > 0 and inv["last_accrual_day"] != today:
        inv["profit_accum"] += inv["capital"] * (daily_pct / 100)
        inv["last_accrual_day"] = today

    inversores[wallet] = inv
    guardar_inversores(inversores)
    return inv


@app.get("/api/investors")
def list_investors(x_token: str = Header(default="")):
    """Listado completo para ti (admin). Requiere token."""
    if ADMIN_TOKEN and x_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token invalido")
    inversores = leer_inversores()

    total_capital = sum(i.get("capital", 0.0) for i in inversores.values())
    total_profit = sum(i.get("profit_accum", 0.0) for i in inversores.values())

    return {
        "count": len(inversores),
        "total_capital": total_capital,
        "total_profit_accum": total_profit,
        "investors": inversores,
    }


@app.get("/")
def root():
    return {"service": "arbitragepool-backend", "status": "running"}
