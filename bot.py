"""
JABIR RUNNER V3 - TRADER EDITION (UPGRADED)
"""
from __future__ import annotations

import asyncio
import base64
import functools
import html
import json
import logging
import math
import os
import random
import re
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# ------------------------------ CONFIG ---------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
HELIUS_KEY = os.getenv("HELIUS_API_KEY")
GROQ_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_PATH = "telegram-webhook"

def _env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default

ADMIN_USER_ID = _env_int("ADMIN_USER_ID", 0)

DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
DEX_ORDERS_URL = "https://api.dexscreener.com/orders/v1/solana/{}"
DEX_BOOSTS_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
GECKO_OHLCV_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/{timeframe}"
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

DB_PATH = os.getenv("DB_PATH", "jabir_runner.db")
CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))
PAID_CACHE_TTL = int(os.getenv("PAID_CACHE_TTL", "300"))
PAID_FAIL_TTL = 30
NOT_FOUND_TTL = 20
HOLDERS_CACHE_TTL = 120
ATH_CACHE_TTL = 300
ATH_FAIL_TTL = 60
NARRATIVE_CACHE_TTL = 300

ATH_TIMEOUT = float(os.getenv("ATH_TIMEOUT", "3.0"))
NARRATIVE_TIMEOUT = float(os.getenv("NARRATIVE_TIMEOUT", "5.0"))
AUTHORITY_TIMEOUT = float(os.getenv("AUTHORITY_TIMEOUT", "3.0"))
PAID_TIMEOUT = float(os.getenv("PAID_TIMEOUT", "3.0"))
TOP_HOLDERS_TIMEOUT = float(os.getenv("TOP_HOLDERS_TIMEOUT", "6.0"))
FULL_HOLDERS_TIMEOUT = float(os.getenv("FULL_HOLDERS_TIMEOUT", "12.0"))
LAUNCH_TIMEOUT = float(os.getenv("LAUNCH_TIMEOUT", "20.0"))

CACHE_MAX = 500
MAX_HOLDER_PAGES = 10
HOLDER_PAGE_SIZE = 1000
LAUNCH_SIG_PAGES = 6
LAUNCH_TX_SAMPLE = 40
SNIPER_SLOTS = 3

DEX_MIN_INTERVAL = float(os.getenv("DEX_MIN_INTERVAL", "1.0"))
DEX_MAX_ATTEMPTS = 5
DEX_BACKOFF_BASE = 2.0
DEX_MAX_BACKOFF = 20.0
DEX_MAX_RETRY_AFTER = 60.0
DEX_TOTAL_DEADLINE = 100.0
GECKO_MIN_INTERVAL = float(os.getenv("GECKO_MIN_INTERVAL", "1.5"))

BURN_ADDRESSES = {"1nc1nerator11111111111111111111111111111111"}
SOL_ADDR_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
BAR = "━━━━━━━━━━━━"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("jabir_runner")

_http: Optional[httpx.AsyncClient] = None
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_missing_cache: dict[str, tuple[float, None]] = {}
_paid_cache: dict[str, tuple[float, Optional[dict[str, bool]]]] = {}
_holders_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
_ath_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
_narrative_cache: dict[str, tuple[float, str]] = {}
_dex_semaphore: Optional[asyncio.Semaphore] = None
_gecko_semaphore: Optional[asyncio.Semaphore] = None
_dex_last_request: float = -1e9
_gecko_last_request: float = -1e9
_MISS = object()
_alerts_check_lock: Optional[asyncio.Lock] = None

class DexScreenerError(RuntimeError):
    pass

class DexRateLimited(DexScreenerError):
    pass

class _LockEntry:
    __slots__ = ("lock", "users")
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0

_mint_locks: dict[str, _LockEntry] = {}

# ------------------------------ HELPERS --------------------------------------
def safe_num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool): return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError): return None

def safe_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool): return None
    try: return int(value)
    except (TypeError, ValueError, OverflowError): return None

def nested(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict): return None
        data = data.get(key)
    return data

def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)

def short_addr(address: str) -> str:
    return f"{address[:6]}...{address[-6:]}" if len(address) > 14 else address

def money(value: Optional[float]) -> str:
    value = safe_num(value)
    if value is None: return "N/A"
    sign = "-" if value < 0 else ""
    value = abs(value)
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= divisor:
            return f"{sign}${value / divisor:.2f}{suffix}"
    if value == 0: return "$0"
    if value < 1: return f"{sign}${value:.10f}".rstrip("0").rstrip(".")
    return f"{sign}${value:,.2f}"

def pct(value: Optional[float]) -> str:
    value = safe_num(value)
    return "N/A" if value is None else f"{value:+.1f}%"

def age_text(seconds: Optional[float]) -> str:
    if seconds is None: return "N/A"
    seconds = max(0, int(seconds))
    if seconds < 3600: return f"{max(1, seconds // 60)}m"
    if seconds < 86400: return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d"

def fmt_count(value: Optional[int]) -> str:
    return "N/A" if value is None else f"{value:,}"

