"""
JABIR RUNNER V3 - TRADER EDITION
Solana token scanner | Python 3.11 | python-telegram-bot 21+
DexScreener + GeckoTerminal + Helius + Groq | Render Webhook

Optional env vars: CACHE_TTL (default 60), PAID_CACHE_TTL (default 300), GROQ_MODEL
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
NARRATIVE_TIMEOUT = 6.0
ATH_TIMEOUT = 6.0
LAUNCH_TIMEOUT = 20.0
CACHE_MAX = 500
MAX_HOLDER_PAGES = 10
HOLDER_PAGE_SIZE = 1000
LAUNCH_SIG_PAGES = 6
LAUNCH_TX_SAMPLE = 40
SNIPER_SLOTS = 3

DEX_MIN_INTERVAL = 1.2
DEX_MAX_ATTEMPTS = 5
DEX_BACKOFF_BASE = 2.0
DEX_MAX_BACKOFF = 20.0
DEX_MAX_RETRY_AFTER = 60.0
DEX_TOTAL_DEADLINE = 100.0
GECKO_MIN_INTERVAL = 2.2

BURN_ADDRESSES = {"1nc1nerator11111111111111111111111111111111"}

TEST_TOKENS = {
    "testicles": "3pGmCpDCr2B6cZFhFJFtLrcMCadMvjV4dB9UGJyNpump",
    "pengu": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
    "grokius": "67ezHLk8PUkjJCXjmmgPbx85VowA52ghfRXa9A8Tpump",
}
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
    """DexScreener request failed."""


class DexRateLimited(DexScreenerError):
    """DexScreener kept answering HTTP 429 after all retries."""


class _LockEntry:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


_mint_locks: dict[str, _LockEntry] = {}

# ------------------------------ HELPERS --------------------------------------
def safe_num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None

def safe_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None

def nested(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data

def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)

def short_addr(address: str) -> str:
    return f"{address[:6]}...{address[-6:]}" if len(address) > 14 else address

def money(value: Optional[float]) -> str:
    value = safe_num(value)
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else ""
    value = abs(value)
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= divisor:
            return f"{sign}${value / divisor:.2f}{suffix}"
    if value == 0:
        return "$0"
    if value < 1:
        return f"{sign}${value:.10f}".rstrip("0").rstrip(".")
    return f"{sign}${value:,.2f}"

def pct(value: Optional[float]) -> str:
    value = safe_num(value)
    return "N/A" if value is None else f"{value:+.1f}%"

def age_text(seconds: Optional[float]) -> str:
    if seconds is None:
        return "N/A"
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d"

def utc_text(timestamp: Optional[int]) -> str:
    if not timestamp:
        return "N/A"
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d %b %Y %H:%M UTC")

def fmt_count(value: Optional[int]) -> str:
    return "N/A" if value is None else f"{value:,}"

def ratio_text(numerator: Optional[float], denominator: Optional[float]) -> str:
    if numerator is None or denominator is None or denominator <= 0:
        return "N/A"
    return f"{numerator / denominator:.2f}x"

def token_supply_text(supply: Optional[dict[str, Any]]) -> str:
    if not supply:
        return "N/A"
    raw, decimals = supply.get("raw"), supply.get("decimals")
    if raw is None or decimals is None:
        return "N/A"
    try:
        amount = Decimal(raw) / (Decimal(10) ** int(decimals))
        return f"{amount:,.4f}".rstrip("0").rstrip(".")
    except Exception:
        return "N/A"

# ------------------------------ BASE58 / ED25519 -----------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ED_P = 2 ** 255 - 19
_ED_D = (-121665 * pow(121666, -1, _ED_P)) % _ED_P

def b58decode(text: str) -> Optional[bytes]:
    number = 0
    for char in text:
        index = _B58.find(char)
        if index < 0:
            return None
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading_zeros + body

@functools.lru_cache(maxsize=4096)
def is_on_curve(address: str) -> Optional[bool]:
    """True = normal wallet key, False = program-derived (PDA), None = invalid address."""
    raw = b58decode(address)
    if raw is None or len(raw) != 32:
        return None
    value = int.from_bytes(raw, "little")
    sign_bit = value >> 255
    y = (value & ((1 << 255) - 1)) % _ED_P
    y2 = y * y % _ED_P
    u = (y2 - 1) % _ED_P
    v = (_ED_D * y2 + 1) % _ED_P
    if v == 0:
        return False
    x2 = u * pow(v, -1, _ED_P) % _ED_P
    if x2 == 0:
        return sign_bit == 0
    return pow(x2, (_ED_P - 1) // 2, _ED_P) == 1

# ------------------------------ DATABASE -------------------------------------
def db_connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    return db

@contextmanager
def db_session():
    """Commit/rollback and always close the SQLite connection."""
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
    if folder:
        os.makedirs(folder, exist_ok=True)
    with db_session() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS first_seen (
                mint TEXT PRIMARY KEY, first_seen INTEGER NOT NULL,
                first_price REAL, first_mc REAL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL,
                ts INTEGER NOT NULL, price REAL, fdv REAL, mc REAL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_mint_ts ON snapshots(mint, ts)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                mint TEXT NOT NULL,
                symbol TEXT,
                target_price REAL,
                direction TEXT,
                base_price REAL,
                pct REAL,
                created_at INTEGER NOT NULL,
                triggered INTEGER DEFAULT 0
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user_active ON alerts(user_id, triggered)")

def record_snapshot(mint: str, price: Optional[float], fdv: Optional[float], mc: Optional[float]) -> None:
    now = int(time.time())
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO first_seen(mint, first_seen, first_price, first_mc) VALUES(?,?,?,?)",
            (mint, now, price, mc if mc is not None else fdv),
        )
        db.execute(
            "INSERT INTO snapshots(mint, ts, price, fdv, mc) VALUES(?,?,?,?,?)",
            (mint, now, price, fdv, mc),
        )
        db.execute("DELETE FROM snapshots WHERE ts < ?", (now - 30 * 86400,))

def tracking_data(mint: str) -> dict[str, Any]:
    now = int(time.time())
    with db_session() as db:
        first = db.execute(
            "SELECT first_seen, first_price, first_mc FROM first_seen WHERE mint=?", (mint,)
        ).fetchone()
        ath = db.execute(
            "SELECT MAX(price), MAX(fdv), MAX(mc) FROM snapshots WHERE mint=?", (mint,)
        ).fetchone()
        old = db.execute(
            "SELECT price, fdv, mc, ts FROM snapshots WHERE mint=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (mint, now - 23 * 3600),
        ).fetchone()
        scans = db.execute("SELECT COUNT(*) FROM snapshots WHERE mint=?", (mint,)).fetchone()
    return {
        "first": first,
        "ath_price": ath[0] if ath else None,
        "ath_fdv": ath[1] if ath else None,
        "ath_mc": ath[2] if ath else None,
        "old_24h": old,
        "scans": scans[0] if scans else None,
    }

# ------------------------------ HTTP / API -----------------------------------
def _scrub(text: str) -> str:
    for secret in (HELIUS_KEY, GROQ_KEY, BOT_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return text

async def request_json(method: str, url: str, **kwargs: Any) -> Any:
    if _http is None:
        raise RuntimeError("HTTP client is not initialized")
    last_error = "unknown error"
    for attempt in range(2):
        try:
            response = await _http.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            body = _scrub(exc.response.text[:300].replace("\n", " "))
            status = exc.response.status_code
            last_error = f"HTTP {status}: {body}"
            if status < 500 and status != 429:
                break
            if attempt == 0:
                retry_after = _retry_after_seconds(exc.response) if status == 429 else None
                await asyncio.sleep(min(retry_after or 0.5, 8.0))
        except (httpx.HTTPError, ValueError) as exc:
            last_error = _scrub(f"{type(exc).__name__}: {exc}")
            if attempt == 0:
                await asyncio.sleep(0.5)
    raise RuntimeError(f"API request failed: {last_error}")

def _get_dex_semaphore() -> asyncio.Semaphore:
    global _dex_semaphore
    if _dex_semaphore is None:
        _dex_semaphore = asyncio.Semaphore(1)
    return _dex_semaphore

def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None

def _dex_backoff(attempt: int) -> float:
    return min(DEX_MAX_BACKOFF, DEX_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0))

async def dex_get_json(
    url: str, *, max_attempts: int = DEX_MAX_ATTEMPTS, max_retry_after: float = DEX_MAX_RETRY_AFTER,
) -> Any:
    global _dex_last_request
    if _http is None:
        raise RuntimeError("HTTP client is not initialized")

    async with _get_dex_semaphore():
        started = time.monotonic()
        problem = "unknown error"
        was_rate_limited = False
        attempt = 0
        for attempt in range(1, max_attempts + 1):
            gap = DEX_MIN_INTERVAL - (time.monotonic() - _dex_last_request)
            if gap > 0:
                await asyncio.sleep(gap)

            wait = _dex_backoff(attempt)
            was_rate_limited = False
            remaining = DEX_TOTAL_DEADLINE - (time.monotonic() - started)
            if remaining <= 0:
                problem = f"total request deadline {DEX_TOTAL_DEADLINE:.0f}s reached"
                break
            try:
                response = await asyncio.wait_for(
                    _http.get(url), timeout=min(25.0, remaining)
                )
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                _dex_last_request = time.monotonic()
                problem = f"network/timeout error ({type(exc).__name__})"
            else:
                _dex_last_request = time.monotonic()
                status = response.status_code
                if status == 429:
                    was_rate_limited = True
                    problem = "HTTP 429 Too Many Requests"
                    retry_after = _retry_after_seconds(response)
                    if retry_after is not None:
                        if retry_after > max_retry_after:
                            raise DexRateLimited(
                                f"HTTP 429, Retry-After {retry_after:.0f}s is above the "
                                f"{max_retry_after:.0f}s limit"
                            )
                        wait = retry_after + random.uniform(0.0, 0.5)
                elif status >= 500:
                    problem = f"HTTP {status} server error"
                elif status >= 400:
                    raise DexScreenerError(f"HTTP {status} (not retried)")
                else:
                    try:
                        return response.json()
                    except ValueError:
                        problem = "invalid JSON in response"

            if attempt >= max_attempts:
                break
            if time.monotonic() - started + wait > DEX_TOTAL_DEADLINE:
                problem += f" (total wait limit {DEX_TOTAL_DEADLINE:.0f}s reached)"
                break
            log.warning(
                "DexScreener %s (attempt %d/%d) - retrying in %.1fs",
                problem, attempt, max_attempts, wait,
            )
            await asyncio.sleep(wait)

    message = f"{problem} after {attempt} attempt(s)"
    if was_rate_limited:
        raise DexRateLimited(message)
    raise DexScreenerError(message)

def _get_gecko_semaphore() -> asyncio.Semaphore:
    global _gecko_semaphore
    if _gecko_semaphore is None:
        _gecko_semaphore = asyncio.Semaphore(1)
    return _gecko_semaphore

async def gecko_get_json(url: str, params: dict[str, Any]) -> Any:
    global _gecko_last_request
    if _http is None:
        return None
    async with _get_gecko_semaphore():
        gap = GECKO_MIN_INTERVAL - (time.monotonic() - _gecko_last_request)
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            response = await _http.get(
                url, params=params, headers={"Accept": "application/json;version=20230302"}
            )
        except httpx.HTTPError as exc:
            _gecko_last_request = time.monotonic()
            log.warning("GeckoTerminal request failed: %s", type(exc).__name__)
            return None
        _gecko_last_request = time.monotonic()
        if response.status_code != 200:
            log.warning("GeckoTerminal HTTP %s", response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            return None

@asynccontextmanager
async def mint_lock(key: str) -> AsyncIterator[None]:
    entry = _mint_locks.get(key)
    if entry is None:
        entry = _LockEntry()
        _mint_locks[key] = entry
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0 and _mint_locks.get(key) is entry:
            del _mint_locks[key]

def _trim_cache(cache: dict[str, tuple[Any, ...]], ttl: float) -> None:
    if len(cache) < CACHE_MAX:
        return
    now = time.time()
    for key in [k for k, v in cache.items() if now - v[0] >= ttl]:
        cache.pop(key, None)
    if len(cache) >= CACHE_MAX:
        cache.pop(next(iter(cache)))

async def helius_rpc(method: str, params: Any) -> Any:
    if not HELIUS_KEY:
        return None
    result = await request_json(
        "POST", HELIUS_RPC_URL,
        params={"api-key": HELIUS_KEY},
        json={"jsonrpc": "2.0", "id": "jabir-runner", "method": method, "params": params},
    )
    if not isinstance(result, dict):
        raise RuntimeError("Helius returned an invalid JSON-RPC response")
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result.get("result")

def _cached_pair(mint: str) -> Optional[dict[str, Any]]:
    entry = _cache.get(mint)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1]
    return None

def _recently_missing(mint: str) -> bool:
    entry = _missing_cache.get(mint)
    return bool(entry and time.time() - entry[0] < NOT_FOUND_TTL)

def _combine_pairs(matching: list[dict[str, Any]]) -> dict[str, Any]:
    primary = max(
        matching,
        key=lambda p: (safe_num(nested(p, "volume", "h24")) or 0,
                       safe_num(nested(p, "liquidity", "usd")) or 0),
    )
    combined = dict(primary)
    for field in ("liquidity", "volume"):
        vals = [safe_num(nested(p, field, "usd" if field == "liquidity" else "h24")) for p in matching]
        vals = [v for v in vals if v is not None]
        if vals:
            combined[field] = {"usd" if field == "liquidity" else "h24": sum(vals)}
    txns = {}
    for period in ("h1", "h24"):
        buys = [safe_num(nested(p, "txns", period, "buys")) for p in matching]
        sells = [safe_num(nested(p, "txns", period, "sells")) for p in matching]
        txns[period] = {
            "buys": sum(v for v in buys if v is not None) if any(v is not None for v in buys) else None,
            "sells": sum(v for v in sells if v is not None) if any(v is not None for v in sells) else None,
        }
    combined["txns"] = txns
    if combined.get("marketCap") is None:
        combined["marketCap"] = next((p.get("marketCap") for p in matching if p.get("marketCap") is not None), None)
    if combined.get("fdv") is None:
        combined["fdv"] = next((p.get("fdv") for p in matching if p.get("fdv") is not None), None)
    created = [safe_num(p.get("pairCreatedAt")) for p in matching]
    created = [c for c in created if c]
    if created:
        combined["pairCreatedAt"] = min(created)
    combined["_pools"] = sorted(
        (
            {
                "address": p.get("pairAddress"),
                "dex": p.get("dexId"),
                "created": safe_num(p.get("pairCreatedAt")),
                "vol": safe_num(nested(p, "volume", "h24")) or 0.0,
            }
            for p in matching if p.get("pairAddress")
        ),
        key=lambda x: x["vol"], reverse=True,
    )
    combined["_pool_count"] = len(matching)
    return combined

async def fetch_pair(mint: str) -> Optional[dict[str, Any]]:
    cached = _cached_pair(mint)
    if cached is not None:
        return cached
    if _recently_missing(mint):
        return None

    async with mint_lock(f"pair:{mint}"):
        cached = _cached_pair(mint)
        if cached is not None:
            return cached
        if _recently_missing(mint):
            return None

        payload = await dex_get_json(DEX_TOKEN_URL.format(mint))
        pairs = payload.get("pairs") if isinstance(payload, dict) else None
        if not isinstance(pairs, list):
            pairs = []
        matching = [
            p for p in pairs
            if isinstance(p, dict)
            and p.get("chainId") == "solana"
            and str(nested(p, "baseToken", "address") or "").lower() == mint.lower()
        ]
        if not matching:
            _trim_cache(_missing_cache, NOT_FOUND_TTL)
            _missing_cache[mint] = (time.time(), None)
            return None

        combined = _combine_pairs(matching)
        _trim_cache(_cache, CACHE_TTL)
        _cache[mint] = (time.time(), combined)
        _missing_cache.pop(mint, None)
        return combined

def _cached_paid(mint: str) -> Any:
    entry = _paid_cache.get(mint)
    if not entry:
        return _MISS
    ttl = PAID_CACHE_TTL if entry[1] is not None else PAID_FAIL_TTL
    return entry[1] if time.time() - entry[0] < ttl else _MISS

def parse_orders(orders: Any) -> Optional[dict[str, bool]]:
    if isinstance(orders, dict) and isinstance(orders.get("orders"), list):
        orders = orders["orders"]
    if not isinstance(orders, list):
        return None
    approved = [o for o in orders if isinstance(o, dict) and o.get("status") == "approved"]
    return {
        "paid": bool(approved),
        "ads": any(o.get("type") in ("tokenAd", "trendingBarAd") for o in approved),
    }

async def fetch_paid_status(mint: str) -> Optional[dict[str, bool]]:
    cached = _cached_paid(mint)
    if cached is not _MISS:
        return cached

    async with mint_lock(f"paid:{mint}"):
        cached = _cached_paid(mint)
        if cached is not _MISS:
            return cached

        result: Optional[dict[str, bool]] = None
        try:
            orders = await dex_get_json(DEX_ORDERS_URL.format(mint), max_attempts=2, max_retry_after=8.0)
            result = parse_orders(orders)
            if result is None:
                log.warning("Unexpected DexScreener orders response type %s for %s", type(orders).__name__, mint)
        except Exception as exc:
            log.warning("Dex paid status unavailable for %s: %s", mint, exc)
            result = None

        _trim_cache(_paid_cache, PAID_CACHE_TTL)
        _paid_cache[mint] = (time.time(), result)
        return result

async def fetch_supply(mint: str) -> Optional[dict[str, Any]]:
    result = await helius_rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
    value = (result or {}).get("value") or {}
    raw = value.get("amount")
    decimals = safe_int(value.get("decimals"))
    if raw is None or decimals is None:
        return None
    try:
        raw_int = int(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    return {"raw": raw_int, "decimals": decimals, "ui_amount": safe_num(value.get("uiAmount"))}

async def fetch_authorities(mint: str) -> Optional[dict[str, Any]]:
    result = await helius_rpc(
        "getAccountInfo", [mint, {"encoding": "base64", "commitment": "confirmed"}]
    )
    value = (result or {}).get("value")
    if not value:
        return None
    account_data = value.get("data")
    if not isinstance(account_data, list) or not account_data:
        return None
    try:
        raw = base64.b64decode(account_data[0])
    except Exception:
        return None
    if len(raw) < 82:
        return None

    def authority(option_offset: int) -> Optional[bool]:
        option = int.from_bytes(raw[option_offset:option_offset + 4], "little")
        return option == 1 if option in (0, 1) else None

    mint_active = authority(0)
    freeze_active = authority(46)
    if mint_active is None or freeze_active is None:
        return None
    return {
        "mint_authority_active": mint_active,
        "freeze_authority_active": freeze_active,
        "token_program": value.get("owner"),
        "layout": "standard SPL",
    }

async def fetch_top_accounts(mint: str) -> Optional[dict[str, Any]]:
    entry = _holders_cache.get(mint)
    if entry and time.time() - entry[0] < HOLDERS_CACHE_TTL:
        return entry[1]

    async with mint_lock(f"top:{mint}"):
        entry = _holders_cache.get(mint)
        if entry and time.time() - entry[0] < HOLDERS_CACHE_TTL:
            return entry[1]

        result = await helius_rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        accounts = [a for a in ((result or {}).get("value") or []) if isinstance(a, dict) and a.get("address")]
        data: Optional[dict[str, Any]] = None
        if accounts:
            info = await helius_rpc(
                "getMultipleAccounts",
                [[a["address"] for a in accounts], {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
            values = (info or {}).get("value") or []
            owners: dict[str, int] = {}
            for account, value in zip(accounts, values):
                owner = nested(value, "data", "parsed", "info", "owner")
                amount = safe_int(account.get("amount"))
                if isinstance(owner, str) and amount:
                    owners[owner] = owners.get(owner, 0) + amount
            if owners:
                data = {
                    "entries": sorted(owners.items(), key=lambda kv: kv[1], reverse=True),
                    "n_accounts": len(accounts),
                }
        _trim_cache(_holders_cache, HOLDERS_CACHE_TTL)
        _holders_cache[mint] = (time.time(), data)
        return data

def summarize_top_holders(
    top: Optional[dict[str, Any]], supply_raw: int, pool_addresses: set[str],
) -> Optional[dict[str, Any]]:
    if not top or not top.get("entries") or supply_raw <= 0:
        return None
    wallets: list[tuple[str, float]] = []
    excluded: list[tuple[str, float]] = []
    for owner, amount in top["entries"]:
        share = amount / supply_raw * 100
        if owner in BURN_ADDRESSES or owner in pool_addresses or is_on_curve(owner) is False:
            excluded.append((owner, share))
        else:
            wallets.append((owner, share))
    top10 = wallets[:10]
    all_shares = [share for _, share in wallets + excluded]
    return {
        "top10_percent": sum(s for _, s in top10) if top10 else None,
        "top_count": len(top10),
        "top_list": [s for _, s in wallets[:5]],
        "excluded_count": len(excluded),
        "excluded_percent": sum(s for _, s in excluded),
        "wallet_share": dict(wallets),
        "complete": False,
        "cutoff_percent": min(all_shares) if all_shares else None,
    }

async def fetch_holders(mint: str, supply_raw: int) -> Optional[dict[str, Any]]:
    if not HELIUS_KEY or supply_raw <= 0:
        return None
    cursor = None
    all_accounts: list[dict[str, Any]] = []
    reported_total = None
    complete = False

    for _ in range(MAX_HOLDER_PAGES):
        params: dict[str, Any] = {
            "mint": mint, "limit": HOLDER_PAGE_SIZE,
            "options": {"showZeroBalance": False},
        }
        if cursor:
            params["cursor"] = cursor
        result = await helius_rpc("getTokenAccounts", params)
        if not result:
            break
        if reported_total is None:
            reported_total = safe_int(result.get("total"))
        accounts = result.get("token_accounts") or []
        all_accounts.extend(a for a in accounts if isinstance(a, dict))
        next_cursor = result.get("cursor")
        if not next_cursor or not accounts:
            complete = True
            break
        cursor = next_cursor

    if not all_accounts:
        return None

    owner_amounts: dict[str, int] = {}
    for account in all_accounts:
        owner = account.get("owner")
        amount = safe_int(account.get("amount"))
        if owner and amount is not None and amount > 0:
            owner_amounts[owner] = owner_amounts.get(owner, 0) + amount

    if reported_total is not None:
        complete = complete and len(all_accounts) >= reported_total
    return {
        "holder_count": len(owner_amounts) if complete else None,
        "complete": complete,
        "accounts_scanned": len(all_accounts),
        "reported_accounts": reported_total,
    }

def _fee_payer(tx: dict[str, Any]) -> Optional[str]:
    keys = nested(tx, "transaction", "message", "accountKeys")
    if isinstance(keys, list) and keys:
        first = keys[0]
        if isinstance(first, dict):
            return first.get("pubkey")
        if isinstance(first, str):
            return first
    return None

def _token_deltas(tx: dict[str, Any], mint: str) -> dict[str, int]:
    meta = (tx or {}).get("meta") or {}
    pre: dict[str, int] = {}
    post: dict[str, int] = {}
    for source, target in ((meta.get("preTokenBalances"), pre), (meta.get("postTokenBalances"), post)):
        for balance in source or []:
            if not isinstance(balance, dict) or balance.get("mint") != mint or not balance.get("owner"):
                continue
            amount = safe_int(nested(balance, "uiTokenAmount", "amount")) or 0
            target[balance["owner"]] = target.get(balance["owner"], 0) + amount
    return {owner: post.get(owner, 0) - pre.get(owner, 0) for owner in set(pre) | set(post)}

def analyze_launch(txs: list[dict[str, Any]], mint: str, supply_raw: int) -> Optional[dict[str, Any]]:
    parsed = [t for t in txs if isinstance(t, dict) and safe_int(t.get("slot")) is not None]
    if not parsed or supply_raw <= 0:
        return None
    launch_slot = safe_int(parsed[0]["slot"])
    covered_slot = safe_int(parsed[-1]["slot"])
    creator = _fee_payer(parsed[0])

    bundle: dict[str, int] = {}
    snipers: dict[str, int] = {}
    creator_buy = 0
    for tx in parsed:
        slot = safe_int(tx["slot"])
        if slot is None or slot > launch_slot + SNIPER_SLOTS:
            continue
        for owner, delta in _token_deltas(tx, mint).items():
            if delta <= 0 or owner in BURN_ADDRESSES or is_on_curve(owner) is False:
                continue
            if owner == creator:
                if slot == launch_slot:
                    creator_buy += delta
                continue
            if slot == launch_slot:
                bundle[owner] = bundle.get(owner, 0) + delta
            elif owner not in bundle:
                snipers[owner] = snipers.get(owner, 0) + delta

    def share(amounts: dict[str, int]) -> float:
        return sum(amounts.values()) / supply_raw * 100

    return {
        "creator": creator,
        "launch_slot": launch_slot,
        "bundle_count": len(bundle),
        "bundle_percent": share(bundle),
        "sniper_count": len(snipers),
        "sniper_percent": share(snipers),
        "creator_buy_percent": creator_buy / supply_raw * 100,
        "slots_covered": (covered_slot - launch_slot) if covered_slot is not None else 0,
        "sample_size": len(parsed),
    }

async def fetch_launch_buyers(mint: str, supply_raw: int) -> Optional[dict[str, Any]]:
    signatures: list[dict[str, Any]] = []
    before = None
    reached_start = False
    for _ in range(LAUNCH_SIG_PAGES):
        options: dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
        if before:
            options["before"] = before
        page = await helius_rpc("getSignaturesForAddress", [mint, options])
        if not isinstance(page, list) or not page:
            reached_start = bool(signatures)
            break
        signatures.extend(s for s in page if isinstance(s, dict) and s.get("signature"))
        if len(page) < 1000:
            reached_start = True
            break
        before = page[-1].get("signature")
    if not reached_start or not signatures:
        return None

    oldest_first = [s for s in reversed(signatures) if not s.get("err")][:LAUNCH_TX_SAMPLE]
    if not oldest_first:
        return None

    limiter = asyncio.Semaphore(8)

    async def load(signature: str) -> Optional[dict[str, Any]]:
        async with limiter:
            return await helius_rpc(
                "getTransaction",
                [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
            )

    results = await asyncio.gather(*(load(s["signature"]) for s in oldest_first), return_exceptions=True)
    txs = [r for r in results if isinstance(r, dict)]
    return analyze_launch(txs, mint, supply_raw)

async def fetch_ath(
    mint: str, pools: list[dict[str, Any]], age_sec: Optional[float], price: Optional[float],
) -> Optional[dict[str, Any]]:
    if not pools or not price or price <= 0:
        return None
    entry = _ath_cache.get(mint)
    if entry:
        ttl = ATH_CACHE_TTL if entry[1] is not None else ATH_FAIL_TTL
        if time.time() - entry[0] < ttl:
            return entry[1]

    async with mint_lock(f"ath:{mint}"):
        entry = _ath_cache.get(mint)
        if entry:
            ttl = ATH_CACHE_TTL if entry[1] is not None else ATH_FAIL_TTL
            if time.time() - entry[0] < ttl:
                return entry[1]

        chosen = [pools[0]]
        dated = [p for p in pools if p.get("created")]
        if dated:
            oldest = min(dated, key=lambda p: p["created"])
            if oldest["address"] != pools[0]["address"]:
                chosen.append(oldest)

        use_hours = age_sec is not None and age_sec <= 40 * 86400
        timeframe, limit = ("hour", 1000) if use_hours else ("day", 180)

        async def one(pool: dict[str, Any]) -> Optional[tuple[float, float]]:
            payload = await gecko_get_json(
                GECKO_OHLCV_URL.format(pool=pool["address"], timeframe=timeframe),
                {"aggregate": 1, "limit": limit, "currency": "usd", "token": "base"},
            )
            candles = nested(payload, "data", "attributes", "ohlcv_list")
            if not isinstance(candles, list):
                return None
            rows = []
            for candle in candles:
                if isinstance(candle, list) and len(candle) >= 5:
                    ts, high, close = safe_num(candle[0]), safe_num(candle[2]), safe_num(candle[4])
                    if ts and high and high > 0:
                        rows.append((ts, high, close))
            if not rows:
                return None
            newest = max(rows, key=lambda r: r[0])
            if newest[2] and not (0.2 <= newest[2] / price <= 5):
                return None
            best = max(rows, key=lambda r: r[1])
            return best[0], best[1]

        found = [r for r in await asyncio.gather(*(one(p) for p in chosen), return_exceptions=True)
                 if isinstance(r, tuple)]
        result = None
        if found:
            ts, high = max(found, key=lambda r: r[1])
            result = {
                "price": max(high, price), "ts": ts if high >= price else time.time(),
                "limited_window": (not use_hours) and age_sec is not None and age_sec > 180 * 86400,
            }
        _trim_cache(_ath_cache, ATH_CACHE_TTL)
        _ath_cache[mint] = (time.time(), result)
        return result

def socials_from_pair(pair: dict[str, Any]) -> dict[str, bool]:
    info = pair.get("info") or {}
    websites = [x for x in (info.get("websites") or []) if isinstance(x, dict) and x.get("url")]
    socials = [x for x in (info.get("socials") or []) if isinstance(x, dict)]
    values = [
        " ".join(str(x.get(k, "")) for k in ("type", "platform", "url", "handle")).lower()
        for x in socials
    ]
    return {
        "x": any("twitter" in x or "x.com" in x for x in values),
        "web": bool(websites),
        "tg": any("telegram" in x or "t.me" in x for x in values),
    }

async def collect_token_data(mint: str, deep: bool = False) -> dict[str, Any]:
    async def pair_and_ath() -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        pair = await fetch_pair(mint)
        if not pair:
            return None, None
        created = safe_num(pair.get("pairCreatedAt"))
        age = time.time() - created / 1000 if created else None
        ath = None
        try:
            ath = await asyncio.wait_for(
                fetch_ath(mint, pair.get("_pools") or [], age, safe_num(pair.get("priceUsd"))),
                ATH_TIMEOUT,
            )
        except Exception as exc:
            log.warning("ATH lookup failed: %s", type(exc).__name__)
        return pair, ath

    async def helius_chain() -> dict[str, Any]:
        if not HELIUS_KEY:
            return {}
        supply = await fetch_supply(mint)
        out: dict[str, Any] = {"supply": supply}
        if not supply:
            return out
        jobs: dict[str, Any] = {"top": fetch_top_accounts(mint)}
        if deep:
            jobs["holders"] = fetch_holders(mint, supply["raw"])
            jobs["launch"] = asyncio.wait_for(fetch_launch_buyers(mint, supply["raw"]), LAUNCH_TIMEOUT)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        for key, value in zip(jobs, results):
            if isinstance(value, Exception):
                log.warning("%s lookup failed: %s", key, _scrub(str(value)) or type(value).__name__)
                value = None
            out[key] = value
        return out

    authority_task = fetch_authorities(mint) if HELIUS_KEY else asyncio.sleep(0, result=None)
    pair_res, paid_res, helius_res, authorities = await asyncio.gather(
        pair_and_ath(), fetch_paid_status(mint), helius_chain(), authority_task,
        return_exceptions=True,
    )
    if isinstance(pair_res, Exception):
        raise pair_res
    pair, ath_info = pair_res
    if not pair:
        raise ValueError("No Solana pair found for this token address.")
    for label, value in (("paid", paid_res), ("helius", helius_res), ("authorities", authorities)):
        if isinstance(value, Exception):
            log.warning("%s lookup failed: %s", label, _scrub(str(value)) or type(value).__name__)
    paid = None if isinstance(paid_res, Exception) else paid_res
    helius = {} if isinstance(helius_res, Exception) else helius_res
    if isinstance(authorities, Exception):
        authorities = None

    base = pair.get("baseToken") or {}
    volume, changes = pair.get("volume") or {}, pair.get("priceChange") or {}
    txns = pair.get("txns") or {}
    h1, h24 = txns.get("h1") or {}, txns.get("h24") or {}

    price = safe_num(pair.get("priceUsd"))
    fdv, mc = safe_num(pair.get("fdv")), safe_num(pair.get("marketCap"))
    liq = safe_num(nested(pair, "liquidity", "usd"))
    created_ms = safe_num(pair.get("pairCreatedAt"))
    created_ts = created_ms / 1000 if created_ms else None
    age_sec = time.time() - created_ts if created_ts else None

    await asyncio.to_thread(record_snapshot, mint, price, fdv, mc)
    tracking = await asyncio.to_thread(tracking_data, mint)
    first = tracking.get("first")
    now = time.time()

    since_first = None
    if first and first[1] and price is not None and first[1] > 0 and now - first[0] >= 300:
        since_first = (price / first[1] - 1) * 100

    mc_ref = mc if mc is not None else fdv
    mc_per_price = (mc_ref / price) if (mc_ref and price) else None
    ath: Optional[dict[str, Any]] = None
    if ath_info and price:
        ath = {
            "price": ath_info["price"], "ts": ath_info["ts"], "source": "chart",
            "limited_window": ath_info.get("limited_window", False),
        }
    elif first and now - first[0] >= 600 and tracking.get("ath_price"):
        ath = {"price": tracking["ath_price"], "ts": None, "source": "tracked", "limited_window": False}
    if ath and price:
        ath["distance"] = (price / ath["price"] - 1) * 100 if ath["price"] > 0 else None
        ath["mc"] = ath["price"] * mc_per_price if mc_per_price else None

    supply = helius.get("supply")
    pools = pair.get("_pools") or []
    pool_set = {p["address"] for p in pools if p.get("address")}
    pool_set.add(pair.get("pairAddress") or "")
    top_holders = None
    if supply and helius.get("top"):
        top_holders = summarize_top_holders(helius["top"], supply["raw"], pool_set)

    launch = helius.get("launch")
    dev = None
    if launch and launch.get("creator"):
        creator = launch["creator"]
        share = (top_holders or {}).get("wallet_share", {}).get(creator)
        dev = {
            "wallet": creator,
            "percent": share,
            "outside_top": share is None and top_holders is not None,
            "cutoff": (top_holders or {}).get("cutoff_percent"),
            "complete": bool((top_holders or {}).get("complete")),
        }

    return {
        "mint": mint, "name": base.get("name") or "Unknown", "symbol": base.get("symbol") or "?",
        "price": price, "liquidity": liq, "volume24": safe_num(volume.get("h24")),
        "fdv": fdv, "mc": mc,
        "price_change_5m": safe_num(changes.get("m5")),
        "price_change_1h": safe_num(changes.get("h1")),
        "price_change_6h": safe_num(changes.get("h6")),
        "price_change_24h": safe_num(changes.get("h24")),
        "buys1h": safe_int(h1.get("buys")), "sells1h": safe_int(h1.get("sells")),
        "buys24": safe_int(h24.get("buys")), "sells24": safe_int(h24.get("sells")),
        "dex": pair.get("dexId") or "N/A", "pair": pair.get("pairAddress"),
        "url": pair.get("url") or f"https://dexscreener.com/solana/{mint}",
        "created_ts": created_ts, "age_sec": age_sec,
        "ath": ath, "since_first": since_first, "scans": tracking.get("scans"),
        "supply": supply, "authorities": authorities,
        "top_holders": top_holders, "holders": helius.get("holders"),
        "launch": launch, "dev": dev, "deep": deep,
        "socials": socials_from_pair(pair),
        "dex_paid": paid.get("paid") if paid else None,
        "dex_ads": paid.get("ads") if paid else None,
        "first_seen": first[0] if first else None,
        "pair_data": pair, "pool_count": pair.get("_pool_count", 1),
    }

# ------------------------------ RISK / NARRATIVE -----------------------------
def buy_ratio(data: dict[str, Any]) -> Optional[float]:
    buys, sells = data.get("buys24"), data.get("sells24")
    if buys is None or sells is None or buys + sells <= 0:
        return None
    return buys / (buys + sells)

def market_flags(data: dict[str, Any]) -> list[tuple[str, str]]:
    flags: list[tuple[str, str]] = []
    liq, vol = data.get("liquidity"), data.get("volume24")
    mc = data.get("mc") or data.get("fdv")
    age = data.get("age_sec")
    if liq is not None and liq < 10_000:
        flags.append(("HIGH", "Liquidity below $10K"))
    if liq is not None and mc and 100_000 <= mc <= 50_000_000 and liq / mc < 0.02:
        flags.append(("HIGH", f"LP/MC very thin ({liq / mc * 100:.1f}%)"))
    elif liq is not None and mc and 100_000 <= mc <= 50_000_000 and liq / mc < 0.05:
        flags.append(("MEDIUM", f"LP/MC thin ({liq / mc * 100:.1f}%)"))
    if age is not None and age < 3600:
        flags.append(("MEDIUM", "Token is less than 1 hour old"))
    if vol is not None and mc and vol > mc * 5:
        flags.append(("MEDIUM", "24h volume is over 5x MC (possible wash trading)"))
    if vol is not None and data.get("fdv") and data["fdv"] > 0 and vol < data["fdv"] * 0.01:
        flags.append(("MEDIUM", "Very low volume compared to FDV"))
    ratio = buy_ratio(data)
    tx_total = (data.get("buys24") or 0) + (data.get("sells24") or 0)
    if ratio is not None and tx_total >= 100 and ratio < 0.40:
        flags.append(("MEDIUM", "Heavy selling (under 40% buys)"))
    return flags

def all_flags(data: dict[str, Any]) -> list[tuple[str, str]]:
    flags = market_flags(data)
    authorities = data.get("authorities") or {}
    if authorities.get("mint_authority_active") is True:
        flags.append(("HIGH", "Mint authority is active (more supply can be created)"))
    if authorities.get("freeze_authority_active") is True:
        flags.append(("HIGH", "Freeze authority is active (wallets can be frozen)"))

    top = data.get("top_holders") or {}
    top_list = top.get("top_list") or []
    if top_list and top_list[0] >= 20:
        flags.append(("HIGH", f"One wallet holds {top_list[0]:.1f}% of supply"))
    top10 = top.get("top10_percent")
    if top10 is not None:
        if top10 >= 50:
            flags.append(("HIGH", f"Top 10 wallets hold {top10:.1f}%"))
        elif top10 >= 30:
            flags.append(("MEDIUM", f"Top 10 wallets hold {top10:.1f}%"))

    launch = data.get("launch") or {}
    if launch.get("bundle_percent", 0) >= 10:
        flags.append(("MEDIUM", f"Launch bundle got about {launch['bundle_percent']:.1f}% (estimate)"))
    if launch.get("sniper_percent", 0) >= 10:
        flags.append(("MEDIUM", f"Snipers got about {launch['sniper_percent']:.1f}% (estimate)"))
    dev = data.get("dev") or {}
    if dev.get("percent") is not None and dev["percent"] >= 10:
        flags.append(("MEDIUM", f"Dev wallet (likely) holds {dev['percent']:.1f}%"))

    return sorted(flags, key=lambda f: 0 if f[0] == "HIGH" else 1)

def verdict(data: dict[str, Any]) -> tuple[str, str]:
    liq, vol = data.get("liquidity"), data.get("volume24")
    mc = data.get("mc") or data.get("fdv")
    chg, age = data.get("price_change_24h"), data.get("age_sec")
    flags = all_flags(data)
    if liq is not None and mc is not None and liq < 10_000 and mc > 100_000:
        return "🔴 AVOID", "Liquidity is too low for this market cap"
    if age is not None and age >= 3600 and mc is not None and mc < 5_000 and (vol is None or vol < 1_000):
        return "🔴 AVOID", f"Dead token: MC {money(mc)}, 24h volume {money(vol)}"
    high = [text for level, text in flags if level == "HIGH"]
    if high:
        return "🟡 WATCH", "High risk: " + high[0]
    medium = [text for level, text in flags if level == "MEDIUM"]
    if medium:
        return "🟡 WATCH", "Risk flag: " + medium[0]
    authorities = data.get("authorities") or {}
    if (authorities.get("mint_authority_active") is not False
            or authorities.get("freeze_authority_active") is not False):
        return "🟡 WATCH", "Mint/freeze authority status is not confirmed revoked"
    if age is not None and age < 3600 and vol is not None and mc and vol > mc * 2:
        return "🟡 WATCH", "Very new token with unusually high volume"
    if chg is not None and chg > 100 and liq is not None and vol is not None and vol > liq:
        return "🟡 WATCH", "Price is overheated. Wait for a pullback or confirmation"
    ratio = buy_ratio(data)
    if (chg is not None and chg > 0 and ratio is not None and ratio >= 0.55
            and liq is not None and liq >= 25_000 and age is not None and age >= 3600):
        return "🟢 BUY", f"Up {chg:.1f}% in 24h, {ratio * 100:.0f}% buys, liquidity {money(liq)}"
    return "🟡 WATCH", "No clear momentum yet"

def fallback_narrative(data: dict[str, Any]) -> str:
    socials = data.get("socials") or {}
    links = [label for label, ok in (("X", socials.get("x")), ("website", socials.get("web")), ("Telegram", socials.get("tg"))) if ok]
    links_text = "has " + ", ".join(links) + " links" if links else "has no social links listed"
    return (
        f"{data['name']} trades on {data['dex']}, is {age_text(data['age_sec'])} old and {links_text}. "
        f"Project identity is unverified."
    )

async def groq_narrative(data: dict[str, Any]) -> str:
    cached = _narrative_cache.get(data["mint"])
    if cached and time.time() - cached[0] < NARRATIVE_CACHE_TTL:
        return cached[1]
    if not GROQ_KEY:
        return fallback_narrative(data)

    prompt_data = {
        "name": data["name"], "ticker": data["symbol"], "dex": data["dex"],
        "token_age": age_text(data["age_sec"]),
        "price_change_1h": data["price_change_1h"],
        "price_change_24h": data["price_change_24h"],
        "liquidity_usd": data["liquidity"], "volume_24h_usd": data["volume24"],
        "social_links_present": data["socials"],
    }
    prompt = (
        "Write ONE short line in simple English (max 20 words) about this token for a trader. "
        "Use only the data given. Say that the project identity is unverified. "
        "Do not invent utility, team, news, partnerships, community or adoption. "
        "Do not advise buying or selling. No quotes, no bullets, no intro. "
        "Data: " + json.dumps(prompt_data, ensure_ascii=False)
    )
    try:
        payload: dict[str, Any] = {
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": "Return exactly one short line of simple English. No quotes."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 800,
        }
        if "gpt-oss" in GROQ_MODEL.lower():
            payload["reasoning_effort"] = "low"
        result = await asyncio.wait_for(
            request_json("POST", GROQ_URL, headers={"Authorization": f"Bearer {GROQ_KEY}"}, json=payload),
            NARRATIVE_TIMEOUT,
        )
        content = (result["choices"][0]["message"].get("content") or "").strip()
        content = content.replace('"', "").replace("'", "").strip()
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        text = lines[0] if lines else fallback_narrative(data)
    except Exception as exc:
        log.warning("Groq narrative skipped: %s", _scrub(str(exc)) or type(exc).__name__)
        text = fallback_narrative(data)
    _trim_cache(_narrative_cache, NARRATIVE_CACHE_TTL)
    _narrative_cache[data["mint"]] = (time.time(), text)
    return text

# ------------------------------ REPORT / UI ----------------------------------
def yes_no(value: Optional[bool]) -> str:
    return "✅" if value is True else "❌" if value is False else "N/A"

def build_report(data: dict[str, Any], narrative: str, deep: bool) -> str:
    verdict_text, verdict_reason = verdict(data)
    flags = all_flags(data)
    authorities = data.get("authorities") or {}
    top = data.get("top_holders") or {}
    holders = data.get("holders") or {}
    launch = data.get("launch") or {}
    dev = data.get("dev") or {}
    socials = data.get("socials") or {}

    mc_text = money(data.get("mc")) if data.get("mc") is not None else (
        f"N/A (FDV {money(data.get('fdv'))})" if data.get("fdv") is not None else "N/A"
    )

    ath = data.get("ath")
    if ath and ath.get("mc") is not None and ath.get("distance") is not None:
        when = ""
        if ath.get("ts"):
            when = f" · {age_text(time.time() - ath['ts'])} ago"
        elif ath.get("source") == "tracked":
            when = " · tracked"
        window = " (6m)" if ath.get("limited_window") else ""
        ath_line = f"🏔 ATH MC {money(ath['mc'])}{window} ({ath['distance']:+.0f}%){when}"
    else:
        ath_line = "🏔 ATH: N/A"

    track_bits = []
    if data.get("since_first") is not None and data.get("first_seen"):
        track_bits.append(f"Since 1st scan {data['since_first']:+.1f}% ({age_text(time.time() - data['first_seen'])} ago)")
    if data.get("scans"):
        track_bits.append(f"Scans: {data['scans']}")
    track_line = "📈 " + " | ".join(track_bits) if track_bits else ""

    def authority_text(key: str) -> str:
        if not authorities:
            return "N/A"
        return "ACTIVE ⚠️" if authorities.get(key) is True else "revoked ✅"

    if top.get("top10_percent") is not None:
        label = "Top 10" if top.get("top_count", 0) >= 10 else f"Top {top.get('top_count')}"
        holders_line = f"👥 {label}: {top['top10_percent']:.1f}%"
        if top.get("excluded_count"):
            holders_line += f" (excl. {top['excluded_count']} pool/program wallets)"
        if top.get("top_list"):
            holders_line += "\n    " + " | ".join(f"{s:.1f}%" for s in top["top_list"])
    elif data.get("supply") is None and not HELIUS_KEY:
        holders_line = "👥 Holders: N/A (Helius key missing)"
    else:
        holders_line = "👥 Top holders: N/A"
    if holders.get("holder_count") is not None:
        holders_line += f"\n    Total holders: {fmt_count(holders['holder_count'])}"
    elif holders.get("accounts_scanned") and not holders.get("complete"):
        holders_line += f"\n    Total holders: {fmt_count(holders['accounts_scanned'])}+ (scan limit)"

    if launch:
        extra = [
            f"📦 Bundle (est.): {launch['bundle_count']} wallets in launch block "
            f"({launch['bundle_percent']:.1f}% of supply)",
            f"🎯 Snipers (est.): {launch['sniper_count']} wallets "
            f"({launch['sniper_percent']:.1f}% of supply)",
        ]
        if dev.get("wallet"):
            if dev.get("percent") is not None:
                dev_text = f"holds {dev['percent']:.1f}%"
            elif dev.get("outside_top") and dev.get("complete"):
                dev_text = "holds 0% (not in holder list)"
            elif dev.get("outside_top") and dev.get("cutoff") is not None:
                dev_text = f"holds under {dev['cutoff']:.1f}% (outside top 20)"
            else:
                dev_text = "holding unknown"
            extra.append(f"🛠 Dev (likely) {esc(short_addr(dev['wallet']))} {dev_text}")
        launch_block = "\n".join(extra)
    elif deep:
        launch_block = "📦 Bundles/snipers/dev: N/A (token has too many transactions or data unavailable)"
    else:
        launch_block = "📦 Bundles/snipers/dev: tap Deep Scan"

    if flags:
        level = "🔴" if any(l == "HIGH" for l, _ in flags) else "🟡"
        risk_text = f"{level} " + "; ".join(t for _, t in flags[:3])
    else:
        risk_text = "🟢 No flags found in available data"

    social_line = (
        f"🔗 X {yes_no(socials.get('x'))} • Web {yes_no(socials.get('web'))} • TG {yes_no(socials.get('tg'))}"
    )

    lines = [
        f"🚀 <b>JABIR RUNNER V3</b> | ${esc(data['symbol'])}",
        BAR,
        f"🪙 {esc(data['name'])} | {esc(data['dex'])} | 🌱 {age_text(data['age_sec'])}",
        f"<code>{esc(data['mint'])}</code>",
        "",
        f"💰 {money(data['price'])} | MC {mc_text}",
        ath_line,
        f"💧 LP {money(data['liquidity'])} | Vol 24h {money(data['volume24'])} | Vol/LP {ratio_text(data['volume24'], data['liquidity'])}",
        f"📊 5m {pct(data.get('price_change_5m'))} | 1h {pct(data['price_change_1h'])} | 6h {pct(data.get('price_change_6h'))} | 24h {pct(data['price_change_24h'])}",
        f"🔄 1h 🅑{fmt_count(data['buys1h'])} Ⓢ{fmt_count(data['sells1h'])} | 24h 🅑{fmt_count(data['buys24'])} Ⓢ{fmt_count(data['sells24'])} | Pools {data.get('pool_count', 1)}",
    ]
    if track_line:
        lines.append(track_line)
    lines += [
        "",
        f"🧠 {esc(narrative)}",
        "",
        f"🔒 Mint: {authority_text('mint_authority_active')} | Freeze: {authority_text('freeze_authority_active')}",
        f"💎 DEX Paid {yes_no(data.get('dex_paid'))} | Ads {yes_no(data.get('dex_ads'))}",
        holders_line,
        launch_block,
        social_line,
        "",
        f"⚠️ Risk: {esc(risk_text)}",
        f"🎯 <b>{verdict_text}</b> - {esc(verdict_reason)}",
        BAR,
        "<i>JABIR RUNNER | See Beyond The Chart.</i>",
    ]
    return "\n".join(lines)

def report_keyboard(mint: str, pair_url: str, pair_address: Optional[str]) -> InlineKeyboardMarkup:
    pair_address = pair_address or mint
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{mint}"),
            InlineKeyboardButton("🛡️ Deep Scan", callback_data=f"deep:{mint}"),
        ],
        [
            InlineKeyboardButton("📈 Chart", url=pair_url),
            InlineKeyboardButton("Photon", url=f"https://photon-sol.tinyastro.io/en/lp/{pair_address}"),
            InlineKeyboardButton("GMGN", url=f"https://gmgn.ai/sol/token/{mint}"),
        ],
        [
            InlineKeyboardButton("Bubblemaps", url=f"https://app.bubblemaps.io/sol/token/{mint}"),
            InlineKeyboardButton("RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
            InlineKeyboardButton("Solscan", url=f"https://solscan.io/token/{mint}"),
        ],
    ])


# ------------------------------ RUNNERS (Boosted + Trending + New Pairs) ---------------------------------------
async def runners_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    status = await message.reply_text("🚀 Finding top Solana runners...")
    try:
        candidates: dict[str, dict[str, Any]] = {}

        # Fetch from multiple sources in parallel
        async def fetch_boosts() -> list:
            try:
                latest = await dex_get_json(DEX_BOOSTS_LATEST_URL)
                top = await dex_get_json(DEX_BOOSTS_TOP_URL)
                items = []
                for payload in (latest, top):
                    if isinstance(payload, list):
                        items.extend(payload)
                return items
            except Exception:
                return []

        async def fetch_trending() -> list:
            try:
                # Search for trending Solana tokens
                payload = await dex_get_json(f"{DEX_SEARCH_URL}?q=solana")
                if isinstance(payload, dict) and isinstance(payload.get("pairs"), list):
                    return payload["pairs"]
                return []
            except Exception:
                return []

        boosts_items, trending_items = await asyncio.gather(
            fetch_boosts(), fetch_trending(), return_exceptions=True
        )

        # Process boosted tokens (they have chainId and tokenAddress)
        if isinstance(boosts_items, list):
            for item in boosts_items:
                if not isinstance(item, dict):
                    continue
                if item.get("chainId") != "solana":
                    continue
                token_addr = item.get("tokenAddress")
                if not token_addr or not SOL_ADDR_RE.fullmatch(token_addr):
                    continue
                # Boosts don't have pair data; we'll fetch it later via search or tokens endpoint
                # Store minimal info for now
                candidates.setdefault(token_addr, {"_needs_pair": True, "_boost_item": item})

        # Process trending/search pairs directly
        if isinstance(trending_items, list):
            for item in trending_items:
                if not isinstance(item, dict):
                    continue
                if item.get("chainId") != "solana":
                    continue
                base = item.get("baseToken") if isinstance(item.get("baseToken"), dict) else {}
                token_addr = base.get("address")
                if not token_addr or not SOL_ADDR_RE.fullmatch(token_addr):
                    continue
                # If we already have this mint from boosts, merge
                existing = candidates.get(token_addr)
                if existing and existing.get("_needs_pair"):
                    existing["_pair_data"] = item
                elif not existing:
                    candidates[token_addr] = {"_pair_data": item}

        # For boosted tokens without pair data, fetch pair info
        tokens_to_fetch = [
            mint for mint, data in candidates.items()
            if data.get("_needs_pair") and not data.get("_pair_data")
        ]
        if tokens_to_fetch:
            # Fetch in batches to avoid too many requests
            for mint in tokens_to_fetch[:15]:  # limit to avoid rate limits
                try:
                    pair = await fetch_pair(mint)
                    if pair:
                        candidates[mint]["_pair_data"] = pair
                except Exception:
                    pass

        # Now filter and score
        results: list[dict[str, Any]] = []
        seen_mints: set[str] = set()

        for mint, data in candidates.items():
            if mint in seen_mints:
                continue
            pair = data.get("_pair_data")
            if not pair or not isinstance(pair, dict):
                continue

            # Extract fields
            base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
            symbol = str(base.get("symbol") or "?")
            mc = safe_num(pair.get("marketCap")) or safe_num(pair.get("fdv"))
            liq = safe_num(nested(pair, "liquidity", "usd"))
            vol = safe_num(nested(pair, "volume", "h24"))
            change = safe_num(nested(pair, "priceChange", "h24"))
            url = pair.get("url")

            # Apply filters: MC > $100K, LP > $5K, Vol 24h > $10K, 24h change > +5%
            if mc is None or mc <= 100_000:
                continue
            if liq is None or liq <= 5_000:
                continue
            if vol is None or vol <= 10_000:
                continue
            if change is None or change <= 5:
                continue

            if not isinstance(url, str) or not url.startswith("https://dexscreener.com/"):
                url = f"https://dexscreener.com/solana/{mint}"

            results.append({
                "mint": mint,
                "symbol": symbol,
                "change": change,
                "mc": mc,
                "liq": liq,
                "vol": vol,
                "url": url,
            })
            seen_mints.add(mint)

        # Sort by volume (descending) and take top 10
        results.sort(key=lambda x: x["vol"], reverse=True)
        ranked = results[:10]

        if not ranked:
            await status.edit_text(
                "No runners found right now. Market is slow. Try again later."
            )
            return

        lines = ["🚀 <b>JABIR RUNNER | TOP RUNNERS</b>", BAR,
                 "<i>Solana • 24h gain • MC > $100K • LP > $5K • Vol > $10K</i>", ""]
        buttons = []
        for rank, r in enumerate(ranked, 1):
            lines.append(
                f"<b>#{rank} ${esc(r['symbol'])}</b>  <b>{pct(r['change'])}</b>\n"
                f"MC {money(r['mc'])} | LP {money(r['liq'])} | Vol {money(r['vol'])}\n"
                f"<code>{esc(r['mint'])}</code>"
            )
            if rank <= 5:
                buttons.append([InlineKeyboardButton(f"📈 #{rank} ${r['symbol'][:16]} Chart", url=r["url"])])
        lines += ["", "<i>Data from DexScreener. Not financial advice.</i>"]
        await status.edit_text(
            "\n\n".join(lines), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            disable_web_page_preview=True,
        )
    except DexRateLimited:
        await status.edit_text("Rate limit reached. Try again in 1 minute.")
    except DexScreenerError as exc:
        log.warning("/runners DexScreener error: %s", exc)
        await status.edit_text("DexScreener is unavailable right now. Please try again shortly.")
    except Exception:
        log.exception("/runners failed")
        await status.edit_text("Could not load runners right now. Please try again later.")

# ------------------------------ PRICE ALERTS ----------------------------------
def _valid_mint(value: str) -> bool:
    return bool(SOL_ADDR_RE.fullmatch(value)) and len(b58decode(value) or b"") == 32

def _alert_usage() -> str:
    return (
        "Usage:\n"
        "/alert <CA> <price> <above|below>\n"
        "Example: /alert 3pGm... 0.001 above\n\n"
        "/alert <CA> <+/-percent>%\n"
        "Example: /alert 3pGm... +50%"
    )

def _create_alert(
    user_id: int, chat_id: int, mint: str, symbol: str,
    target_price: float, direction: str, base_price: float,
    percent: Optional[float],
) -> Optional[int]:
    with db_session() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT COUNT(*) FROM alerts WHERE user_id=? AND triggered=0",
            (user_id,),
        ).fetchone()[0]
        if active >= 10:
            return None
        cursor = db.execute(
            """INSERT INTO alerts
               (user_id, chat_id, mint, symbol, target_price, direction,
                base_price, pct, created_at, triggered)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (user_id, chat_id, mint, symbol, target_price, direction,
             base_price, percent, int(time.time())),
        )
        return int(cursor.lastrowid)

