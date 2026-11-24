"""
JABIR RUNNER V3 - FINAL FIXED (DexScreener 429 hardening)
Solana token scanner | Python 3.11 | python-telegram-bot 21+
DexScreener + Helius + Groq | Render Webhook

Optional env vars: CACHE_TTL (default 60), PAID_CACHE_TTL (default 300)
"""
from __future__ import annotations

import asyncio
import base64
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
from contextlib import asynccontextmanager
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
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")  # llama-3.3-70b-versatile was shut down by Groq

RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_PATH = "telegram-webhook"

DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
DEX_ORDERS_URL = "https://api.dexscreener.com/orders/v1/solana/{}"
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

DB_PATH = os.getenv("DB_PATH", "jabir_runner.db")
CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))               # token data cache (seconds)
PAID_CACHE_TTL = int(os.getenv("PAID_CACHE_TTL", "300"))    # DexScreener orders / DEX Paid cache
PAID_FAIL_TTL = 30                                          # short cache for failed orders lookups
NOT_FOUND_TTL = 20                                          # short cache for "no pair found"
CACHE_MAX = 500
MAX_HOLDER_PAGES = 10
HOLDER_PAGE_SIZE = 1000

# DexScreener rate limiting
DEX_MIN_INTERVAL = 1.2      # minimum seconds between ANY two DexScreener requests
DEX_MAX_ATTEMPTS = 5        # attempts per call (429, 5xx and network errors are retried)
DEX_BACKOFF_BASE = 2.0      # exponential backoff base, seconds
DEX_MAX_WAIT = 20.0         # never wait longer than this between attempts

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
log = logging.getLogger("jabir_runner")
_http: Optional[httpx.AsyncClient] = None
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_missing_cache: dict[str, tuple[float, None]] = {}
_paid_cache: dict[str, tuple[float, Optional[bool]]] = {}

# DexScreener shared limiter state (one request at a time, spaced by DEX_MIN_INTERVAL)
_dex_semaphore: Optional[asyncio.Semaphore] = None
_dex_last_request: float = -1e9
_MISS = object()


class DexScreenerError(RuntimeError):
    """DexScreener request failed (permanent 4xx, repeated 5xx/network errors, bad data)."""


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

# ------------------------------ DATABASE -------------------------------------
def db_connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    return db

def init_db() -> None:
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with db_connect() as db:
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

def record_snapshot(mint: str, price: Optional[float], fdv: Optional[float], mc: Optional[float]) -> None:
    now = int(time.time())
    with db_connect() as db:
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
    with db_connect() as db:
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
    return {
        "first": first,
        "ath_price": ath[0] if ath else None,
        "ath_fdv": ath[1] if ath else None,
        "ath_mc": ath[2] if ath else None,
        "old_24h": old,
    }

# ------------------------------ HTTP / API -----------------------------------
def _scrub(text: str) -> str:
    """Remove API keys/tokens from any text before it is logged."""
    for secret in (HELIUS_KEY, GROQ_KEY, BOT_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return text

async def request_json(method: str, url: str, **kwargs: Any) -> Any:
    """Generic JSON request used for Helius and Groq (NOT for DexScreener)."""
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
            last_error = f"HTTP {exc.response.status_code}: {body}"
            if attempt == 0:
                await asyncio.sleep(0.5)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = _scrub(f"{type(exc).__name__}: {exc}")
            if attempt == 0:
                await asyncio.sleep(0.5)
    raise RuntimeError(f"API request failed: {last_error}")

# ---- DexScreener: shared rate limiter + retry ----
def _get_dex_semaphore() -> asyncio.Semaphore:
    """Created lazily so it always belongs to the running event loop."""
    global _dex_semaphore
    if _dex_semaphore is None:
        _dex_semaphore = asyncio.Semaphore(1)
    return _dex_semaphore

def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    """Parse Retry-After (seconds or HTTP date). Returns None if absent/invalid."""
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
    """Exponential backoff with jitter, capped at DEX_MAX_WAIT."""
    return min(DEX_MAX_WAIT, DEX_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0))