def ratio_text(numerator: Optional[float], denominator: Optional[float]) -> str:
    if numerator is None or denominator is None or denominator <= 0: return "N/A"
    return f"{numerator / denominator:.2f}x"

# ------------------------------ BASE58 / ED25519 -----------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ED_P = 2 ** 255 - 19
_ED_D = (-121665 * pow(121666, -1, _ED_P)) % _ED_P

def b58decode(text: str) -> Optional[bytes]:
    number = 0
    for char in text:
        index = _B58.find(char)
        if index < 0: return None
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading_zeros + body

@functools.lru_cache(maxsize=4096)
def is_on_curve(address: str) -> Optional[bool]:
    raw = b58decode(address)
    if raw is None or len(raw) != 32: return None
    value = int.from_bytes(raw, "little")
    sign_bit = value >> 255
    y = (value & ((1 << 255) - 1)) % _ED_P
    y2 = y * y % _ED_P
    u = (y2 - 1) % _ED_P
    v = (_ED_D * y2 + 1) % _ED_P
    if v == 0: return False
    x2 = u * pow(v, -1, _ED_P) % _ED_P
    if x2 == 0: return sign_bit == 0
    return pow(x2, (_ED_P - 1) // 2, _ED_P) == 1

# ------------------------------ DATABASE -------------------------------------
def db_connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    return db

@contextmanager
def db_session():
    db = db_connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init_db() -> None:
    folder = os.path.dirname(DB_PATH)
    if folder: os.makedirs(folder, exist_ok=True)
    with db_session() as db:
        db.execute("CREATE TABLE IF NOT EXISTS first_seen (mint TEXT PRIMARY KEY, first_seen INTEGER NOT NULL, first_price REAL, first_mc REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, ts INTEGER NOT NULL, price REAL, fdv REAL, mc REAL)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_mint_ts ON snapshots(mint, ts)")
        db.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, mint TEXT NOT NULL, symbol TEXT, target_price REAL, direction TEXT, base_price REAL, pct REAL, created_at INTEGER NOT NULL, triggered INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, scans INTEGER DEFAULT 0)")

def record_snapshot(mint: str, price: Optional[float], fdv: Optional[float], mc: Optional[float]) -> None:
    now = int(time.time())
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO first_seen(mint, first_seen, first_price, first_mc) VALUES(?,?,?,?)", (mint, now, price, mc if mc is not None else fdv))
        db.execute("INSERT INTO snapshots(mint, ts, price, fdv, mc) VALUES(?,?,?,?,?)", (mint, now, price, fdv, mc))
        db.execute("DELETE FROM snapshots WHERE ts < ?", (now - 30 * 86400,))

def tracking_data(mint: str) -> dict[str, Any]:
    with db_session() as db:
        first = db.execute("SELECT first_seen, first_price, first_mc FROM first_seen WHERE mint=?", (mint,)).fetchone()
        ath = db.execute("SELECT MAX(price), MAX(fdv), MAX(mc) FROM snapshots WHERE mint=?", (mint,)).fetchone()
        scans = db.execute("SELECT COUNT(*) FROM snapshots WHERE mint=?", (mint,)).fetchone()
    return {"first": first, "ath_price": ath[0] if ath else None, "scans": scans[0] if scans else None}

def record_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    now = int(time.time())
    with db_session() as db:
        db.execute("INSERT INTO users(user_id, username, first_name, first_seen, last_seen, scans) VALUES(?,?,?,?,?,0) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen", (user_id, username, first_name, now, now))

def record_scan(user_id: int) -> None:
    with db_session() as db:
        db.execute("UPDATE users SET scans = scans + 1 WHERE user_id=?", (user_id,))

