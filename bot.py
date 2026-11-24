"""
JABIR RUNNER | Solana token scanner bot (v1)
Python 3.11 | python-telegram-bot v20+ | DexScreener (no key) | in-memory cache
Env var required: BOT_TOKEN
"""
import asyncio
import html
import logging
import math
import os
import re
import time
from typing import Any, Optional

import httpx
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("jabir_runner")

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
CACHE_TTL = 30  # seconds
CACHE_MAX = 500
SOL_ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")  # base58, no 0 O I l
ERR_MSG = "Token not found or API down"
BAR = "━━━━━━━━━━━━"

_cache: dict[str, tuple[float, dict]] = {}


# ----------------------------------------------------------------------------
# SMALL HELPERS
# ----------------------------------------------------------------------------
def num(x: Any) -> Optional[float]:
    """Safe float. Returns None if missing/invalid. Never invents data."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def dig(d: Any, *keys: str) -> Any:
    """Nested dict getter that returns None instead of raising."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


_SUB = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def format_price(n: Optional[float]) -> str:
    """$0.00445 | $0.0₅123 | $1.23 | $42.3K | $395M | $1.2B. None -> N/A."""
    n = num(n)
    if n is None:
        return "N/A"
    if n == 0:
        return "$0"
    neg = n < 0
    n = abs(n)
    sign = "-" if neg else ""

    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            s = f"{n / div:.1f}".rstrip("0").rstrip(".")
            return f"{sign}${s}{suffix}"
    if n >= 1:
        return f"{sign}${n:,.2f}"

    zeros = int(-math.floor(math.log10(n))) - 1  # leading zeros after "0."
    if zeros >= 4:
        digits = f"{n:.{zeros + 3}f}"[2 + zeros:]
        return f"{sign}$0.0{str(zeros).translate(_SUB)}{digits}"
    return f"{sign}${n:.{zeros + 3}f}"


def fmt_pct(p: Optional[float]) -> str:
    p = num(p)
    if p is None:
        return "N/A"
    return f"{p:+.1f}%" if abs(p) < 1000 else f"{p:+,.0f}%"


def fmt_age(sec: Optional[float]) -> str:
    if sec is None:
        return "N/A"
    sec = max(0, int(sec))
    if sec < 3600:
        return f"{max(1, sec // 60)}m"
    if sec < 86400:
        return f"{sec // 3600}h {(sec % 3600) // 60}m"
    return f"{sec // 86400}d"


def fmt_int(n: Optional[float]) -> str:
    n = num(n)
    return "N/A" if n is None else f"{int(n):,}"


def short_addr(a: str) -> str:
    return f"{a[:4]}...{a[-4:]}" if len(a) > 10 else a


def esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


# ----------------------------------------------------------------------------
# DATA
# ----------------------------------------------------------------------------
def parse_pair(pair: dict, address: str, now: Optional[float] = None) -> dict:
    """Normalize a raw DexScreener pair. Missing fields stay None."""
    now = now or time.time()

    created_ms = num(pair.get("pairCreatedAt"))
    created_ts = created_ms / 1000 if created_ms else None  # ms -> s
    age_sec = (now - created_ts) if created_ts else None

    info = pair.get("info") or {}
    websites = [w for w in (info.get("websites") or []) if isinstance(w, dict) and w.get("url")]
    socials = [s for s in (info.get("socials") or []) if isinstance(s, dict) and s.get("url")]

    def has_social(kind: str, domains: tuple) -> bool:
        for s in socials:
            if str(s.get("type", "")).lower() == kind:
                return True
            if any(dm in str(s.get("url", "")).lower() for dm in domains):
                return True
        return False

    d = {
        "address": address,
        "name": dig(pair, "baseToken", "name"),
        "symbol": dig(pair, "baseToken", "symbol"),
        "dex": pair.get("dexId"),
        "pair": pair.get("pairAddress"),
        "url": pair.get("url"),
        "price": num(pair.get("priceUsd")),
        "fdv": num(pair.get("fdv")),
        "mc": num(pair.get("marketCap")),
        "liq": num(dig(pair, "liquidity", "usd")),
        "vol24": num(dig(pair, "volume", "h24")),
        "chg24": num(dig(pair, "priceChange", "h24")),
        "chg1h": num(dig(pair, "priceChange", "h1")),
        "buys24": num(dig(pair, "txns", "h24", "buys")),
        "sells24": num(dig(pair, "txns", "h24", "sells")),
        "buys1h": num(dig(pair, "txns", "h1", "buys")),
        "sells1h": num(dig(pair, "txns", "h1", "sells")),
        "created_ts": created_ts,
        "age_sec": age_sec,
        "has_x": has_social("twitter", ("twitter.com", "x.com")),
        "has_tg": has_social("telegram", ("t.me", "telegram")),
        "has_web": bool(websites),
        "dex_paid": bool(websites or socials),  # proxy: DexScreener info exists
    }
    d["coverage"] = _coverage(d)
    return d