async def dex_get_json(url: str) -> Any:
    """
    The ONLY way the bot talks to DexScreener (token AND orders endpoints).
    - One request at a time (semaphore wraps the whole request + retry loop)
    - At least DEX_MIN_INTERVAL seconds between requests
    - Up to DEX_MAX_ATTEMPTS attempts for 429, 5xx, network errors, bad JSON
    - 429: honors Retry-After, otherwise exponential backoff + jitter
    - Other 4xx (404, 400...) are permanent: raised immediately, no retry
    """
    global _dex_last_request
    if _http is None:
        raise RuntimeError("HTTP client is not initialized")

    async with _get_dex_semaphore():
        problem = "unknown error"
        was_rate_limited = False
        for attempt in range(1, DEX_MAX_ATTEMPTS + 1):
            gap = DEX_MIN_INTERVAL - (time.monotonic() - _dex_last_request)
            if gap > 0:
                await asyncio.sleep(gap)

            wait = _dex_backoff(attempt)
            was_rate_limited = False
            try:
                response = await _http.get(url)
            except httpx.TransportError as exc:  # timeouts, connect/read errors
                _dex_last_request = time.monotonic()
                problem = f"network error ({type(exc).__name__})"
            else:
                _dex_last_request = time.monotonic()
                status = response.status_code
                if status == 429:
                    was_rate_limited = True
                    problem = "HTTP 429 Too Many Requests"
                    retry_after = _retry_after_seconds(response)
                    if retry_after is not None:
                        if retry_after > DEX_MAX_WAIT:
                            raise DexRateLimited(
                                f"HTTP 429, Retry-After {retry_after:.0f}s is above the "
                                f"{DEX_MAX_WAIT:.0f}s limit"
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

            if attempt >= DEX_MAX_ATTEMPTS:
                break
            log.warning(
                "DexScreener %s (attempt %d/%d) - retrying in %.1fs",
                problem, attempt, DEX_MAX_ATTEMPTS, wait,
            )
            await asyncio.sleep(wait)

    message = f"{problem} after {DEX_MAX_ATTEMPTS} attempts"
    if was_rate_limited:
        raise DexRateLimited(message)
    raise DexScreenerError(message)

@asynccontextmanager
async def mint_lock(key: str) -> AsyncIterator[None]:
    """Per-key async lock; entries are removed when nobody is using them."""
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
    combined["_pool_count"] = len(matching)
    return combined

async def fetch_pair(mint: str) -> Optional[dict[str, Any]]:
    cached = _cached_pair(mint)
    if cached is not None:
        return cached
    if _recently_missing(mint):
        return None

    async with mint_lock(f"pair:{mint}"):
        # Recheck after getting the lock: another scan may have just filled the cache.
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

async def fetch_paid_status(mint: str) -> Optional[bool]:
    cached = _cached_paid(mint)
    if cached is not _MISS:
        return cached

    async with mint_lock(f"paid:{mint}"):
        cached = _cached_paid(mint)
        if cached is not _MISS:
            return cached

        result: Optional[bool] = None
        try:
            orders = await dex_get_json(DEX_ORDERS_URL.format(mint))
            if isinstance(orders, list):
                result = any(
                    isinstance(order, dict) and order.get("status") == "approved"
                    for order in orders
                )
        except Exception as exc:
            log.warning("Dex paid status unavailable for %s: %s", mint, exc)
            result = None

        _trim_cache(_paid_cache, PAID_CACHE_TTL)
        _paid_cache[mint] = (time.time(), result)  # failures (None) are cached only briefly
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

    def authority(option_offset: int, key_offset: int) -> Optional[bool]:
        option = int.from_bytes(raw[option_offset:option_offset + 4], "little")
        return option == 1 if option in (0, 1) else None

    mint_active = authority(0, 4)
    freeze_active = authority(46, 50)
    if mint_active is None or freeze_active is None:
        return None
    return {
        "mint_authority_active": mint_active,
        "freeze_authority_active": freeze_active,
        "token_program": value.get("owner"),
        "layout": "standard SPL",
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

    ranked = sorted(owner_amounts.items(), key=lambda item: item[1], reverse=True)
    if reported_total is not None:
        complete = complete and len(all_accounts) >= reported_total
    top10 = (sum(amount for _, amount in ranked[:10]) / supply_raw * 100) if complete else None
    top_five = [f"{amount / supply_raw * 100:.1f}%" for _, amount in ranked[:5]]
    return {
        "holder_count": len(owner_amounts) if complete else None,
        "top10_percent": top10,
        "top_five": top_five,
        "complete": complete,
        "accounts_scanned": len(all_accounts),
        "reported_accounts": reported_total,
    }

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
    pair = await fetch_pair(mint)
    if not pair:
        raise ValueError("Ba a samu Solana pair da mint ɗin yake base token ba.")

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
    old = tracking.get("old_24h")
    fdv_change_24h = None
    if old and old[1] and fdv is not None and old[1] > 0:
        fdv_change_24h = (fdv / old[1] - 1) * 100

    paid_task = fetch_paid_status(mint)
    supply_task = fetch_supply(mint) if HELIUS_KEY else asyncio.sleep(0, result=None)
    authority_task = fetch_authorities(mint) if HELIUS_KEY else asyncio.sleep(0, result=None)
    paid, supply, authorities = await asyncio.gather(
        paid_task, supply_task, authority_task, return_exceptions=True
    )
    for label, value in (("paid", paid), ("supply", supply), ("authorities", authorities)):
        if isinstance(value, Exception):
            log.warning("%s lookup failed: %s", label, value)
    if isinstance(paid, Exception): paid = None
    if isinstance(supply, Exception): supply = None
    if isinstance(authorities, Exception): authorities = None

    holders = None
    if deep and supply:
        try:
            holders = await fetch_holders(mint, supply["raw"])
        except Exception as exc:
            log.warning("Holder lookup failed: %s", exc)

    first = tracking.get("first")
    ath_price = tracking.get("ath_price")
    ath_distance = ((price / ath_price) - 1) * 100 if price is not None and ath_price and ath_price > 0 else None

    return {
        "mint": mint, "name": base.get("name") or "Unknown", "symbol": base.get("symbol") or "?",
        "price": price, "liquidity": liq, "volume24": safe_num(volume.get("h24")),
        "fdv": fdv, "mc": mc,
        "price_change_1h": safe_num(changes.get("h1")),
        "price_change_24h": safe_num(changes.get("h24")),
        "buys1h": safe_int(h1.get("buys")), "sells1h": safe_int(h1.get("sells")),
        "buys24": safe_int(h24.get("buys")), "sells24": safe_int(h24.get("sells")),
        "dex": pair.get("dexId") or "N/A", "pair": pair.get("pairAddress"),
        "url": pair.get("url") or f"https://dexscreener.com/solana/{mint}",
        "created_ts": created_ts, "age_sec": age_sec,
        "ath_price": ath_price, "ath_distance": ath_distance,
        "fdv_change_24h": fdv_change_24h,
        "supply": supply, "authorities": authorities, "holders": holders,
        "socials": socials_from_pair(pair), "dex_paid": paid,
        "first_seen": first[0] if first else None,
        "first_price": first[1] if first else None, "first_mc": first[2] if first else None,
        "pair_data": pair, "pool_count": pair.get("_pool_count", 1),
    }

# ------------------------------ RISK / NARRATIVE -----------------------------
def buy_ratio(data: dict[str, Any]) -> Optional[float]:
    buys, sells = data.get("buys24"), data.get("sells24")
    if buys is None or sells is None or buys + sells <= 0:
        return None
    return buys / (buys + sells)

def market_flags(data: dict[str, Any]) -> list[tuple[str, str]]:
    flags = []
    liq, vol = data.get("liquidity"), data.get("volume24")
    mc = data.get("mc") or data.get("fdv")
    age = data.get("age_sec")
    if liq is not None and liq < 10_000:
        flags.append(("HIGH", "Liquidity below $10K"))
    # FIXED: LP/MC check ONLY for small/mid caps 100K - 50M. Large caps like PENGU are exempt.
    if liq is not None and mc and 100_000 <= mc <= 50_000_000 and liq / mc < 0.02:
        flags.append(("HIGH", f"LP/MC very thin ({liq / mc * 100:.1f}%)"))
    elif liq is not None and mc and 100_000 <= mc <= 50_000_000 and liq / mc < 0.05:
        flags.append(("MEDIUM", f"LP/MC thin ({liq / mc * 100:.1f}%)"))
    if age is not None and age < 3600:
        flags.append(("MEDIUM", "Pair younger than 1 hour"))
    if vol is not None and mc and vol > mc * 5:
        flags.append(("MEDIUM", "24h volume exceeds 5x MC; verify wash-trading risk"))
    if vol is not None and data.get("fdv") and data["fdv"] > 0 and vol < data["fdv"] * 0.01:
        flags.append(("MEDIUM", "Volume is thin versus FDV"))
    ratio = buy_ratio(data)
    tx_total = (data.get("buys24") or 0) + (data.get("sells24") or 0)
    if ratio is not None and tx_total >= 100 and ratio < 0.40:
        flags.append(("MEDIUM", "Heavy sell pressure"))
    return flags

def security_assessment(data: dict[str, Any]) -> dict[str, Any]:
    authorities = data.get("authorities") or {}
    holders = data.get("holders") or {}
    flags = []
    if authorities.get("mint_authority_active") is True:
        flags.append("Mint authority active")
    if authorities.get("freeze_authority_active") is True:
        flags.append("Freeze authority active")
    top10 = holders.get("top10_percent")
    if top10 is not None and top10 >= 50:
        flags.append(f"Top 10 wallets hold {top10:.1f}%")
        top_five = holders.get("top_five") or []
        top_one = safe_num(str(top_five[0]).rstrip("%")) if top_five else None
        if top_one is not None and top_one >= 50:
            flags.append(f"Top wallet holds {top_one:.1f}% (may be bonding curve/pool, not verified)")
    if flags:
        level = "HIGH"
    elif not authorities:
        level = "UNKNOWN"
    elif data.get("holders") is None:
        level = "PARTIAL"
    else:
        level = "CHECKED" if holders.get("complete") else "PARTIAL"
    return {"level": level, "flags": flags}

def verdict(data: dict[str, Any]) -> tuple[str, str]:
    liq, vol = data.get("liquidity"), data.get("volume24")
    mc = data.get("mc") or data.get("fdv")
    chg, age = data.get("price_change_24h"), data.get("age_sec")
    flags = market_flags(data)
    security = security_assessment(data)
    if liq is not None and mc is not None and liq < 10_000 and mc > 100_000:
        return "🔴 AVOID", "Low liquidity relative to valuation"
    if (age is not None and age >= 3600 and mc is not None and mc < 5_000
            and (vol is None or vol < 1_000)):
        return "🔴 AVOID", f"Dead/illiquid: MC {money(mc)}, 24h vol {money(vol)}"
    if any(level == "HIGH" for level, _ in flags) or security["level"] == "HIGH":
        return "🟡 WATCH", "High-risk warning detected; inspect the flags"
    if age is not None and age < 3600 and vol is not None and mc and vol > mc * 2:
        return "🟡 WATCH", "Very new pair with unusually high volume"
    if chg is not None and chg > 100 and liq is not None and vol is not None and vol > liq:
        return "🟡 WATCH", "Potentially overheated; wait for confirmation"
    # FIXED: Allow BUY when data is clean
    ratio = buy_ratio(data)
    if chg is not None and chg > 0 and ratio is not None and ratio >= 0.55 and liq is not None and liq >= 25_000 and age is not None and age >= 3600:
        return "🟢 BUY", f"Up {chg:.1f}%, {ratio*100:.0f}% buys, LP {money(liq)}"
    missing = []
    if not HELIUS_KEY: missing.append("Helius key missing")
    if security["level"] in ("UNKNOWN", "PARTIAL"): missing.append("on-chain checks incomplete")
    if data.get("buys24") is None or data.get("sells24") is None: missing.append("transaction data incomplete")
    return "🟡 WATCH", "No momentum confirmation. " + (", ".join(missing) if missing else "Use a separately tested strategy.")

async def groq_narrative(data: dict[str, Any]) -> str:
    if not GROQ_KEY:
        return "AI narrative: N/A (GROQ_API_KEY missing)"
    prompt_data = {
        "name": data["name"], "ticker": data["symbol"], "dex": data["dex"],
        "pair_age": age_text(data["age_sec"]),
        "price_change_1h": data["price_change_1h"],
        "price_change_24h": data["price_change_24h"],
        "liquidity_usd": data["liquidity"], "volume_24h_usd": data["volume24"],
        "social_links_present": data["socials"],
    }
    prompt = (
        "Write exactly two concise lines in natural Hausa. Describe only what the "
        "provided name, symbol, links and figures support. Clearly say project identity "
        "and social authenticity are unverified. Do not invent utility, team, news, "
        "partnerships, community, followers or adoption. Do not advise buying/selling. "
        "Data: " + json.dumps(prompt_data, ensure_ascii=False)
    )
    try:
        payload: dict[str, Any] = {
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": "Return only two concise Hausa lines. No unsupported claims."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            # gpt-oss models "think" first and thinking tokens count toward this limit
            "max_tokens": 800,
        }
        if "gpt-oss" in GROQ_MODEL.lower():
            payload["reasoning_effort"] = "low"
        result = await request_json(
            "POST", GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json=payload,
        )
        content = (result["choices"][0]["message"].get("content") or "").strip()
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        return "\n".join(lines[:2]) if lines else "AI narrative: N/A"
    except Exception as exc:
        log.warning("Groq narrative failed: %s", exc)
        return "AI narrative: N/A (Groq request failed)"

# ------------------------------ REPORT / UI ----------------------------------
def yes_no(value: Optional[bool]) -> str:
    return "✅" if value is True else "❌" if value is False else "N/A"

def build_report(data: dict[str, Any], narrative: str, deep: bool) -> str:
    security = security_assessment(data)
    market = market_flags(data)
    verdict_text, verdict_reason = verdict(data)
    supply, holders = data.get("supply") or {}, data.get("holders") or {}
    authorities = data.get("authorities") or {}

    mc_value = data.get("mc")
    mc_text = money(mc_value) if mc_value is not None else (
        f"N/A (FDV {money(data.get('fdv'))})" if data.get("fdv") is not None else "N/A"
    )
    ath_text = "N/A (tracking just started)"
    first_seen = data.get("first_seen")
    tracked_long_enough = first_seen is not None and time.time() - first_seen >= 600
    if tracked_long_enough and data.get("ath_price") is not None and data.get("ath_distance") is not None:
        ath_text = f"{money(data['ath_price'])} ({data['ath_distance']:+.1f}% from tracked high)"
    top10 = holders.get("top10_percent")
    top10_text = f"{top10:.1f}%" if top10 is not None else "N/A"
    mint_auth = "ACTIVE" if authorities.get("mint_authority_active") is True else (
        "REVOKED" if authorities and authorities.get("mint_authority_active") is False else "N/A"
    )
    freeze_auth = "ACTIVE" if authorities.get("freeze_authority_active") is True else (
        "REVOKED" if authorities and authorities.get("freeze_authority_active") is False else "N/A"
    )
    market_text = "; ".join(text for _, text in market[:3]) if market else "No market flags detected"
    security_text = "; ".join(security["flags"]) if security["flags"] else (
        "No detected authority/holder flag; this is not a full audit"
    )
    pair = data.get("pair") or data["mint"]
    chart_url = f"https://dexscreener.com/solana/{pair}"
    photon_url = f"https://photon-sol.tinyastro.io/en/lp/{pair}"
    solscan_url = f"https://solscan.io/token/{data['mint']}"
    dex_url = data.get("url") or chart_url

    lines = [
        f"🚀 <b>JABIR RUNNER V3</b> | ${esc(data['symbol'])}", BAR,
        f"🪙 {esc(data['name'])} | {esc(data['dex'])} | 🌱 {age_text(data['age_sec'])}",
        f"📍 CA: <code>{esc(short_addr(data['mint']))}</code>",
        "", "<b>📊 MARKET</b>",
        f"💰 {money(data['price'])} ({pct(data['price_change_24h'])}) | MC: {mc_text}",
        f"🏔️ ATH: {ath_text}",
        f"💧 LP: {money(data['liquidity'])} | Vol 24h: {money(data['volume24'])} | Vol/LP: {ratio_text(data['volume24'], data['liquidity'])}",
        f"⏱️ 1H: {pct(data['price_change_1h'])} | Buys: {fmt_count(data['buys1h'])} | Sells: {fmt_count(data['sells1h'])}",
        f"⚡ FDV: {money(data['fdv'])} | Supply: {token_supply_text(supply)} | Pools: {data.get('pool_count', 1)}",
        "", f"<b>🧠 NARRATIVE</b>\n{esc(narrative)}",
        "", "<b>🔒 SECURITY</b>",
        f"├ Mint: {mint_auth} | Freeze: {freeze_auth} | DEX Paid: {yes_no(data.get('dex_paid'))}",
        f"├ Top 10: {top10_text} | Holders: {fmt_count(holders.get('holder_count'))}",
        f"├ Top wallets: {esc('|'.join(holders.get('top_five') or []) or 'N/A')}",
        "└ Bundle/Dev/Snipers/Fake holders: N/A (not attributed)",
        "", f"<b>⚠️ MARKET RISK:</b> {esc(market_text)}",
        f"<b>🛡️ ON-CHAIN:</b> {esc(security['level'])} - {esc(security_text)}",
        f"<b>🎯 VERDICT:</b> {verdict_text} - {esc(verdict_reason)}",
        f"🕒 First scan: {utc_text(data.get('first_seen'))} | ATH is bot-tracked only",
        f"🔗 <a href=\"{esc(chart_url)}\">Chart</a> | <a href=\"{esc(dex_url)}\">DexScreener</a> | <a href=\"{esc(photon_url)}\">Photon</a> | <a href=\"{esc(solscan_url)}\">Solscan</a>",
        BAR, "<i>JABIR RUNNER | See Beyond The Chart.</i>",
    ]
    return "\n".join(lines)

def report_keyboard(mint: str, pair_url: str, pair_address: Optional[str]) -> InlineKeyboardMarkup:
    pair_address = pair_address or mint
    photon = f"https://photon-sol.tinyastro.io/en/lp/{pair_address}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{mint}"),
            InlineKeyboardButton("📈 Chart", url=pair_url),
            InlineKeyboardButton("🛡️ Deep Scan", callback_data=f"deep:{mint}"),
        ],
        [
            InlineKeyboardButton("DexScreener", url=pair_url),
            InlineKeyboardButton("Photon", url=photon),
            InlineKeyboardButton("Solscan", url=f"https://solscan.io/token/{mint}"),
        ],
    ])