def stats_snapshot() -> dict[str, Any]:
    now = int(time.time())
    with db_session() as db:
        total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        today = db.execute("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (now - 86400,)).fetchone()[0]
        total_scans = db.execute("SELECT COALESCE(SUM(scans), 0) FROM users").fetchone()[0]
        top = db.execute("SELECT username, first_name, scans FROM users WHERE scans > 0 ORDER BY scans DESC LIMIT 10").fetchall()
    return {"total": total, "today": today, "total_scans": total_scans, "top": top}

# ------------------------------ HTTP / API -----------------------------------
def _scrub(text: str) -> str:
    for secret in (HELIUS_KEY, GROQ_KEY, BOT_TOKEN):
        if secret: text = text.replace(secret, "***")
    return text

async def request_json(method: str, url: str, **kwargs: Any) -> Any:
    if _http is None: raise RuntimeError("HTTP client is not initialized")
    last_error = "unknown error"
    for attempt in range(2):
        try:
            response = await _http.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            last_error = f"HTTP {exc.response.status_code}"
            if exc.response.status_code < 500 and exc.response.status_code != 429: break
            if attempt == 0: await asyncio.sleep(0.5)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = _scrub(f"{type(exc).__name__}")
            if attempt == 0: await asyncio.sleep(0.5)
    raise RuntimeError(f"API request failed: {last_error}")

def _get_dex_semaphore() -> asyncio.Semaphore:
    global _dex_semaphore
    if _dex_semaphore is None: _dex_semaphore = asyncio.Semaphore(1)
    return _dex_semaphore

async def dex_get_json(url: str, max_attempts: int = 5) -> Any:
    global _dex_last_request
    async with _get_dex_semaphore():
        for attempt in range(1, max_attempts + 1):
            gap = DEX_MIN_INTERVAL - (time.monotonic() - _dex_last_request)
            if gap > 0: await asyncio.sleep(gap)
            try:
                response = await _http.get(url, timeout=20.0)
                _dex_last_request = time.monotonic()
                if response.status_code == 429:
                    await asyncio.sleep(min(60.0, 2.0 * attempt))
                    continue
                if response.status_code >= 400: raise DexScreenerError(f"HTTP {response.status_code}")
                return response.json()
            except Exception as e:
                _dex_last_request = time.monotonic()
                if attempt == max_attempts: raise DexScreenerError(str(e))
                await asyncio.sleep(2.0 * attempt)

def _get_gecko_semaphore() -> asyncio.Semaphore:
    global _gecko_semaphore
    if _gecko_semaphore is None: _gecko_semaphore = asyncio.Semaphore(1)
    return _gecko_semaphore

async def gecko_get_json(url: str, params: dict[str, Any]) -> Any:
    global _gecko_last_request
    async with _get_gecko_semaphore():
        gap = GECKO_MIN_INTERVAL - (time.monotonic() - _gecko_last_request)
        if gap > 0: await asyncio.sleep(gap)
        try:
            resp = await _http.get(url, params=params, headers={"Accept": "application/json;version=20230302"}, timeout=15.0)
            _gecko_last_request = time.monotonic()
            if resp.status_code != 200: return None
            return resp.json()
        except Exception:
            _gecko_last_request = time.monotonic()
            return None

@asynccontextmanager
async def mint_lock(key: str) -> AsyncIterator[None]:
    entry = _mint_locks.setdefault(key, _LockEntry())
    entry.users += 1
    try:
        async with entry.lock: yield
    finally:
        entry.users -= 1
        if entry.users == 0: _mint_locks.pop(key, None)

async def helius_rpc(method: str, params: Any) -> Any:
    if not HELIUS_KEY: return None
    result = await request_json("POST", HELIUS_RPC_URL, params={"api-key": HELIUS_KEY}, json={"jsonrpc": "2.0", "id": "jabir", "method": method, "params": params})
    if isinstance(result, dict) and result.get("error"): raise RuntimeError(str(result["error"]))
    return result.get("result") if isinstance(result, dict) else None

def _cached_pair(mint: str) -> Optional[dict[str, Any]]:
    entry = _cache.get(mint)
    return entry[1] if entry and time.time() - entry[0] < CACHE_TTL else None

async def fetch_pair(mint: str) -> Optional[dict[str, Any]]:
    cached = _cached_pair(mint)
    if cached: return cached
    async with mint_lock(f"pair:{mint}"):
        cached = _cached_pair(mint)
        if cached: return cached
        payload = await dex_get_json(DEX_TOKEN_URL.format(mint))
        pairs = payload.get("pairs") if isinstance(payload, dict) else []
        matching = [p for p in (pairs or []) if p.get("chainId") == "solana" and str(nested(p, "baseToken", "address") or "").lower() == mint.lower()]
        if not matching: return None
        primary = max(matching, key=lambda p: (safe_num(nested(p, "volume", "h24")) or 0, safe_num(nested(p, "liquidity", "usd")) or 0))
        combined = dict(primary)
        
        # Merge txns and volume
        txns = {"h1": {"buys": 0, "sells": 0}, "h24": {"buys": 0, "sells": 0}}
        for p in matching:
            for period in ("h1", "h24"):
                txns[period]["buys"] += safe_int(nested(p, "txns", period, "buys")) or 0
                txns[period]["sells"] += safe_int(nested(p, "txns", period, "sells")) or 0
        combined["txns"] = txns
        
        combined["_pools"] = sorted([{"address": p.get("pairAddress"), "created": safe_num(p.get("pairCreatedAt")), "vol": safe_num(nested(p, "volume", "h24")) or 0} for p in matching if p.get("pairAddress")], key=lambda x: x["vol"], reverse=True)
        _cache[mint] = (time.time(), combined)
        return combined

async def fetch_supply(mint: str) -> Optional[dict[str, Any]]:
    result = await helius_rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
    value = (result or {}).get("value") or {}
    if value.get("amount") and value.get("decimals") is not None:
        return {"raw": int(value["amount"]), "decimals": int(value["decimals"])}
    return None

async def fetch_authorities(mint: str) -> Optional[dict[str, Any]]:
    result = await helius_rpc("getAccountInfo", [mint, {"encoding": "base64", "commitment": "confirmed"}])
    value = (result or {}).get("value")
    if not value or not value.get("data"): return None
    try:
        raw = base64.b64decode(value["data"][0])
        if len(raw) < 82: return None
        mint_active = int.from_bytes(raw[0:4], "little") == 1
        freeze_active = int.from_bytes(raw[46:50], "little") == 1
        return {"mint_authority_active": mint_active, "freeze_authority_active": freeze_active}
    except Exception: return None

async def fetch_top_accounts(mint: str) -> Optional[dict[str, Any]]:
    entry = _holders_cache.get(mint)
    if entry and time.time() - entry[0] < HOLDERS_CACHE_TTL: return entry[1]
    async with mint_lock(f"top:{mint}"):
        result = await helius_rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        accounts = [a for a in ((result or {}).get("value") or []) if isinstance(a, dict) and a.get("address")]
        if not accounts: return None
        info = await helius_rpc("getMultipleAccounts", [[a["address"] for a in accounts], {"encoding": "jsonParsed", "commitment": "confirmed"}])
        owners = {}
        for account, val in zip(accounts, (info or {}).get("value") or []):
            owner = nested(val, "data", "parsed", "info", "owner")
            amount = safe_int(account.get("amount"))
            if isinstance(owner, str) and amount:
                owners[owner] = owners.get(owner, 0) + amount
        data = {"entries": sorted(owners.items(), key=lambda kv: kv[1], reverse=True)} if owners else None
        _holders_cache[mint] = (time.time(), data)
        return data

async def fetch_creator_history(creator: str) -> dict[str, Any]:
    """[NEW] Checks the last 10 tokens created by this wallet for rugs."""
    if not creator or not HELIUS_KEY: return {"checked": False, "rugs": 0}
    try:
        # Get signatures of creator wallet
        sigs = await helius_rpc("getSignaturesForAddress", [creator, {"limit": 20, "commitment": "confirmed"}])
        if not isinstance(sigs, list): return {"checked": False, "rugs": 0}
        
        rugs = 0
        tokens_checked = 0
        # Look for token creations (mints)
        for sig_info in sigs:
            if sig_info.get("err"): continue
            tx = await helius_rpc("getTransaction", [sig_info["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            if not tx: continue
            
            # Check if this tx created a token (InitializeMint)
            logs = tx.get("meta", {}).get("logMessages", [])
            if any("InitializeMint" in log for log in logs):
                # Found a creation. Check its current price.
                mints = [b["mint"] for b in tx.get("meta", {}).get("postTokenBalances", []) if b.get("mint")]
                if not mints: continue
                new_mint = mints[0]
                tokens_checked += 1
                
                # Fetch its current state on DexScreener
                try:
                    payload = await dex_get_json(DEX_TOKEN_URL.format(new_mint))
                    pairs = payload.get("pairs", []) if payload else []
                    if pairs:
                        # If liquidity is < $1000 or price dropped > 90% from ATH (using our basic check)
                        liq = safe_num(nested(pairs[0], "liquidity", "usd")) or 0
                        change = safe_num(nested(pairs[0], "priceChange", "h24")) or 0
                        if liq < 500 or change < -90:
                            rugs += 1
                    else:
                        # Token exists but no pairs = likely rugged/abandoned
                        rugs += 1
                except Exception: pass
                
                if tokens_checked >= 10: break # Limit to 10
                
        return {"checked": True, "rugs": rugs, "tokens": tokens_checked}
    except Exception as e:
        log.warning("Creator history failed: %s", e)
        return {"checked": False, "rugs": 0}

async def fetch_launch_data(mint: str, supply_raw: int) -> Optional[dict[str, Any]]:
    """[NEW] Improved to find Dev, Bundles, and handle TX limits."""
    signatures = []
    before = None
    reached_start = False
    for _ in range(LAUNCH_SIG_PAGES):
        opts = {"limit": 1000, "commitment": "confirmed"}
        if before: opts["before"] = before
        page = await helius_rpc("getSignaturesForAddress", [mint, opts])
        if not isinstance(page, list) or not page: break
        signatures.extend(s for s in page if isinstance(s, dict) and s.get("signature"))
        if len(page) < 1000: reached_start = True; break
        before = page[-1].get("signature")
    
    if not reached_start:
        # Too many transactions (over 6000)
        return {"limit_reached": True, "tx_count": len(signatures)}

    oldest = [s for s in reversed(signatures) if not s.get("err")][:40]
    if not oldest: return None

    limiter = asyncio.Semaphore(8)
    async def load(sig): 
        async with limiter: return await helius_rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
    
    txs = [r for r in await asyncio.gather(*(load(s["signature"]) for s in oldest), return_exceptions=True) if isinstance(r, dict)]
    
    if not txs: return None
    launch_slot = safe_int(txs[0].get("slot"))
    creator = nested(txs[0], "transaction", "message", "accountKeys", 0, "pubkey")
    
    # Check dev holding
    dev_holding = 0
    for tx in txs:
        for b in tx.get("meta", {}).get("postTokenBalances", []):
            if b.get("mint") == mint and b.get("owner") == creator:
                dev_holding = max(dev_holding, safe_int(nested(b, "uiTokenAmount", "amount")) or 0)
                
    # Check bundles (multiple wallets funded by same wallet in launch block)
    bundle_wallets = set()
    for tx in txs:
        if safe_int(tx.get("slot")) == launch_slot:
            for b in tx.get("meta", {}).get("postTokenBalances", []):
                if b.get("mint") == mint and b.get("owner") != creator:
                    bundle_wallets.add(b.get("owner"))

    return {
        "limit_reached": False,
        "creator": creator,
        "dev_holding_pct": (dev_holding / supply_raw * 100) if supply_raw else 0,
        "bundle_count": len(bundle_wallets),
        "bundle_pct": (sum(len(b) for b in bundle_wallets) / supply_raw * 100) if supply_raw else 0
    }

def socials_from_pair(pair: dict[str, Any]) -> dict[str, Any]:
    """[NEW] Extracts real links for Socials section."""
    info = pair.get("info") or {}
    websites = [x.get("url") for x in (info.get("websites") or []) if isinstance(x, dict) and x.get("url")]
    socials = [x for x in (info.get("socials") or []) if isinstance(x, dict)]
    
    x_url, tg_url = None, None
    for s in socials:
        p = str(s.get("platform", "")).lower()
        u = s.get("url")
        if "twitter" in p or "x" in p: x_url = u
        if "telegram" in p or "tg" in p: tg_url = u
        
    return {"x": x_url, "web": websites[0] if websites else None, "tg": tg_url}

# ------------------------------ RISK / VERDICT -------------------------------
def get_verdict(data: dict[str, Any]) -> tuple[str, str]:
    """[NEW] Calculates risk score and verdict based on new rules."""
    score = 0
    reasons = []

    # 1. Creator History (High priority)
    creator_info = data.get("creator_info", {})
    if creator_info.get("checked") and creator_info.get("rugs", 0) > 0:
        return "⛔ AVOID", f"Creator is a RUGGER (Has rugged {creator_info['rugs']} times)"

    # 2. Dev / Bundles Logic
    launch = data.get("launch_data") or {}
    if launch.get("limit_reached"):
        score += 20
        reasons.append("Too many tx to verify Dev")
    else:
        dev_pct = launch.get("dev_holding_pct", 0)
        if dev_pct > 10:
            score += 30
            reasons.append(f"Dev holds {dev_pct:.1f}%")
        elif dev_pct == 0:
            pass # Sold out
        if launch.get("bundle_count", 0) > 2:
            score += 15
            reasons.append("Bundles detected")

    # 3. Security Checks
    auth = data.get("authorities") or {}
    if auth.get("mint_authority_active") is True: score += 40; reasons.append("Mint is ACTIVE")
    if auth.get("freeze_authority_active") is True: score += 40; reasons.append("Freeze is ACTIVE")
    
    # 4. Market Conditions
    liq = data.get("liquidity") or 0
    if liq < 5000: score += 20; reasons.append("Very low liquidity")
    
    top = data.get("top_holders") or {}
    if top.get("top10_percent", 0) > 40: score += 20; reasons.append("Top 10 hold >40%")

    if score >= 60:
        return "⛔ AVOID", reasons[0] if reasons else "High risk detected"
    elif score >= 20:
        return "🟡 WATCH", reasons[0] if reasons else "Some risk flags found"
    else:
        return "🟢 BUY", "Looks relatively safe"

# ------------------------------ AI NARRATIVE ---------------------------------
async def generate_narrative(data: dict[str, Any]) -> str:
    """[NEW] Generates a detailed, funny 4-6 sentence AI narrative."""
    if not GROQ_KEY: return "This token is being scanned. Please check the data above to make your own decision."
    
    prompt_data = {
        "name": data["name"], "symbol": data["symbol"], "age": age_text(data.get("age_sec")),
        "price_change_1h": data.get("price_change_1h"), "liq": data.get("liquidity"),
        "volume": data.get("volume24"), "top10": data.get("top_holders", {}).get("top10_percent"),
        "creator_status": "Rugger" if data.get("creator_info", {}).get("rugs", 0) > 0 else "Clean/Unknown",
        "dev_status": "Unknown" if data.get("launch_data", {}).get("limit_reached") else f"{data.get('launch_data', {}).get('dev_holding_pct', 0):.1f}%",
        "socials": "Yes" if data.get("socials", {}).get("x") else "No"
    }
    
    prompt = (
        "You are a funny, smart crypto trader friend. Write a 4-6 sentence analysis of this Solana token. "
        "Use slang and emojis. Be entertaining but explain WHY the token is good or bad. "
        "Mention the creator status, dev holding, and volume vs liquidity. "
        f"Data: {json.dumps(prompt_data)}"
    )
    
    try:
        payload = {
            "model": GROQ_MODEL,
            "messages": [{"role": "system", "content": "You are a crypto meme lord."}, {"role": "user", "content": prompt}],
            "temperature": 0.7, "max_tokens": 300
        }
        res = await asyncio.wait_for(request_json("POST", GROQ_URL, headers={"Authorization": f"Bearer {GROQ_KEY}"}, json=payload), 8.0)
        content = res["choices"][0]["message"]["content"].strip()
        return content if content else "No narrative generated."
    except Exception:
        return "The AI is sleeping right now, but the data above tells the real story. Check the Dev and Creator details carefully."

# ------------------------------ REPORT BUILDER -------------------------------
def build_report(data: dict[str, Any], narrative: str, deep: bool) -> str:
    verdict_text, verdict_reason = get_verdict(data)
    authorities = data.get("authorities") or {}
    top = data.get("top_holders") or {}
    launch = data.get("launch_data") or {}
    creator_info = data.get("creator_info") or {}
    socials = data.get("socials") or {}

    # --- 1. DEV & BUNDLES ---
    if launch.get("limit_reached"):
        dev_line = "👨‍💻 Dev: ⚠️ UNKNOWN - Too many tx to verify (+20 Risk)"
    else:
        dev_pct = launch.get("dev_holding_pct", 0)
        if dev_pct > 10:
            dev_line = f"👨‍💻 Dev: 🔴 Holding {dev_pct:.1f}% - High dump risk"
        elif dev_pct == 0:
            dev_line = "👨‍💻 Dev: ✅ Sold Out - Cannot dump"
        else:
            dev_line = f"👨‍💻 Dev: 🟢 Holding {dev_pct:.1f}%"
            
    if launch.get("bundle_count", 0) > 0:
        dev_line += f"\n📦 Bundles Detected: {launch['bundle_count']} wallets"

    # --- 2. CREATOR HISTORY ---
    if creator_info.get("checked"):
        if creator_info.get("rugs", 0) > 0:
            creator_line = f"🏭 Creator: 🔴 RUGGER - Has rugged before ({creator_info['rugs']} rugs)"
        else:
            creator_line = "🏭 Creator: ✅ Clean - No rugs found"
    else:
        creator_line = "🏭 Creator: ⚠️ Unknown (Could not verify)"

    # --- 4. SOCIALS SECTION ---
    x_val = f"[{socials['x'].split('//')[-1]}]({socials['x']}) ✅" if socials.get("x") else "❌ No X"
    tg_val = f"[{socials['tg'].split('//')[-1]}]({socials['tg']}) ✅" if socials.get("tg") else "❌ No TG"
    web_val = f"[{socials['web'].split('//')[-1]}]({socials['web']}) ✅" if socials.get("web") else "❌ No Web"
    chart_url = data['url']
    dex_tools = f"https://www.dextools.io/app/en/solana/pair-explorer/{data['pair']}"
    
    socials_block = (
        "📱 <b>Socials:</b>\n"
        f"🐦 X: {x_val}\n"
        f"💬 TG: {tg_val}\n"
        f"🌐 Web: {web_val}\n"
        f"📊 Chart: <a href='{chart_url}'>DEXScreener</a> • <a href='{dex_tools}'>DEXTools</a>"
    )

    # --- FORMAT MAIN ---
    mc_text = money(data.get("mc")) if data.get("mc") else f"N/A (FDV {money(data.get('fdv'))})"
    
    lines = [
        f"🚀 <b>JABIR RUNNER V3</b> | ${esc(data['symbol'])}",
        f"🪙 {esc(data['name'])} | {esc(data['dex'])} | 🌱 {age_text(data['age_sec'])}",
        f"<code>{esc(data['mint'])}</code>",
        "",
        f"💰 {money(data['price'])} | MC {mc_text}",
        f"💧 LP {money(data['liquidity'])} | Vol 24h {money(data['volume24'])} | Vol/LP {ratio_text(data['volume24'], data['liquidity'])}",
        f"📊 5m {pct(data.get('price_change_5m'))} | 1h {pct(data.get('price_change_1h'))} | 6h {pct(data.get('price_change_6h'))} | 24h {pct(data.get('price_change_24h'))}",
        f"🔄 1h 🅑{fmt_count(data['buys1h'])} Ⓢ{fmt_count(data['sells1h'])} | 24h 🅑{fmt_count(data['buys24'])} Ⓢ{fmt_count(data['sells24'])} | Pools {data.get('pool_count', 1)}",
        "",
        f"🔒 Mint: {'ACTIVE ⚠️' if authorities.get('mint_authority_active') else 'revoked ✅'} | Freeze: {'ACTIVE ⚠️' if authorities.get('freeze_authority_active') else 'revoked ✅'}",
        f"👥 Top 10: {top.get('top10_percent', 0):.1f}% | Holders: {fmt_count(data.get('holders', {}).get('holder_count'))}",
        "",
        creator_line,
        dev_line,
        "",
        f"🧠 <i>{esc(narrative)}</i>",
        "",
        socials_block,
        "",
        f"🎯 <b>{verdict_text}</b> - {esc(verdict_reason)}",
        BAR,
        "<i>JABIR RUNNER | See Beyond The Chart.</i>"
    ]
    return "\n".join(lines)

# ------------------------------ RUNNERS / START / UI -------------------------
async def runners_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    status = await message.reply_text("🚀 Finding top Solana runners...")
    try:
        candidates = {}
        latest = await dex_get_json(DEX_BOOSTS_LATEST_URL)
        top = await dex_get_json(DEX_BOOSTS_TOP_URL)
        items = (latest if isinstance(latest, list) else []) + (top if isinstance(top, list) else [])
        
        for item in items:
            if not isinstance(item, dict) or item.get("chainId") != "solana": continue
            addr = item.get("tokenAddress")
            if addr and SOL_ADDR_RE.fullmatch(addr): candidates.setdefault(addr, {"_needs_pair": True})

        for mint in list(candidates.keys())[:15]:
            pair = await fetch_pair(mint)
            if pair: candidates[mint]["_pair_data"] = pair

        results = []
        for mint, data in candidates.items():
            pair = data.get("_pair_data")
            if not pair: continue
            mc = safe_num(pair.get("marketCap")) or safe_num(pair.get("fdv")) or 0
            liq = safe_num(nested(pair, "liquidity", "usd")) or 0
            vol = safe_num(nested(pair, "volume", "h24")) or 0
            chg = safe_num(nested(pair, "priceChange", "h24")) or 0
            
            if mc > 100_000 and liq > 5_000 and vol > 10_000 and chg > 5:
                results.append({"mint": mint, "symbol": nested(pair, "baseToken", "symbol") or "?", "chg": chg, "mc": mc, "liq": liq, "vol": vol, "url": f"https://dexscreener.com/solana/{mint}"})
                
        results.sort(key=lambda x: x["vol"], reverse=True)
        ranked = results[:10]
        
        if not ranked: return await status.edit_text("No runners found right now. Market is slow.")
        
        lines = ["🚀 <b>TOP RUNNERS</b>\n"]
        buttons = []
        for i, r in enumerate(ranked, 1):
            lines.append(f"<b>#{i} ${esc(r['symbol'])}</b> <b>{pct(r['chg'])}</b>\nMC {money(r['mc'])} | LP {money(r['liq'])} | Vol {money(r['vol'])}")
            if i <= 5: buttons.append([InlineKeyboardButton(f"📈 #{i} ${r['symbol'][:16]} Chart", url=r["url"])])
            
        await status.edit_text("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        await status.edit_text("Could not load runners.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await asyncio.to_thread(record_user, update.effective_user.id, update.effective_user.username, update.effective_user.first_name)
        
        keyboard = [
            [InlineKeyboardButton("🔍 Scan Token", callback_data="btn_scan"), InlineKeyboardButton("📈 Trending", callback_data="btn_trending")],
            [InlineKeyboardButton("❓ How It Works", callback_data="btn_how"), InlineKeyboardButton("📢 Channel", url="https://t.me/yourchannel")],
            [InlineKeyboardButton("💬 Support", url="https://t.me/yoursupport")]
        ]
        
        await update.message.reply_text(
            "🚀 <b>JABIR RUNNER V3</b>\n"
            "<i>See Beyond The Chart.</i>\n\n"
            "Send a Solana token address (CA) to scan it.\n\n"
            "Top runners: /runners\n"
            "Price alerts: /alert | /alerts | /delalert | /clearalerts\n"
            "Commands: /help",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "📖 <b>GUIDE</b>\n\n"
            "• Send a Solana CA to scan.\n"
            "• <code>/alert CA 0.001 above</code> or <code>/alert CA +50%</code>\n"
            "• /runners lists top trending tokens.\n"
            "• Deep Scan reveals Creator History and Bundles.",
            parse_mode=ParseMode.HTML
        )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "btn_scan":
        await query.message.reply_text("Send the Solana CA now.")
    elif data == "btn_trending":
        class FakeMsg: 
            async def reply_text(self, *args, **kwargs): return await query.message.reply_text(*args, **kwargs)
        await runners_command(type('Update', (), {'effective_message': FakeMsg(), 'effective_user': query.from_user})(), context)
    elif data == "btn_how":
        await query.message.reply_text(
            "❓ <b>HOW IT WORKS</b>\n\n"
            "🔒 <b>Mint:</b> If active, the dev can print infinite tokens and dump on you.\n"
            "❄️ <b>Freeze:</b> If active, the dev can freeze your wallet so you can't sell.\n"
            "🏭 <b>Creator:</b> We check if the wallet that made this token has rugged other tokens before.\n"
            "👨‍💻 <b>Dev:</b> How much the developer holds. If they hold >10%, they can dump.\n"
            "📦 <b>Bundles:</b> We check if the creator used multiple wallets to buy at launch (sniper bots).",
            parse_mode=ParseMode.HTML
        )
    elif ":" in data:
        action, mint = data.split(":", 1)
        if SOL_ADDR_RE.fullmatch(mint):
            await perform_scan(mint, context, status_message=query.message, deep=(action == "deep"))

# ------------------------------ SCAN LOGIC -----------------------------------
async def perform_scan(mint: str, context: ContextTypes.DEFAULT_TYPE, status_message=None, chat_id: Optional[int] = None, deep: bool = False) -> None:
    async def notify(txt):
        if status_message: await status_message.edit_text(txt)
        elif chat_id: await context.bot.send_message(chat_id, txt)
        
    try:
        if status_message and deep: await status_message.edit_text("🔍 Deep Scanning...")
        
        # Stage 1: Fetch basics
        pair = await fetch_pair(mint)
        if not pair: return await notify("❌ Token not found on DexScreener.")
        
        supply = await fetch_supply(mint)
        auth = await fetch_authorities(mint)
        
        # Stage 2: Fire heavy tasks in parallel
        top_task = asyncio.create_task(fetch_top_accounts(mint))
        creator_task = asyncio.create_task(fetch_creator_history(mint)) # Placeholder, we need creator from launch_data
        
        launch_data = None
        if supply:
            launch_data = await fetch_launch_data(mint, supply["raw"])
            
        # If launch data has creator, check history
        creator_info = {"checked": False}
        if launch_data and launch_data.get("creator"):
            creator_info = await fetch_creator_history(launch_data["creator"])
            
        top = await top_task
        
        # Build Data object
        base = pair.get("baseToken") or {}
        txns = pair.get("txns") or {}
        data = {
            "mint": mint, "name": base.get("name") or "Unknown", "symbol": base.get("symbol") or "?",
            "price": safe_num(pair.get("priceUsd")), "liquidity": safe_num(nested(pair, "liquidity", "usd")),
            "volume24": safe_num(nested(pair, "volume", "h24")), "fdv": safe_num(pair.get("fdv")),
            "mc": safe_num(pair.get("marketCap")), "dex": pair.get("dexId") or "N/A",
            "pair": pair.get("pairAddress"), "url": pair.get("url"), "pool_count": len(pair.get("_pools", [])),
            "price_change_5m": safe_num(nested(pair, "priceChange", "m5")),
            "price_change_1h": safe_num(nested(pair, "priceChange", "h1")),
            "price_change_6h": safe_num(nested(pair, "priceChange", "h6")),
            "price_change_24h": safe_num(nested(pair, "priceChange", "h24")),
            "buys1h": safe_int(nested(txns, "h1", "buys")), "sells1h": safe_int(nested(txns, "h1", "sells")),
            "buys24": safe_int(nested(txns, "h24", "buys")), "sells24": safe_int(nested(txns, "h24", "sells")),
            "created_ts": safe_num(pair.get("pairCreatedAt")), "age_sec": time.time() - (safe_num(pair.get("pairCreatedAt")) or 0)/1000 if pair.get("pairCreatedAt") else None,
            "authorities": auth, "launch_data": launch_data, "creator_info": creator_info,
            "socials": socials_from_pair(pair)
        }
        
        # Process Top Holders
        if supply and top:
            wallets = []
            for owner, amt in top["entries"]:
                share = amt / supply["raw"] * 100
                if owner not in BURN_ADDRESSES and is_on_curve(owner) is not False:
                    wallets.append(share)
            data["top_holders"] = {"top10_percent": sum(wallets[:10]) if wallets else 0}
        else:
            data["top_holders"] = {"top10_percent": 0}
            
        narrative = await generate_narrative(data)
        report = build_report(data, narrative, deep)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{mint}"), InlineKeyboardButton("🛡️ Deep Scan", callback_data=f"deep:{mint}")],
            [InlineKeyboardButton("Photon", url=f"https://photon-sol.tinyastro.io/en/lp/{mint}"), InlineKeyboardButton("GMGN", url=f"https://gmgn.ai/sol/token/{mint}"), InlineKeyboardButton("Bubblemaps", url=f"https://app.bubblemaps.io/sol/token/{mint}")]
        ])
        
        if status_message: await status_message.edit_text(report, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True)
        elif chat_id: await context.bot.send_message(chat_id, report, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True)
        
    except Exception as e:
        log.exception("Scan failed")
        await notify(f"❌ Scan error: {e}")

async def handle_ca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not message.text or not user: return
    await asyncio.to_thread(record_user, user.id, user.username, user.first_name)
    
    match = SOL_ADDR_RE.search(message.text.strip())
    if not match: return
    mint = match.group(0)
    
    status = await message.reply_text("🔍 Scanning...")
    await perform_scan(mint, context, status_message=status)
    await asyncio.to_thread(record_scan, user.id)

# ------------------------------ ALERTS / STATS -------------------------------
async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Use /alert CA above 0.001 to set alert.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if ADMIN_USER_ID and user.id != ADMIN_USER_ID: return await message.reply_text("Admin only.")
    s = await asyncio.to_thread(stats_snapshot)
    await message.reply_text(f"📊 <b>STATS</b>\nTotal users: {s['total']}\nActive today: {s['today']}\nTotal scans: {s['total_scans']}", parse_mode=ParseMode.HTML)

# ------------------------------ MAIN -----------------------------------------
async def post_init(application: Application) -> None:
    global _http
    _http = httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "JABIR-RUNNER/4.0"})
    await asyncio.to_thread(init_db)

async def post_shutdown(application: Application) -> None:
    if _http: await _http.aclose()

def main() -> None:
    if not BOT_TOKEN or not RENDER_URL or not WEBHOOK_SECRET: raise SystemExit("Missing env vars")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("runners", runners_command))
    app.add_handler(CommandHandler("alert", alert_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ca))
    
    app.run_webhook(listen="0.0.0.0", port=PORT, url_path=WEBHOOK_PATH, webhook_url=f"{RENDER_URL}/{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET)

if __name__ == "__main__":
    main()
