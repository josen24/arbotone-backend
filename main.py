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
from web3 import Web3

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
#  Verificación on-chain de depósitos (BSC — USDT BEP-20)
# ────────────────────────────────────────────────────────────
BSC_RPC          = os.getenv("BSC_RPC", "https://bsc-dataseed1.binance.org/")
POOL_WALLET      = os.getenv("POOL_WALLET", "0xb1F9217BB122F9A35995a73c5C9274Bc80BeD7D4").lower()
USDT_BEP20_ADDR  = os.getenv("USDT_BEP20_ADDR", "0x55d398326f99059fF775485246999027B3197955")
MIN_CONFIRMACIONES = int(os.getenv("MIN_CONFIRMACIONES", "3"))
TOLERANCIA_MONTO_PCT = 0.02  # 2% de tolerancia entre lo declarado y lo real on-chain

# ABI mínimo: solo lo necesario para leer decimals() y el evento Transfer
ERC20_ABI_MINIMO = [
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]
TRANSFER_EVENT_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

_w3 = Web3(Web3.HTTPProvider(BSC_RPC))
_usdt_contract = _w3.eth.contract(address=Web3.to_checksum_address(USDT_BEP20_ADDR), abi=ERC20_ABI_MINIMO)
_usdt_decimals_cache = None


def _obtener_decimales_usdt() -> int:
    global _usdt_decimals_cache
    if _usdt_decimals_cache is None:
        _usdt_decimals_cache = _usdt_contract.functions.decimals().call()
    return _usdt_decimals_cache


