"""
Backend FastAPI para ArbitragePool — Crash 1000 + Inversores.
Version migrada a PostgreSQL (Neon) — reemplaza los archivos JSON
por una base de datos real, para que ningun dato se pierda ante
reinicios o redeploys de Railway.

Endpoints Crash 1000 (sin cambios funcionales):
    POST /api/crash1000/update   <- lo llama crash1000_monitor.py (requiere token)
    GET  /api/crash1000/status   <- lo llama el frontend (publico)

Endpoints de Inversores (sin cambios funcionales):
    GET  /api/investors/{wallet}            <- estado de un inversor (publico)
    POST /api/investors/{wallet}/deposit     <- registrar deposito (publico)
    POST /api/investors/{wallet}/withdraw    <- solicitar retiro (publico)
    POST /api/investors/{wallet}/accrue      <- aplicar rendimiento diario (publico)
    GET  /api/investors                      <- listado completo (requiere token admin)

Deploy en Railway:
    1. Sube este archivo (main.py) y el requirements.txt a tu repo de GitHub.
       requirements.txt debe incluir: fastapi, uvicorn, pydantic, psycopg2-binary
    2. En Railway, agrega la variable de entorno:
         CRASH1000_TOKEN   = (tu token secreto existente)
         DATABASE_URL      = (la connection string que te da Neon, algo como
                               postgresql://usuario:password@host/dbname)
    3. Railway redeploya automaticamente al detectar el push a GitHub.
    4. Al arrancar, este archivo crea las tablas solas si no existen
       (no hace falta correr nada manual la primera vez).

NOTA IMPORTANTE: ya no se usa el volumen persistente de Railway ni los
archivos JSON. Todo el estado (bot y inversores) vive ahora en la base
de datos de Neon, que sobrevive a cualquier reinicio/redeploy de Railway.
"""

import os
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import psycopg2
import psycopg2.extras