def _active_alert_count(user_id: int) -> int:
    with db_session() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM alerts WHERE user_id=? AND triggered=0",
            (user_id,),
        ).fetchone()
    return int(row[0] or 0)

def _get_user_alerts(user_id: int, limit: int = 20) -> list[tuple]:
    with db_session() as db:
        return db.execute(
            """SELECT id, mint, symbol, target_price, direction, base_price,
                      pct, created_at, triggered
               FROM alerts WHERE user_id=? ORDER BY id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()

def _delete_alert(user_id: int, alert_id: int) -> int:
    with db_session() as db:
        cursor = db.execute(
            "DELETE FROM alerts WHERE user_id=? AND id=?",
            (user_id, alert_id),
        )
        return cursor.rowcount

def _clear_alerts(user_id: int) -> int:
    with db_session() as db:
        cursor = db.execute("DELETE FROM alerts WHERE user_id=?", (user_id,))
        return cursor.rowcount

def _untriggered_alerts() -> list[tuple]:
    with db_session() as db:
        return db.execute(
            """SELECT id, user_id, chat_id, mint, symbol, target_price,
                      direction, base_price, pct
               FROM alerts WHERE triggered=0 ORDER BY id ASC"""
        ).fetchall()

def _mark_alert_triggered(alert_id: int) -> bool:
    with db_session() as db:
        cursor = db.execute(
            "UPDATE alerts SET triggered=1 WHERE id=? AND triggered=0",
            (alert_id,),
        )
        return cursor.rowcount == 1

async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    args = context.args or []
    if len(args) not in (2, 3):
        await message.reply_text(_alert_usage())
        return

    mint = args[0].strip()
    if not _valid_mint(mint):
        await message.reply_text("Invalid Solana token address.")
        return

    if len(args) == 3:
        price = safe_num(args[1])
        direction = args[2].lower()
        if price is None or price <= 0 or direction not in ("above", "below"):
            await message.reply_text(_alert_usage())
            return
        percent = None
    else:
        raw_percent = args[1].strip()
        if not raw_percent.endswith("%"):
            await message.reply_text(_alert_usage())
            return
        percent_value = safe_num(raw_percent[:-1])
        if percent_value is None or percent_value == 0 or abs(percent_value) > 100000:
            await message.reply_text(_alert_usage())
            return
        direction = "up_pct" if percent_value > 0 else "down_pct"
        percent = abs(percent_value)
        price = None

    try:
        if await asyncio.to_thread(_active_alert_count, user.id) >= 10:
            await message.reply_text("You have max 10 active alerts.")
            return
        pair = await fetch_pair(mint)
        if not pair:
            await message.reply_text("Token not found on DexScreener.")
            return
        current = safe_num(pair.get("priceUsd"))
        if current is None or current <= 0:
            await message.reply_text("Current token price is unavailable. Try again later.")
            return
        symbol = str(nested(pair, "baseToken", "symbol") or "?")
        if direction == "above":
            target = price
        elif direction == "below":
            target = price
        elif direction == "up_pct":
            target = current * (1 + (percent or 0) / 100)
        else:
            target = current * (1 - (percent or 0) / 100)
            if target <= 0:
                await message.reply_text("Percentage is too large for a downward price alert.")
                return

        alert_id = await asyncio.to_thread(
            _create_alert, user.id, message.chat_id, mint, symbol,
            target, direction, current, percent,
        )
        if alert_id is None:
            await message.reply_text("You have max 10 active alerts.")
            return
        await message.reply_text(
            f"✅ <b>Alert Created</b>\n"
            f"ID: <code>{alert_id}</code> | ${esc(symbol)}\n"
            f"Current: <code>{current:.12g}</code>\n"
            f"Target: <code>{target:.12g}</code> ({esc(direction)})\n"
            f"Use /alerts to manage your alerts.",
            parse_mode=ParseMode.HTML,
        )
    except DexRateLimited:
        await message.reply_text("Rate limit reached. Try again in 1 minute.")
    except Exception:
        log.exception("Could not create price alert")
        await message.reply_text("Could not create alert right now. Please try again.")

async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    try:
        rows = await asyncio.to_thread(_get_user_alerts, user.id)
        if not rows:
            await message.reply_text("You have no alerts yet. Create one with /alert.")
            return
        lines = ["🔔 <b>YOUR PRICE ALERTS</b>", BAR]
        for row in rows:
            alert_id, mint, symbol, target, direction, base, percent, created, triggered = row
            status = "✅ TRIGGERED" if triggered else "🟢 ACTIVE"
            target_num = safe_num(target)
            target_text = format(target_num, ".12g") if target_num is not None else "N/A"
            lines.append(
                f"<b>#{alert_id} ${esc(symbol or '?')}</b> · {status}\n"
                f"Target: <code>{target_text}</code> · {esc(direction)}\n"
                f"<code>{esc(mint)}</code>"
            )
        lines.append("\nUse /delalert ID to delete one, /clearalerts to delete all.")
        await message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Could not list alerts")
        await message.reply_text("Could not load your alerts.")

async def delalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    if len(context.args or []) != 1 or not (context.args[0].isdigit()):
        await message.reply_text("Usage: /delalert <id>")
        return
    try:
        count = await asyncio.to_thread(_delete_alert, user.id, int(context.args[0]))
        await message.reply_text("Alert deleted." if count else "Alert not found.")
    except Exception:
        log.exception("Could not delete alert")
        await message.reply_text("Could not delete alert.")

async def clearalerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    try:
        count = await asyncio.to_thread(_clear_alerts, user.id)
        await message.reply_text(f"Deleted {count} alert(s).")
    except Exception:
        log.exception("Could not clear alerts")
        await message.reply_text("Could not clear alerts.")

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    global _alerts_check_lock
    if _alerts_check_lock is None:
        _alerts_check_lock = asyncio.Lock()
    if _alerts_check_lock.locked():
        return
    async with _alerts_check_lock:
        try:
            rows = await asyncio.to_thread(_untriggered_alerts)
        except Exception:
            log.exception("Could not read active alerts")
            return

        for row in rows:
            alert_id, user_id, chat_id, mint, symbol, target, direction, base, percent = row
            try:
                pair = await fetch_pair(mint)
                if not pair:
                    continue
                current = safe_num(pair.get("priceUsd"))
                target = safe_num(target)
                if current is None or target is None or current <= 0:
                    continue
                hit = (
                    (direction in ("above", "up_pct") and current >= target)
                    or (direction in ("below", "down_pct") and current <= target)
                )
                if not hit:
                    continue
                marked = await asyncio.to_thread(_mark_alert_triggered, alert_id)
                if not marked:
                    continue
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔔 <b>Alert Triggered</b> ${esc(symbol or nested(pair, 'baseToken', 'symbol') or '?')} "
                        f"reached <code>{current:.12g}</code> "
                        f"(target <code>{target:.12g}</code>)"
                    ),
                    parse_mode=ParseMode.HTML,
                )
                await perform_scan(mint, context, chat_id=chat_id, deep=False, announce=False)
            except Exception:
                log.exception("Alert check failed for alert ID %s", alert_id)
                continue


# ------------------------------ TELEGRAM -------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "🚀 <b>JABIR RUNNER V3</b>\n<i>See Beyond The Chart.</i>\n\n"
            "Send a Solana token address (CA) to scan it.\n"
            "Quick tests: /testicles /pengu /grokius\n"
            "Top runners: /runners\n"
            "Price alerts: /alert | /alerts | /delalert | /clearalerts\n"
            "Commands: /help",
            parse_mode=ParseMode.HTML,
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "📖 <b>JABIR RUNNER V3 GUIDE</b>\n\n"
            "• Send a Solana CA to scan it.\n"
            "• /testicles, /pengu, /grokius are quick tests.\n"
            "• /runners lists top Solana runners (MC > $100K, LP > $5K, Vol > $10K, 24h +5%).\n"
            "• <code>/alert CA 0.001 above</code> or <code>/alert CA +50%</code> creates a price alert.\n"
            "• <code>/alerts</code> lists alerts; <code>/delalert ID</code> deletes one; <code>/clearalerts</code> deletes all.\n"
            "• Refresh updates the market data.\n"
            "• Deep Scan adds total holder count and an estimate of launch bundles, snipers and dev wallet "
            "(only works for tokens with a small transaction history).\n\n"
            "Top holders exclude pool/program wallets. ATH comes from GeckoTerminal chart data. "
            "Bundle/sniper/dev numbers are estimates, not proof.",
            parse_mode=ParseMode.HTML,
        )

async def perform_scan(
    mint: str, context: ContextTypes.DEFAULT_TYPE,
    status_message=None, chat_id: Optional[int] = None, deep: bool = False,
    announce: bool = True,
) -> None:
    async def notify(text: str) -> None:
        try:
            if status_message:
                await status_message.edit_text(text)
            elif chat_id is not None:
                await context.bot.send_message(chat_id, text)
        except Exception:
            log.exception("Could not send scan notice")

    try:
        if status_message and announce:
            await status_message.edit_text("Scanning..." + (" (deep scan)" if deep else ""))
        data = await collect_token_data(mint, deep=deep)
        narrative = await groq_narrative(data)
        report = build_report(data, narrative, deep)
        keyboard = report_keyboard(mint, data["url"], data.get("pair"))
        if status_message:
            await status_message.edit_text(
                report, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        elif chat_id is not None:
            await context.bot.send_message(
                chat_id, report, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    except ValueError as exc:
        await notify(f"{exc}")
    except DexRateLimited as exc:
        log.warning("Scan for %s stopped by DexScreener rate limit: %s", mint, exc)
        await notify("DexScreener is rate-limiting requests right now. Please try again in about a minute.")
    except DexScreenerError as exc:
        log.warning("Scan for %s failed, DexScreener error: %s", mint, exc)
        await notify("DexScreener is not responding properly right now. Please try again shortly.")
    except Exception:
        log.exception("Scan failed for %s", mint)
        await notify("Scan failed. Check the Render logs and API keys.")

async def handle_ca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    match = SOL_ADDR_RE.search(message.text.strip())
    if not match:
        return
    mint = match.group(0)
    try:
        await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    except Exception:
        pass
    status = await message.reply_text("Scanning...")
    await perform_scan(mint, context, status_message=status, deep=False, announce=False)

async def preset_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    if update.message:
        status = await update.message.reply_text(f"Scanning {key.upper()}...")
        await perform_scan(TEST_TOKENS[key], context, status_message=status, deep=False, announce=False)

async def testicles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await preset_scan(update, context, "testicles")

async def pengu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await preset_scan(update, context, "pengu")

async def grokius_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await preset_scan(update, context, "grokius")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if query.message is None:
        return
    raw = query.data or ""
    if ":" not in raw:
        return
    action, mint = raw.split(":", 1)
    if action not in ("refresh", "deep") or not SOL_ADDR_RE.fullmatch(mint):
        return
    await perform_scan(mint, context, status_message=query.message, deep=(action == "deep"))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled Telegram error: %s", context.error)

async def post_init(application: Application) -> None:
    global _http
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(25.0, connect=10.0),
        headers={"User-Agent": "JABIR-RUNNER/3.0"},
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=10),
    )
    await asyncio.to_thread(init_db)
    log.info("JABIR RUNNER V3 initialized")

async def post_shutdown(application: Application) -> None:
    global _http
    if _http:
        await _http.aclose()
        _http = None

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Missing TELEGRAM_TOKEN or BOT_TOKEN")
    if not RENDER_URL:
        raise SystemExit("Missing RENDER_EXTERNAL_URL")
    if not WEBHOOK_SECRET:
        raise SystemExit("Missing WEBHOOK_SECRET")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
        raise SystemExit("WEBHOOK_SECRET must be 1-256 chars of A-Z, a-z, 0-9, _ or -")

    app = (
        Application.builder().token(BOT_TOKEN).concurrent_updates(True)
        .post_init(post_init).post_shutdown(post_shutdown).build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("testicles", testicles_cmd))
    app.add_handler(CommandHandler("pengu", pengu_cmd))
    app.add_handler(CommandHandler("grokius", grokius_cmd))
    app.add_handler(CommandHandler("runners", runners_command))
    app.add_handler(CommandHandler("alert", alert_command))
    app.add_handler(CommandHandler("alerts", alerts_command))
    app.add_handler(CommandHandler("delalert", delalert_command))
    app.add_handler(CommandHandler("clearalerts", clearalerts_command))
    if app.job_queue is not None:
        app.job_queue.run_repeating(check_alerts, interval=120, first=10, name="price-alert-checker")
    else:
        log.error("JobQueue is unavailable. Install python-telegram-bot[job-queue] to enable price alerts.")
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ca))
    app.add_error_handler(error_handler)

    log.info("JABIR RUNNER V3 starting webhook")
    app.run_webhook(
        listen="0.0.0.0", port=PORT, url_path=WEBHOOK_PATH,
        webhook_url=f"{RENDER_URL}/{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET, drop_pending_updates=True,
        bootstrap_retries=-1,
    )

if __name__ == "__main__":
    main()