def verificar_deposito_onchain(tx_hash: str, monto_declarado: float) -> dict:
    """Verifica en la blockchain de BSC que la transacción:
      1) existe y fue exitosa,
      2) tiene suficientes confirmaciones,
      3) es una transferencia de USDT (BEP-20) hacia la wallet del pool,
      4) el monto coincide (con tolerancia) con lo que el usuario declaró.
    Devuelve {"ok": bool, "motivo": str, "monto_real": float}. El monto_real
    (leído de la blockchain) es el que se acredita, nunca el que el usuario
    declaró — así no importa si el frontend manda un número incorrecto.
    """
    try:
        tx_hash = tx_hash.strip()
        if not tx_hash.startswith("0x") or len(tx_hash) != 66:
            return {"ok": False, "motivo": "El hash de transacción no tiene el formato correcto", "monto_real": 0.0}

        receipt = _w3.eth.get_transaction_receipt(tx_hash)
        if receipt is None:
            return {"ok": False, "motivo": "La transacción no se encontró en la blockchain", "monto_real": 0.0}

        if receipt.status != 1:
            return {"ok": False, "motivo": "La transacción existe pero falló en la blockchain", "monto_real": 0.0}

        bloque_actual = _w3.eth.block_number
        confirmaciones = bloque_actual - receipt.blockNumber
        if confirmaciones < MIN_CONFIRMACIONES:
            return {"ok": False, "motivo": f"Todavía no tiene suficientes confirmaciones ({confirmaciones}/{MIN_CONFIRMACIONES}). Probá de nuevo en un minuto.", "monto_real": 0.0}

        decimales = _obtener_decimales_usdt()
        usdt_addr_checksum = Web3.to_checksum_address(USDT_BEP20_ADDR)

        monto_real = 0.0
        encontrado = False
        for log in receipt.logs:
            if log.address.lower() != usdt_addr_checksum.lower():
                continue
            if len(log.topics) < 3 or log.topics[0].hex() != TRANSFER_EVENT_TOPIC:
                continue
            destino = "0x" + log.topics[2].hex()[-40:]
            if destino.lower() != POOL_WALLET.lower():
                continue
            valor_wei = int(log.data.hex(), 16) if isinstance(log.data, (bytes, bytearray)) else int(log.data, 16)
            monto_real = valor_wei / (10 ** decimales)
            encontrado = True
            break

        if not encontrado:
            return {"ok": False, "motivo": "La transacción no incluye una transferencia de USDT hacia la wallet del pool", "monto_real": 0.0}

        diferencia_pct = abs(monto_real - monto_declarado) / monto_declarado if monto_declarado > 0 else 1.0
        if diferencia_pct > TOLERANCIA_MONTO_PCT:
            return {"ok": False, "motivo": f"El monto declarado (${monto_declarado:.2f}) no coincide con el monto real on-chain (${monto_real:.2f})", "monto_real": monto_real}

        return {"ok": True, "motivo": "Verificado correctamente", "monto_real": monto_real}

    except Exception as e:
        return {"ok": False, "motivo": f"Error verificando la transacción: {e}", "monto_real": 0.0}


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
        # Restricción de unicidad: el mismo tx_hash no puede registrarse dos veces
        # (evita que un depósito real se reenvíe varias veces para cobrar de más).
        # Índice parcial: solo aplica a hashes no nulos.
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_deposits_txhash_unique
            ON investor_deposits (tx_hash)
            WHERE tx_hash IS NOT NULL;
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
        # Columnas para poder marcar un retiro como completado con prueba on-chain
        # (se agregan de forma segura si la tabla ya existía sin ellas).
        cur.execute("ALTER TABLE investor_withdrawals ADD COLUMN IF NOT EXISTS payout_tx_hash TEXT;")
        cur.execute("ALTER TABLE investor_withdrawals ADD COLUMN IF NOT EXISTS completed_ts TEXT;")
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
        "extra": row.get("extra"),
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

    cur.execute("SELECT id, amount, ts, status, payout_tx_hash, completed_ts FROM investor_withdrawals WHERE wallet = %s ORDER BY id;", (wallet,))
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

    if not payload.tx_hash:
        raise HTTPException(status_code=400, detail="Falta el hash de la transacción (tx_hash)")

    wallet = wallet.lower()
    tx_hash = payload.tx_hash.strip().lower()
    now_iso = datetime.now(timezone.utc).isoformat()
    today = hoy_str()

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Rechazar de entrada si este hash ya fue registrado por cualquier wallet
        cur.execute("SELECT wallet FROM investor_deposits WHERE tx_hash = %s;", (tx_hash,))
        ya_existe = cur.fetchone()
        if ya_existe:
            cur.close()
            raise HTTPException(status_code=409, detail="Este hash de transacción ya fue registrado anteriormente")

        cur.close()

    # Verificación real en la blockchain de BSC (fuera de la transacción de DB,
    # ya que puede tardar unos segundos en responder el nodo RPC)
    verificacion = verificar_deposito_onchain(tx_hash, payload.amount)
    if not verificacion["ok"]:
        raise HTTPException(status_code=400, detail=verificacion["motivo"])

    monto_verificado = verificacion["monto_real"]

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        asegurar_inversor_existe(cur, wallet)

        # IMPORTANTE: al depositar, marcamos last_accrual_day = hoy.
        # Esto evita que el rendimiento (positivo o negativo) generado
        # ANTES del deposito se le aplique al capital recien ingresado.
        # El inversor empieza a acumular rendimiento recien desde manana.
        cur.execute("""
            UPDATE investors
            SET capital = capital + %s,
                joined_at = COALESCE(joined_at, %s),
                last_accrual_day = %s
            WHERE wallet = %s;
        """, (monto_verificado, now_iso, today, wallet))

        try:
            cur.execute("""
                INSERT INTO investor_deposits (wallet, amount, tx_hash, ts)
                VALUES (%s, %s, %s, %s);
            """, (wallet, monto_verificado, tx_hash, now_iso))
        except psycopg2.errors.UniqueViolation:
            # Carrera improbable: alguien registró el mismo hash en el medio. Abortar limpio.
            conn.rollback()
            cur.close()
            raise HTTPException(status_code=409, detail="Este hash de transacción ya fue registrado (carrera detectada)")

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


class CompletarRetiroPayload(BaseModel):
    payout_tx_hash: str


@app.post("/api/investors/{wallet}/withdrawals/{withdrawal_id}/complete")
def completar_retiro(wallet: str, withdrawal_id: int, payload: CompletarRetiroPayload, x_token: str = Header(default="")):
    """Vos (admin) marcás un retiro como pagado, adjuntando el hash de la
    transacción real que enviaste desde tu wallet. El frontend puede
    entonces mostrarle al inversor una confirmación verificable, en vez
    de dejarlo con la duda de si el retiro se procesó o no."""
    if ADMIN_TOKEN and x_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token invalido")

    wallet = wallet.lower()
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE investor_withdrawals
            SET status = 'completed', payout_tx_hash = %s, completed_ts = %s
            WHERE id = %s AND wallet = %s;
        """, (payload.payout_tx_hash.strip(), now_iso, withdrawal_id, wallet))

        if cur.rowcount == 0:
            cur.close()
            raise HTTPException(status_code=404, detail="No se encontró ese retiro para esa wallet")

        inv = cargar_inversor_completo(cur, wallet)
        cur.close()

    return inv
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