app = FastAPI(title="ArbitragePool Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL", "")
TOKEN = os.getenv("CRASH1000_TOKEN", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", TOKEN)
OFFLINE_AFTER_SEG = 60

if not DATABASE_URL:
    raise RuntimeError(
        "Falta la variable de entorno DATABASE_URL. "
        "Configúrala en Railway con la connection string de Neon."
    )


# ────────────────────────────────────────────────────────────
#  Conexion y creacion de tablas
# ────────────────────────────────────────────────────────────
@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                balance DOUBLE PRECISION,
                pnl_dia DOUBLE PRECISION,
                pnl_total_ciclo DOUBLE PRECISION,
                estado TEXT,
                wins INTEGER,
                losses INTEGER,
                extra JSONB,
                daily_return_pct_real DOUBLE PRECISION,
                daily_return_pct_investor DOUBLE PRECISION,
                last_update_ts DOUBLE PRECISION,
                last_update_iso TEXT,
                CONSTRAINT single_row CHECK (id = 1)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS investors (
                wallet TEXT PRIMARY KEY,
                capital DOUBLE PRECISION NOT NULL DEFAULT 0,
                profit_accum DOUBLE PRECISION NOT NULL DEFAULT 0,
                joined_at TEXT,
                last_accrual_day TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS investor_deposits (
                id SERIAL PRIMARY KEY,
                wallet TEXT NOT NULL REFERENCES investors(wallet),
                amount DOUBLE PRECISION NOT NULL,
                tx_hash TEXT,
                ts TEXT NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS investor_withdrawals (
                id SERIAL PRIMARY KEY,
                wallet TEXT NOT NULL REFERENCES investors(wallet),
                amount DOUBLE PRECISION NOT NULL,
                ts TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            );
        """)
        cur.close()


init_db()


# ────────────────────────────────────────────────────────────
#  Crash 1000 (bot en vivo)
# ────────────────────────────────────────────────────────────
class UpdatePayload(BaseModel):
    balance: float
    pnl_dia: float
    pnl_total_ciclo: float
    estado: str
    wins: int
    losses: int
    extra: Optional[Dict[str, Any]] = None


DAILY_RETURN_CAP_PCT = 2.0


def calcular_balance_inicio_dia(balance_actual: float, pnl_dia: float) -> float:
    return balance_actual - pnl_dia


@app.post("/api/crash1000/update")
def update(payload: UpdatePayload, x_token: str = Header(default="")):
    if TOKEN and x_token != TOKEN:
        raise HTTPException(status_code=401, detail="Token invalido")

    balance_inicio_dia = calcular_balance_inicio_dia(payload.balance, payload.pnl_dia)
    retorno_pct_real = (payload.pnl_dia / balance_inicio_dia) * 100 if balance_inicio_dia > 0 else 0.0
    retorno_pct_investor = min(retorno_pct_real, DAILY_RETURN_CAP_PCT)

    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bot_state (id, balance, pnl_dia, pnl_total_ciclo, estado, wins, losses,
                                    extra, daily_return_pct_real, daily_return_pct_investor,
                                    last_update_ts, last_update_iso)
            VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                balance = EXCLUDED.balance,
                pnl_dia = EXCLUDED.pnl_dia,
                pnl_total_ciclo = EXCLUDED.pnl_total_ciclo,
                estado = EXCLUDED.estado,
                wins = EXCLUDED.wins,
                losses = EXCLUDED.losses,
                extra = EXCLUDED.extra,
                daily_return_pct_real = EXCLUDED.daily_return_pct_real,
                daily_return_pct_investor = EXCLUDED.daily_return_pct_investor,
                last_update_ts = EXCLUDED.last_update_ts,
                last_update_iso = EXCLUDED.last_update_iso;
        """, (
            payload.balance, payload.pnl_dia, payload.pnl_total_ciclo, payload.estado,
            payload.wins, payload.losses, psycopg2.extras.Json(payload.extra) if payload.extra else None,
            retorno_pct_real, retorno_pct_investor, now_ts, now_iso
        ))
        cur.close()

    return {"ok": True}


@app.get("/api/crash1000/status")
def status():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM bot_state WHERE id = 1;")
        row = cur.fetchone()
        cur.close()

    if not row:
        return {"online": False, "balance": None, "wins": 0, "losses": 0}

    online = (time.time() - (row.get("last_update_ts") or 0)) < OFFLINE_AFTER_SEG
    return {
        "online": online,
        "balance": row.get("balance"),
        "pnl_dia": row.get("pnl_dia"),
        "pnl_total_ciclo": row.get("pnl_total_ciclo"),
        "estado": row.get("estado"),
        "wins": row.get("wins"),
        "losses": row.get("losses"),
        "daily_return_pct_investor": row.get("daily_return_pct_investor", 0.0),
        "last_update": row.get("last_update_iso"),
    }


# ────────────────────────────────────────────────────────────
#  Inversores
# ────────────────────────────────────────────────────────────
class DepositPayload(BaseModel):
    amount: float
    tx_hash: Optional[str] = None


def hoy_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def investor_default_dict(wallet: str) -> dict:
    return {
        "wallet": wallet,
        "capital": 0.0,
        "profit_accum": 0.0,
        "joined_at": None,
        "last_accrual_day": None,
        "deposits": [],
        "withdraw_requests": [],
    }


def cargar_inversor_completo(cur, wallet: str) -> dict:
    """Lee el inversor + su historial de depositos/retiros. Devuelve default si no existe."""
    cur.execute("SELECT * FROM investors WHERE wallet = %s;", (wallet,))
    row = cur.fetchone()
    if not row:
        return investor_default_dict(wallet)

    inv = dict(row)

    cur.execute("SELECT amount, tx_hash, ts FROM investor_deposits WHERE wallet = %s ORDER BY id;", (wallet,))
    inv["deposits"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT amount, ts, status FROM investor_withdrawals WHERE wallet = %s ORDER BY id;", (wallet,))
    inv["withdraw_requests"] = [dict(r) for r in cur.fetchall()]

    return inv


def asegurar_inversor_existe(cur, wallet: str):
    cur.execute("""
        INSERT INTO investors (wallet, capital, profit_accum, joined_at, last_accrual_day)
        VALUES (%s, 0, 0, NULL, NULL)
        ON CONFLICT (wallet) DO NOTHING;
    """, (wallet,))


@app.get("/api/investors/{wallet}")
def get_investor(wallet: str):
    wallet = wallet.lower()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        inv = cargar_inversor_completo(cur, wallet)
        cur.close()
    return inv


@app.post("/api/investors/{wallet}/deposit")
def deposit(wallet: str, payload: DepositPayload):
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a 0")

    wallet = wallet.lower()
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        asegurar_inversor_existe(cur, wallet)

        cur.execute("""
            UPDATE investors
            SET capital = capital + %s,
                joined_at = COALESCE(joined_at, %s)
            WHERE wallet = %s;
        """, (payload.amount, now_iso, wallet))

        cur.execute("""
            INSERT INTO investor_deposits (wallet, amount, tx_hash, ts)
            VALUES (%s, %s, %s, %s);
        """, (wallet, payload.amount, payload.tx_hash, now_iso))

        inv = cargar_inversor_completo(cur, wallet)
        cur.close()

    return inv


@app.post("/api/investors/{wallet}/withdraw")
def withdraw(wallet: str):
    wallet = wallet.lower()
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT capital, profit_accum FROM investors WHERE wallet = %s;", (wallet,))
        row = cur.fetchone()

        if not row or (row["capital"] <= 0 and row["profit_accum"] <= 0):
            cur.close()
            raise HTTPException(status_code=400, detail="No hay capital registrado para retirar")

        monto_solicitado = row["capital"] + row["profit_accum"]

        cur.execute("""
            INSERT INTO investor_withdrawals (wallet, amount, ts, status)
            VALUES (%s, %s, %s, 'pending');
        """, (wallet, monto_solicitado, now_iso))

        cur.execute("""
            UPDATE investors
            SET capital = 0, profit_accum = 0, last_accrual_day = NULL
            WHERE wallet = %s;
        """, (wallet,))

        inv = cargar_inversor_completo(cur, wallet)
        cur.close()

    return inv


@app.post("/api/investors/{wallet}/accrue")
def accrue(wallet: str):
    """Aplica el rendimiento diario del bot a este inversor (una vez por dia)."""
    wallet = wallet.lower()
    today = hoy_str()

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        asegurar_inversor_existe(cur, wallet)

        cur.execute("SELECT daily_return_pct_investor FROM bot_state WHERE id = 1;")
        bot_row = cur.fetchone()
        daily_pct = bot_row["daily_return_pct_investor"] if bot_row else 0.0

        cur.execute("SELECT capital, last_accrual_day FROM investors WHERE wallet = %s;", (wallet,))
        row = cur.fetchone()

        if row and row["capital"] > 0 and row["last_accrual_day"] != today:
            incremento = row["capital"] * (daily_pct / 100)
            cur.execute("""
                UPDATE investors
                SET profit_accum = profit_accum + %s,
                    last_accrual_day = %s
                WHERE wallet = %s;
            """, (incremento, today, wallet))

        inv = cargar_inversor_completo(cur, wallet)
        cur.close()

    return inv


@app.get("/api/investors")
def list_investors(x_token: str = Header(default="")):
    """Listado completo para ti (admin). Requiere token."""
    if ADMIN_TOKEN and x_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token invalido")

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT wallet FROM investors;")
        wallets = [r["wallet"] for r in cur.fetchall()]

        inversores = {}
        total_capital = 0.0
        total_profit = 0.0
        for w in wallets:
            inv = cargar_inversor_completo(cur, w)
            inversores[w] = inv
            total_capital += inv.get("capital", 0.0)
            total_profit += inv.get("profit_accum", 0.0)

        cur.close()

    return {
        "count": len(inversores),
        "total_capital": total_capital,
        "total_profit_accum": total_profit,
        "investors": inversores,
    }


@app.get("/")
def root():
    return {"service": "arbitragepool-backend", "status": "running", "db": "postgresql"}