def _coverage(d: dict) -> float:
    core = ["price", "fdv", "mc", "liq", "vol24", "chg24", "buys24", "sells24", "created_ts", "dex"]
    return sum(d[k] is not None for k in core) / len(core)


def aggregate_pairs(pairs: list, address: str, now: Optional[float] = None) -> dict:
    """
    One token usually trades in many pools. Combine them so big tokens
    don't look 'thin' because we picked a tiny pool.
    - price / change / dex / links: from the most active pair (highest 24h vol)
    - liquidity, volume, txns: summed across pairs
    - MC / FDV: token-level, taken from the primary pair (fallback: any pair)
    - age: oldest pair; socials: any pair that has them
    """
    now = now or time.time()
    parsed = [parse_pair(p, address, now) for p in pairs]
    primary = max(parsed, key=lambda x: (x["vol24"] or 0, x["liq"] or 0))
    d = dict(primary)

    def total(k: str) -> Optional[float]:
        vals = [x[k] for x in parsed if x[k] is not None]
        return sum(vals) if vals else None

    for k in ("liq", "vol24", "buys24", "sells24", "buys1h", "sells1h"):
        d[k] = total(k)
    for k in ("mc", "fdv"):
        if d[k] is None:
            d[k] = next((x[k] for x in parsed if x[k] is not None), None)

    created = [x["created_ts"] for x in parsed if x["created_ts"]]
    if created:
        d["created_ts"] = min(created)
        d["age_sec"] = now - d["created_ts"]
    for k in ("has_x", "has_tg", "has_web", "dex_paid"):
        d[k] = any(x[k] for x in parsed)

    d["pairs"] = len(parsed)
    d["coverage"] = _coverage(d)
    return d


async def fetch_token_data(address: str) -> Optional[dict]:
    """Call DexScreener, pick the best Solana pair, return normalized dict or None."""
    now = time.time()
    hit = _cache.get(address)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    pairs = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(DEX_URL.format(address))
                r.raise_for_status()
                pairs = (r.json() or {}).get("pairs")
            break
        except Exception as e:  # network, HTTP, JSON - never crash
            log.warning("DexScreener attempt %s failed for %s: %s", attempt + 1, address, e)
            await asyncio.sleep(0.5)

    if not pairs or not isinstance(pairs, list):
        return None

    try:
        sol = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"]
        if not sol:
            return None
        own = [p for p in sol if dig(p, "baseToken", "address") == address]
        if own:
            data = aggregate_pairs(own, address)
        else:  # address only appears as a quote token: use the single most active pair
            best = max(sol, key=lambda p: num(dig(p, "volume", "h24")) or 0)
            data = aggregate_pairs([best], address)
    except Exception as e:
        log.exception("parse failure for %s: %s", address, e)
        return None

    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[address] = (now, data)
    return data


async def get_security_data(address: str) -> dict:
    """
    PLACEHOLDER. Plug Helius in here later (bundle %, dev holding, top 10, holders).
    Keep the same keys so build_report() needs no changes.
    Return real values only - never guess.
    """
    # TODO(Helius): e.g. getTokenLargestAccounts / DAS getAsset / enhanced txs
    return {
        "bundled": "N/A (Helius coming)",
        "dev": "N/A",
        "fake_holders": "N/A",
        "top10": "N/A",
        "holders": "N/A",
    }


# ----------------------------------------------------------------------------
# LOGIC
# ----------------------------------------------------------------------------
def _buy_ratio(d: dict) -> Optional[float]:
    b, s = d.get("buys24"), d.get("sells24")
    if b is None or s is None or (b + s) == 0:
        return None
    return b / (b + s)


