"""
JABIR RUNNER V3 - FINAL MERGED (Claude Backend + Clean Report)
Solana token scanner | Python 3.11 | python-telegram-bot 21+
DexScreener + Helius + Groq | Render Webhook
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
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_PATH = "telegram-webhook"

DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
DEX_ORDERS_URL = "https://api.dexscreener.com/orders/v1/solana/{}"
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

DB_PATH = os.getenv("DB_PATH", "jabir_runner.db")
CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))
PAID_CACHE_TTL = int(os.getenv("PAID_CACHE_TTL", "300"))
PAID_FAIL_TTL = 30
NOT_FOUND_TTL = 20
CACHE_MAX = 500
MAX_HOLDER_PAGES = 10
HOLDER_PAGE_SIZE = 1000

DEX_MIN_INTERVAL = 1.2
DEX_MAX_ATTEMPTS = 5
DEX_BACKOFF_BASE = 2.0
DEX_MAX_BACKOFF = 20.0
DEX_MAX_RETRY_AFTER = 60.0
DEX_TOTAL_DEADLINE = 100.0

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
_paid_cache: dict[str, tuple[float, Optional[bool]]] = {}
_dex_semaphore: Optional[asyncio.Semaphore] = None
_dex_last_request: float = -1e9
_MISS = object()

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
            return f"{sign}${value / divisor:.2f}{