# ------------------------------ TELEGRAM -------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "🚀 <b>JABIR RUNNER V3</b>\n<i>See Beyond The Chart.</i>\n\n"
            "Turo Solana token Contract Address (CA) domin scan.\n"
            "Quick tests: /testicles /pengu /grokius\nCommands: /help",
            parse_mode=ParseMode.HTML,
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "📖 <b>JABIR RUNNER V3 GUIDE</b>\n\n"
            "• Turo Solana CA domin scan.\n"
            "• /testicles, /pengu, /grokius domin gwaji.\n"
            "• Refresh yana sabunta market data.\n"
            "• Deep Scan yana neman holder data ta Helius.\n\n"
            "ATH yana farawa daga lokacin tracking. Bundle/dev/sniper/fake-holder "
            "attribution bai gama ginawa ba. Helius da Groq suna buƙatar API keys nasu.",
            parse_mode=ParseMode.HTML,
        )

async def perform_scan(
    mint: str, context: ContextTypes.DEFAULT_TYPE,
    status_message=None, chat_id: Optional[int] = None, deep: bool = False,
) -> None:
    async def notify(text: str) -> None:
        """Send a plain error notice without ever raising."""
        try:
            if status_message:
                await status_message.edit_text(text)
            elif chat_id is not None:
                await context.bot.send_message(chat_id, text)
        except Exception:
            log.exception("Could not send scan notice")

    try:
        if status_message:
            await status_message.edit_text("🔍 JABIR RUNNER\n\n⏳ Fetching market data...")
        data = await collect_token_data(mint, deep=deep)
        if status_message:
            await status_message.edit_text("🧠 Preparing trader report...")
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
        message = f"❌ {esc(exc)}"
        if status_message:
            await status_message.edit_text(message, parse_mode=ParseMode.HTML)
        elif chat_id is not None:
            await context.bot.send_message(chat_id, message, parse_mode=ParseMode.HTML)
    except DexRateLimited as exc:
        log.warning("Scan for %s stopped by DexScreener rate limit: %s", mint, exc)
        await notify("⏳ DexScreener is rate-limiting requests right now. Please try again in about a minute.")
    except DexScreenerError as exc:
        log.warning("Scan for %s failed, DexScreener error: %s", mint, exc)
        await notify("❌ DexScreener is not responding properly right now. Please try again shortly.")
    except Exception:
        log.exception("Scan failed for %s", mint)
        await notify("❌ Scan failed. Duba Render Logs da API keys/configuration.")

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
    status = await message.reply_text("🔍 JABIR RUNNER\n\nAn karɓi CA. Ana fara scan...")
    await perform_scan(mint, context, status_message=status, deep=False)

async def preset_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    if update.message:
        status = await update.message.reply_text(f"🔍 Starting {key.upper()} scan...")
        await perform_scan(TEST_TOKENS[key], context, status_message=status, deep=False)

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

    app = (
        Application.builder().token(BOT_TOKEN)
        .post_init(post_init).post_shutdown(post_shutdown).build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("testicles", testicles_cmd))
    app.add_handler(CommandHandler("pengu", pengu_cmd))
    app.add_handler(CommandHandler("grokius", grokius_cmd))
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