def _risk_flags(d: dict) -> list[tuple[str, str]]:
    """Inferred risk from DexScreener data only. Returns [(level, text)]."""
    flags: list[tuple[str, str]] = []
    liq, vol, age = d["liq"], d["vol24"], d["age_sec"]
    mc_ref = d["mc"] if d["mc"] is not None else d["fdv"]

    if liq is not None and liq < 10_000:
        flags.append(("red", "Liq under $10K"))
    # LP/MC ratio only matters for small/mid caps; large caps normally have low ratios
    if liq is not None and mc_ref and 100_000 <= mc_ref <= 50_000_000:
        if liq / mc_ref < 0.02:
            flags.append(("red", f"Liq only {liq / mc_ref * 100:.1f}% of MC"))
        elif liq / mc_ref < 0.05:
            flags.append(("yellow", f"Thin liq ({liq / mc_ref * 100:.1f}% of MC)"))
    if age is not None and age < 3600:
        flags.append(("yellow", "Brand new pair (<1h)"))
    if vol is not None and mc_ref and vol > mc_ref * 5:
        flags.append(("yellow", "Vol 5x+ MC (wash/pump?)"))
    if vol is not None and d["fdv"] and vol < d["fdv"] * 0.01:
        flags.append(("yellow", "Thin vol vs FDV"))
    r = _buy_ratio(d)
    if r is not None and (d["buys24"] + d["sells24"]) >= 100 and r < 0.40:
        flags.append(("yellow", "Heavy sell pressure"))
    return flags


def _risk_line(d: dict, force_red: Optional[str] = None) -> str:
    flags = _risk_flags(d)
    if force_red:
        return f"🔴 {force_red}"
    if any(lv == "red" for lv, _ in flags):
        txt = " · ".join(t for lv, t in flags if lv == "red")
        return f"🔴 {txt}"
    if flags:
        return "🟡 " + " · ".join(t for _, t in flags[:2])
    return "🟢 No red flags in market data (bundles/holders unchecked)"


def calculate_verdict(d: dict) -> tuple[str, str, str]:
    """Returns (risk, verdict, reason). Risk/verdict strings include emoji."""
    liq, vol, chg, age = d["liq"], d["vol24"], d["chg24"], d["age_sec"]
    mc_ref = d["mc"] if d["mc"] is not None else d["fdv"]  # FDV only as fallback for logic

    # 1) Low liquidity trap
    if liq is not None and mc_ref is not None and liq < 10_000 and mc_ref > 100_000:
        reason = f"Low liquidity trap (LP {format_price(liq)} vs MC {format_price(mc_ref)})"
        return _risk_line(d, force_red="Low liquidity trap"), "🔴 AVOID", reason

    # 2) New + volume way above MC
    if age is not None and vol is not None and mc_ref is not None and age < 3600 and vol > mc_ref * 2:
        return _risk_line(d), "🟡 WATCH", "Possible wash / pump (vol > 2x MC, under 1h old)"

    # 3) Overheated
    if chg is not None and liq is not None and vol is not None and chg > 100 and liq < vol:
        return _risk_line(d), "🟡 WATCH", f"Overheated ({fmt_pct(chg)} 24h, vol > liq)"

    # 4) Not enough data
    if d["coverage"] < 0.5:
        return "🟡 Too little data to judge", "⚪ NEED MORE DATA", f"Only {d['coverage'] * 100:.0f}% of fields available"

    # 5) Momentum
    risk = _risk_line(d)
    ratio = _buy_ratio(d)
    if (
        not risk.startswith("🔴")
        and chg is not None and chg > 0
        and ratio is not None and ratio >= 0.55
        and liq is not None and liq >= 25_000
        and age is not None and age >= 3600
    ):
        return risk, "🟢 BUY", f"Up {fmt_pct(chg)}, {ratio * 100:.0f}% buys, LP {format_price(liq)}"

    parts = [f"24h {fmt_pct(chg)}"]
    if ratio is not None:
        parts.append(f"{ratio * 100:.0f}% buys")
    if liq is not None and liq < 25_000:
        parts.append(f"LP {format_price(liq)}")
    if age is not None and age < 3600:
        parts.append("under 1h old")
    return risk, "🟡 WATCH", "No momentum confirmation (" + ", ".join(parts) + ")"


def _narrative(d: dict) -> str:
    """Max 2 lines. Built only from available data - no invented story."""
    name = esc((d["name"] or "Unknown")[:24])
    dex = esc((d["dex"] or "N/A").capitalize())
    links = [lbl for lbl, ok in (("X", d["has_x"]), ("Web", d["has_web"]), ("TG", d["has_tg"])) if ok]
    line1 = f"{name} on {dex}, {fmt_age(d['age_sec'])} old; " + (
        "links: " + "/".join(links) if links else "no socials listed"
    ) + "."

    mc_ref = d["mc"] if d["mc"] is not None else d["fdv"]
    ratio = _buy_ratio(d)
    heat = []
    if d["vol24"] is not None and mc_ref:
        heat.append(f"vol {d['vol24'] / mc_ref:.1f}x MC")
    if ratio is not None:
        heat.append(f"{ratio * 100:.0f}% buys 24h")
    line2 = ("Heat: " + ", ".join(heat) + ".") if heat else "Not enough data to judge the story."
    return f"{line1}\n{line2}"


