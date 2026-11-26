"""
JABIR RUNNER V3 - TRADER EDITION (FIXED)
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
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

DB_PATH = os.getenv("DB_PATH", "jabir_runner.db")
CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))
HOLDERS_CACHE_TTL = 120
NARRATIVE_CACHE_TTL = 300

NARRATIVE_TIMEOUT = float(os.getenv("NARRATIVE_TIMEOUT", "6.0"))
TOP_HOLDERS_TIMEOUT = float(os.getenv("TOP_HOLDERS_TIMEOUT", "6.0"))
LAUNCH_TIMEOUT = float(os.getenv("LAUNCH_TIMEOUT", "20.0"))

CACHE_MAX = 500
MAX_HOLDER_PAGES = 10
HOLDER_PAGE_SIZE = 1000
LAUNCH_SIG_PAGES = 6
LAUNCH_TX_SAMPLE = 40
SNIPER_SLOTS = 3

DEX_MIN_INTERVAL = float(os.getenv("DEX_MIN_INTERVAL", "1.0"))
GECKO_MIN_INTERVAL = float(os.getenv("GECKO_MIN_INTERVAL", "1.5"))

BURN_ADDRESSES = {"1nc1nerator11111111111111111111111111111111"}
SOL_ADDR_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
BAR = "━━━━━━━━━━━━"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
for _noisy in ("httpx", "httpcore"): logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("jabir_runner")

_http: Optional[httpx.AsyncClient] = None
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_missing_cache: dict[str, tuple[float, None]] = {}
_holders_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
_narrative_cache: dict[str, tuple[float, str]] = {}
_dex_semaphore: Optional[asyncio.Semaphore] = None
_dex_last_request: float = -1e9

class DexScreenerError(RuntimeError): pass
class DexRateLimited(DexScreenerError): pass

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

def esc(value: Any) -> str: return html.escape(str(value), quote=True)

def short_addr(address: str) -> str:
    return f"{address[:6]}...{address[-6:]}" if len(address) > 14 else address

def money(value: Optional[float]) -> str:
    value = safe_num(value)
    if value is None: return "N/A"
    sign = "-" if value < 0 else ""
    value = abs(value)
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= divisor: return f"{sign}${value / divisor:.2f}{suffix}"
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

# ------------------------------ MINT LOCK (Restored) -------------------------
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
        db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, scans INTEGER DEFAULT 0)")

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
    return {"total": total, "today": today, "total_scans": total_scans}

# ------------------------------ HTTP / API -----------------------------------
async def request_json(method: str, url: str, **kwargs: Any) -> Any:
    if _http is None: raise RuntimeError("HTTP client is not initialized")
    response = await _http.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()

def _get_dex_semaphore() -> asyncio.Semaphore:
    global _dex_semaphore
    if _dex_semaphore is None: _dex_semaphore = asyncio.Semaphore(1)
    return _dex_semaphore

async def dex_get_json(url: str, max_attempts: int = 3) -> Any:
    global _dex_last_request
    async with _get_dex_semaphore():
        for attempt in range(1, max_attempts + 1):
            gap = DEX_MIN_INTERVAL - (time.monotonic() - _dex_last_request)
            if gap > 0: await asyncio.sleep(gap)
            try:
                response = await _http.get(url, timeout=20.0)
                _dex_last_request = time.monotonic()
                if response.status_code == 429:
                    await asyncio.sleep(2.0 * attempt)
                    continue
                if response.status_code >= 400: raise DexScreenerError(f"HTTP {response.status_code}")
                return response.json()
            except Exception as e:
                _dex_last_request = time.monotonic()
                if attempt == max_attempts: raise DexScreenerError(str(e))
                await asyncio.sleep(2.0 * attempt)

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
        txns = {"h1": {"buys": 0, "sells": 0}, "h24": {"buys": 0, "sells": 0}}
        for p in matching:
            for period in ("h1", "h24"):
                txns[period]["buys"] += safe_int(nested(p, "txns", period, "buys")) or 0
                txns[period]["sells"] += safe_int(nested(p, "txns", period, "sells")) or 0
        combined["txns"] = txns
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
        return {"mint_authority_active": int.from_bytes(raw[0:4], "little") == 1, "freeze_authority_active": int.from_bytes(raw[46:50], "little") == 1}
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
            if isinstance(owner, str) and amount: owners[owner] = owners.get(owner, 0) + amount
        data = {"entries": sorted(owners.items(), key=lambda kv: kv[1], reverse=True)} if owners else None
        _holders_cache[mint] = (time.time(), data)
        return data

async def fetch_holder_count(mint: str) -> Optional[int]:
    if not HELIUS_KEY: return None
    cursor = None
    total_accounts = 0
    for _ in range(MAX_HOLDER_PAGES):
        params = {"mint": mint, "limit": HOLDER_PAGE_SIZE, "options": {"showZeroBalance": False}}
        if cursor: params["cursor"] = cursor
        result = await helius_rpc("getTokenAccounts", params)
        if not result: break
        accounts = result.get("token_accounts") or []
        total_accounts += len(accounts)
        cursor = result.get("cursor")
        if not cursor or not accounts: break
    return total_accounts if total_accounts > 0 else None

async def fetch_creator_history(creator: str) -> dict[str, Any]:
    if not creator or not HELIUS_KEY: return {"checked": False, "rugs": 0}
    try:
        sigs = await helius_rpc("getSignaturesForAddress", [creator, {"limit": 20, "commitment": "confirmed"}])
        if not isinstance(sigs, list): return {"checked": False, "rugs": 0}
        rugs = 0
        for sig_info in sigs:
            if sig_info.get("err"): continue
            tx = await helius_rpc("getTransaction", [sig_info["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            if not tx: continue
            logs = tx.get("meta", {}).get("logMessages", [])
            if any("InitializeMint" in log for log in logs):
                mints = [b["mint"] for b in tx.get("meta", {}).get("postTokenBalances", []) if b.get("mint")]
                if not mints: continue
                try:
                    payload = await dex_get_json(DEX_TOKEN_URL.format(mints[0]))
                    pairs = payload.get("pairs", []) if payload else []
                    if pairs:
                        liq = safe_num(nested(pairs[0], "liquidity", "usd")) or 0
                        change = safe_num(nested(pairs[0], "priceChange", "h24")) or 0
                        if liq < 500 or change < -90: rugs += 1
                    else: rugs += 1
                except Exception: pass
        return {"checked": True, "rugs": rugs}
    except Exception: return {"checked": False, "rugs": 0}

async def fetch_launch_data(mint: str, supply_raw: int) -> Optional[dict[str, Any]]:
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
    
    if not reached_start: return {"limit_reached": True}

    oldest = [s for s in reversed(signatures) if not s.get("err")][:40]
    if not oldest: return None

    limiter = asyncio.Semaphore(8)
    async def load(sig): 
        async with limiter: return await helius_rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
    
    txs = [r for r in await asyncio.gather(*(load(s["signature"]) for s in oldest), return_exceptions=True) if isinstance(r, dict)]
    if not txs: return None
    
    launch_slot = safe_int(txs[0].get("slot"))
    creator = nested(txs[0], "transaction", "message", "accountKeys", 0, "pubkey")
    
    dev_holding = 0
    for tx in txs:
        for b in tx.get("meta", {}).get("postTokenBalances", []):
            if b.get("mint") == mint and b.get("owner") == creator:
                dev_holding = max(dev_holding, safe_int(nested(b, "uiTokenAmount", "amount")) or 0)

    return {"limit_reached": False, "creator": creator, "dev_holding_pct": (dev_holding / supply_raw * 100) if supply_raw else 0}

def socials_from_pair(pair: dict[str, Any]) -> dict[str, Any]:
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

def get_verdict(data: dict[str, Any]) -> tuple[str, str]:
    score = 0
    reasons = []
    creator_info = data.get("creator_info", {})
    if creator_info.get("checked") and creator_info.get("rugs", 0) > 0:
        return "⛔ AVOID", f"Creator is a RUGGER ({creator_info['rugs']} rugs)"

    launch = data.get("launch_data") or {}
    if launch.get("limit_reached"):
        score += 20; reasons.append("Dev UNKNOWN (+20 Risk)")
    else:
        dev_pct = launch.get("dev_holding_pct", 0)
        if dev_pct > 10: score += 30; reasons.append(f"Dev holds {dev_pct:.1f}%")

    auth = data.get("authorities") or {}
    if auth.get("mint_authority_active"): score += 40; reasons.append("Mint ACTIVE")
    if auth.get("freeze_authority_active"): score += 40; reasons.append("Freeze ACTIVE")
    if (data.get("liquidity") or 0) < 5000: score += 20; reasons.append("Low liquidity")
    if data.get("top_holders", {}).get("top10_percent", 0) > 40: score += 20; reasons.append("Top 10 >40%")

    if score >= 60: return "⛔ AVOID", reasons[0] if reasons else "High risk"
    elif score >= 20: return "🟡 WATCH", reasons[0] if reasons else "Some risks"
    else: return "🟢 BUY", "Looks safe"

async def generate_narrative(data: dict[str, Any]) -> str:
    if not GROQ_KEY: return "No AI narrative available."
    prompt = (
        f"Write a 2-3 sentence funny summary for this Solana token: {data['name']} (${data['symbol']}). "
        f"Age: {age_text(data.get('age_sec'))}. Liq: ${data.get('liquidity') or 0:,.0f}. "
        f"Dev status: {'UNKNOWN' if data.get('launch_data', {}).get('limit_reached') else 'KNOWN'}. "
        f"Use 1-2 emojis. Be brief. No financial advice."
    )
    try:
        payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7, "max_tokens": 120}
        res = await asyncio.wait_for(request_json("POST", GROQ_URL, headers={"Authorization": f"Bearer {GROQ_KEY}"}, json=payload), 6.0)
        return res["choices"][0]["message"]["content"].strip()
    except Exception: return "The AI is resting. Check the risks above."

def build_report(data: dict[str, Any], narrative: str, deep: bool) -> str:
    verdict_text, verdict_reason = get_verdict(data)
    auth = data.get("authorities") or {}
    launch = data.get("launch_data") or {}
    creator_info = data.get("creator_info") or {}
    socials = data.get("socials") or {}
    holders_count = data.get("holders_count")

    dev_line = "👨‍💻 Dev: ⚠️ UNKNOWN - Too many tx (+20 Risk)" if launch.get("limit_reached") else f"👨‍💻 Dev: 🔴 Holding {launch.get('dev_holding_pct', 0):.1f}%" if launch.get("dev_holding_pct", 0) > 10 else "👨‍💻 Dev: ✅ Sold Out" if launch.get("dev_holding_pct", 0) == 0 else f"👨‍💻 Dev: 🟢 Holding {launch.get('dev_holding_pct', 0):.1f}%"
    creator_line = f"🏭 Creator: 🔴 RUGGER ({creator_info['rugs']} rugs)" if creator_info.get("rugs", 0) > 0 else "🏭 Creator: ✅ Clean"

    x_val = f"[{socials['x'].split('//')[-1]}]({socials['x']}) ✅" if socials.get("x") else "❌ No X"
    tg_val = f"[{socials['tg'].split('//')[-1]}]({socials['tg']}) ✅" if socials.get("tg") else "❌ No TG"
    web_val = f"[{socials['web'].split('//')[-1]}]({socials['web']}) ✅" if socials.get("web") else "❌ No Web"

    return "\n".join([
        f"🚀 <b>JABIR RUNNER V3</b> | ${esc(data['symbol'])}",
        f"🪙 {esc(data['name'])} | {esc(data['dex'])} | 🌱 {age_text(data['age_sec'])}",
        f"<code>{esc(data['mint'])}</code>\n",
        f"💰 {money(data['price'])} | MC {money(data.get('mc')) or money(data.get('fdv'))}",
        f"💧 LP {money(data['liquidity'])} | Vol 24h {money(data['volume24'])} | Vol/LP {ratio_text(data['volume24'], data['liquidity'])}",
        f"📊 5m {pct(data.get('price_change_5m'))} | 1h {pct(data['price_change_1h'])} | 6h {pct(data.get('price_change_6h'))} | 24h {pct(data['price_change_24h'])}",
        f"🔄 1h 🅑{fmt_count(data['buys1h'])} Ⓢ{fmt_count(data['sells1h'])} | 24h 🅑{fmt_count(data['buys24'])} Ⓢ{fmt_count(data['sells24'])}\n",
        f"🔒 Mint: {'ACTIVE ⚠️' if auth.get('mint_authority_active') else 'revoked ✅'} | Freeze: {'ACTIVE ⚠️' if auth.get('freeze_authority_active') else 'revoked ✅'}",
        f"👥 Top 10: {data.get('top_holders', {}).get('top10_percent', 0):.1f}% | Holders: {fmt_count(holders_count)}\n",
        creator_line,
        dev_line,
        f"\n🧠 <i>{esc(narrative)}</i>\n",
        "📱 <b>Socials:</b>",
        f"🐦 X: {x_val}",
        f"💬 TG: {tg_val}",
        f"🌐 Web: {web_val}",
        f"📊 Chart: <a href='{data['url']}'>DEXScreener</a> • <a href='https://www.dextools.io/app/en/solana/pair-explorer/{data['pair']}'>DEXTools</a>\n",
        f"🎯 <b>{verdict_text}</b> - {esc(verdict_reason)}",
        BAR,
        "<i>JABIR RUNNER | See Beyond The Chart.</i>"
    ])

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
    except Exception: await status.edit_text("Could not load runners.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await asyncio.to_thread(record_user, update.effective_user.id, update.effective_user.username, update.effective_user.first_name)
        keyboard = [
            [InlineKeyboardButton("🔍 Scan Token", callback_data="btn_scan"), InlineKeyboardButton("📈 Trending", callback_data="btn_trending")],
            [InlineKeyboardButton("❓ How It Works", callback_data="btn_how"), InlineKeyboardButton("📢 Channel", url="https://t.me/yourchannel")],
            [InlineKeyboardButton("💬 Support", url="https://t.me/yoursupport")]
        ]
        await update.message.reply_text(
            "🚀 <b>JABIR RUNNER V3</b>\n<i>See Beyond The Chart.</i>\n\nSend a Solana token address (CA) to scan it.\n\nTop runners: /runners\nPrice alerts: /alert | /alerts | /delalert | /clearalerts\nCommands: /help",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("📖 <b>GUIDE</b>\n\n• Send a Solana CA to scan.\n• <code>/alert CA 0.001 above</code>\n• /runners lists top trending tokens.\n• Deep Scan reveals Creator History and Bundles.", parse_mode=ParseMode.HTML)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "btn_scan": await query.message.reply_text("Send the Solana CA now.")
    elif data == "btn_trending":
        class FakeMsg: 
            async def reply_text(self, *args, **kwargs): return await query.message.reply_text(*args, **kwargs)
        await runners_command(type('Update', (), {'effective_message': FakeMsg(), 'effective_user': query.from_user})(), context)
    elif data == "btn_how":
        await query.message.reply_text("❓ <b>HOW IT WORKS</b>\n\n🔒 <b>Mint:</b> Dev can print infinite tokens.\n❄️ <b>Freeze:</b> Dev can freeze your wallet.\n🏭 <b>Creator:</b> Checks if this wallet rugged before.\n👨‍💻 <b>Dev:</b> How much the developer holds.\n📦 <b>Bundles:</b> Checks for sniper bots at launch.", parse_mode=ParseMode.HTML)
    elif ":" in data:
        action, mint = data.split(":", 1)
        if SOL_ADDR_RE.fullmatch(mint): await perform_scan(mint, context, status_message=query.message, deep=(action == "deep"))

# ------------------------------ SCAN LOGIC -----------------------------------
async def perform_scan(mint: str, context: ContextTypes.DEFAULT_TYPE, status_message=None, chat_id: Optional[int] = None, deep: bool = False) -> None:
    async def notify(txt):
        if status_message: await status_message.edit_text(txt)
        elif chat_id: await context.bot.send_message(chat_id, txt)
        
    try:
        if status_message and deep: await status_message.edit_text("🔍 Deep Scanning...")
        
        pair = await fetch_pair(mint)
        if not pair: return await notify("❌ Token not found on DexScreener.")
        
        supply = await fetch_supply(mint)
        auth = await fetch_authorities(mint)
        
        # Heavy tasks
        holders_task = asyncio.create_task(fetch_holder_count(mint))
        
        launch_data = None
        if supply: launch_data = await fetch_launch_data(mint, supply["raw"])
            
        creator_info = {"checked": False}
        if launch_data and launch_data.get("creator"):
            creator_info = await fetch_creator_history(launch_data["creator"])
            
        top = await fetch_top_accounts(mint)
        holders_count = await holders_task
        
        base = pair.get("baseToken") or {}
        txns = pair.get("txns") or {}
        data = {
            "mint": mint, "name": base.get("name") or "Unknown", "symbol": base.get("symbol") or "?",
            "price": safe_num(pair.get("priceUsd")), "liquidity": safe_num(nested(pair, "liquidity", "usd")),
            "volume24": safe_num(nested(pair, "volume", "h24")), "fdv": safe_num(pair.get("fdv")),
            "mc": safe_num(pair.get("marketCap")), "dex": pair.get("dexId") or "N/A",
            "pair": pair.get("pairAddress"), "url": pair.get("url"),
            "price_change_5m": safe_num(nested(pair, "priceChange", "m5")),
            "price_change_1h": safe_num(nested(pair, "priceChange", "h1")),
            "price_change_6h": safe_num(nested(pair, "priceChange", "h6")),
            "price_change_24h": safe_num(nested(pair, "priceChange", "h24")),
            "buys1h": safe_int(nested(txns, "h1", "buys")), "sells1h": safe_int(nested(txns, "h1", "sells")),
            "buys24": safe_int(nested(txns, "h24", "buys")), "sells24": safe_int(nested(txns, "h24", "sells")),
            "created_ts": safe_num(pair.get("pairCreatedAt")), "age_sec": time.time() - (safe_num(pair.get("pairCreatedAt")) or 0)/1000 if pair.get("pairCreatedAt") else None,
            "authorities": auth, "launch_data": launch_data, "creator_info": creator_info,
            "socials": socials_from_pair(pair), "holders_count": holders_count
        }
        
        if supply and top:
            wallets = [amt / supply["raw"] * 100 for owner, amt in top["entries"] if owner not in BURN_ADDRESSES and is_on_curve(owner) is not False]
            data["top_holders"] = {"top10_percent": sum(wallets[:10]) if wallets else 0}
        else: data["top_holders"] = {"top10_percent": 0}
            
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
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ca))
    
    app.run_webhook(listen="0.0.0.0", port=PORT, url_path=WEBHOOK_PATH, webhook_url=f"{RENDER_URL}/{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET)

if __name__ == "__main__":
    main()