def build_report(data: dict, sec: Optional[dict] = None) -> str:
    """Exact output format, 20 non-blank lines. HTML parse mode."""
    sec = sec or {}
    sym = esc((data["symbol"] or "???").upper())
    name = esc((data["name"] or "Unknown")[:24])
    dex = esc((data["dex"] or "N/A").capitalize())
    risk, verdict, reason = calculate_verdict(data)

    mc_txt = format_price(data["mc"])
    if data["mc"] is None and data["fdv"] is not None:
        mc_txt = f"N/A (FDV {format_price(data['fdv'])})"

    b1, s1 = data["buys1h"], data["sells1h"]
    x = "✅" if data["has_x"] else "❌"
    w = "✅" if data["has_web"] else "❌"
    t = "✅" if data["has_tg"] else "❌"

    addr = data["address"]
    pair = data["pair"] or addr
    chart = f"https://dexscreener.com/solana/{pair}"
    dex_link = data["url"] or f"https://dexscreener.com/solana/{addr}"
    photon = f"https://photon-sol.tinyastro.io/en/lp/{pair}"

    lines = [
        f"🚀 <b>JABIR RUNNER</b> | ${sym}",
        BAR,
        f"🪙 {name} (${sym}) | 🟣 {dex} | 🌱 {fmt_age(data['age_sec'])}",
        f"📍 CA: <code>{esc(short_addr(addr))}</code>",
        "",
        "📊 <b>STATS</b>",
        f"💰 {format_price(data['price'])} ({fmt_pct(data['chg24'])}) | MC: {mc_txt} | ATH: N/A",
        f"💧 LP: {format_price(data['liq'])} | 📊 Vol: {format_price(data['vol24'])} | "
        f"1H: {fmt_pct(data['chg1h'])} 🅑{fmt_int(b1)} Ⓢ{fmt_int(s1)}",
        "",
        f"🧠 <b>NARRATIVE:</b> {_narrative(data)}",
        "",
        "🔒 <b>SECURITY</b>",
        f"├ Bundled: {esc(sec.get('bundled', 'N/A (Helius coming)'))} | Dev: {esc(sec.get('dev', 'N/A'))}",
        f"├ Fake Holders: {esc(sec.get('fake_holders', 'N/A'))} | Top 10: {esc(sec.get('top10', 'N/A'))}",
        f"└ DEX Paid: {'✅' if data['dex_paid'] else '❌'}",
        "",
        f"👥 <b>HOLDERS:</b> {esc(sec.get('holders', 'N/A'))}",
        f"🔗 Socials: X {x} • Web {w} • TG {t}",
        "",
        f"⚠️ <b>RISK:</b> {esc(risk)}",
        f"🎯 <b>VERDICT:</b> {verdict} - {esc(reason)}",
        "",
        f'🔗 <a href="{esc(chart)}">Chart</a> | <a href="{esc(dex_link)}">DexScreener</a> | '
        f'<a href="{esc(photon)}">Photon</a>',
        BAR,
        "JABIR RUNNER | See Beyond The Chart.",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# TELEGRAM HANDLERS
# ----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text(
            "🚀 <b>JABIR RUNNER</b>\nPaste a Solana token address (CA) and I'll scan it.\n"
            "<i>See Beyond The Chart.</i>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.warning("start failed: %s", e)


async def handle_ca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return
    m = SOL_ADDR_RE.search(msg.text)
    if not m:
        return  # not a CA, stay quiet in groups
    address = m.group(0)

    try:
        await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    except Exception:
        pass

    try:
        data = await fetch_token_data(address)
        if not data:
            await msg.reply_text(ERR_MSG)
            return
        try:
            sec = await get_security_data(address)
        except Exception as e:
            log.warning("security placeholder failed: %s", e)
            sec = {}
        report = build_report(data, sec)
        await msg.reply_text(
            report,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.exception("handle_ca failed for %s: %s", address, e)
        try:
            await msg.reply_text(ERR_MSG)
        except Exception:
            pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error: %s", context.error)


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN env var is missing")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ca))
    app.add_error_handler(on_error)

    log.info("Jabir Runner is live")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
