
import os
import re
import json
import time
import asyncio
import threading
import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =====================================
# ENVIRONMENT VARIABLES
# =====================================

TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("BOT_TOKEN")
GROQ_KEY = os.environ.get("GROQ_API_KEY")

RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
WEBHOOK_PATH = "telegram-webhook"


# =====================================
# BRANDING
# =====================================

BOT_NAME = "JABIR RUNNER"
BOT_TAGLINE = "See Beyond The Chart."

GROQ_MODEL = "openai/gpt-oss-120b"

DEX_CACHE_TTL = 60
DEX_MAX_RETRIES = 2

DEX_CACHE = {}
DEX_LOCK = threading.Lock()


# =====================================
# NARRATIVE WEIGHTS
# =====================================

WEIGHTS = {
    "meme_simplicity": 15,
    "viral_post_evidence": 20,
    "strong_news": 20,
    "originality": 10,
    "momentum": 10,
    "longevity": 10,
    "community_authenticity": 10,
    "narrative_connection": 5,
}

PILLAR_LABELS = {
    "meme_simplicity": "Meme Simplicity",
    "viral_post_evidence": "Viral Post Evidence",
    "strong_news": "Strong News",
    "originality": "Originality",
    "momentum": "Momentum",
    "longevity": "Longevity",
    "community_authenticity": "Community Authenticity",
    "narrative_connection": "Narrative-Token Connection",
}


# =====================================
# GROQ SYSTEM PROMPT
# =====================================

SYSTEM_PROMPT = """
You are the evidence-based Narrative Intelligence Engine
of JABIR RUNNER.

Analyze the supplied DexScreener token data.

STRICT EVIDENCE RULES:

1. Use ONLY the supplied DexScreener information.
2. Never claim to have searched social media, news,
   websites, Telegram or other sources.
3. Never invent partnerships, viral posts, holders,
   audits, developer information or announcements.
4. Trading volume is NOT proof of community interest.
5. Volume/liquidity ratio is NOT proof of bullish momentum.
6. Missing evidence must be clearly identified.
7. Do not confuse missing data with proof of failure.
8. Do not claim a token is safe.
9. Be conservative with scores.
10. Narrative lifespan is uncertain without historical
    and external evidence. Never predict an exact lifespan.

SCORING:

Score each pillar from 0 to 10.

meme_simplicity:
A clear and understandable meme concept.

viral_post_evidence:
Actual viral social-media evidence.
If unavailable, score 0.

strong_news:
Verified news or partnerships.
If unavailable, score 0.

originality:
Distinctiveness of the concept.

momentum:
Only observable market activity. Do not treat volume
alone as proof of positive momentum.

longevity:
Evidence of lasting relevance. Missing history means
low confidence and a conservative score.

community_authenticity:
Verified community evidence. DexScreener alone cannot
establish community authenticity.

narrative_connection:
How clearly the token identity matches its narrative.

OUTPUT:

Return ONLY valid JSON. No markdown or code fences.

Use exactly these keys:

{
  "narrative": "One sentence",
  "catalysts": [],
  "scores": {
    "meme_simplicity": {
      "score": 0,
      "evidence": "Explanation",
      "evidence_type": "Verified/Missing/AI Interpretation"
    },
    "viral_post_evidence": {
      "score": 0,
      "evidence": "Explanation",
      "evidence_type": "Verified/Missing/AI Interpretation"
    },
    "strong_news": {
      "score": 0,
      "evidence": "Explanation",
      "evidence_type": "Verified/Missing/AI Interpretation"
    },
    "originality": {
      "score": 0,
      "evidence": "Explanation",
      "evidence_type": "Verified/Missing/AI Interpretation"
    },
    "momentum": {
      "score": 0,
      "evidence": "Explanation",
      "evidence_type": "Verified/Missing/AI Interpretation"
    },
    "longevity": {
      "score": 0,
      "evidence": "Explanation",
      "evidence_type": "Verified/Missing/AI Interpretation"
    },
    "community_authenticity": {
      "score": 0,
      "evidence": "Explanation",
      "evidence_type": "Verified/Missing/AI Interpretation"
    },
    "narrative_connection": {
      "score": 0,
      "evidence": "Explanation",
      "evidence_type": "Verified/Missing/AI Interpretation"
    }
  },
  "lifespan": {
    "estimate": "Uncertain",
    "reason": "Explanation"
  },
  "risks": [
    "Risk 1",
    "Risk 2",
    "Risk 3"
  ],
  "unverified": [],
  "confidence": "Low"
}

IMPORTANT:

- Score must be a number between 0 and 10.
- Do not calculate total score. Python calculates it.
- Use Hausa for explanations.
- Keep evidence concise and factual.
- Use only the supplied data.
- If a field is unsupported, say it is missing.
- Confidence must be High, Medium or Low.
- Lifespan estimate should be Hours, Days, Weeks,
  Long-term or Uncertain.
"""


# =====================================
# KEYBOARDS
# =====================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📖 How to Use",
                callback_data="how_to_use"
            )
        ]
    ])


def back_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏠 Back to Home",
                callback_data="back_home"
            )
        ]
    ])


# =====================================
# MESSAGES
# =====================================

def welcome_message():
    return (
        "🚀 JABIR RUNNER\n"
        "See Beyond The Chart.\n\n"
        "🔍 SOLANA TOKEN NARRATIVE INTELLIGENCE\n\n"
        "Barka da zuwa Jabir Runner!\n"
        "Ina taimaka maka wajen binciken narrative "
        "da market data na token.\n\n"
        "📊 FEATURES\n"
        "• Narrative Analysis\n"
        "• Weighted Scoring\n"
        "• Market Data\n"
        "• Evidence Assessment\n"
        "• Risk Awareness\n\n"
        "👇 Turo min Solana Contract Address (CA).\n\n"
        "⚠️ Narrative score ba ya tabbatar da tsaron token."
    )


def help_message():
    return (
        "📖 JABIR RUNNER GUIDE\n\n"
        "YADDA AKE AMFANI:\n\n"
        "1. Ka samo Contract Address.\n"
        "2. Ka turo CA ɗin nan.\n"
        "3. Zan ɗauko bayanai daga DexScreener.\n"
        "4. Groq zai tantance narrative.\n"
        "5. Za ka samu weighted score da risk report.\n\n"
        "📊 PILLARS:\n"
        "Meme, Viral, News, Originality, Momentum,\n"
        "Longevity, Community da Connection.\n\n"
        "🧾 EVIDENCE TYPES:\n"
        "Verified = An samu shaida.\n"
        "Missing = Ba a samu bayanin ba.\n"
        "AI Interpretation = Fassarar AI daga bayanan da ake da su.\n\n"
        "⚠️ Wannan bincike ne, ba buy/sell signal ba."
    )


def how_to_use_message():
    return (
        "📖 HOW TO USE JABIR RUNNER\n\n"
        "1. Buɗe DexScreener.\n"
        "2. Kwafi Contract Address.\n"
        "3. Turo CA ɗin nan.\n"
        "4. Jira market data.\n"
        "5. Jira Narrative Engine.\n"
        "6. Karanta rahoton da zai bayyana.\n\n"
        "💡 Yi amfani da CA daidai.\n\n"
        "⚠️ Ka tuna: Narrative analysis ba cikakken "
        "contract security audit ba ne."
    )


# =====================================
# TELEGRAM COMMANDS
# =====================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        welcome_message(),
        reply_markup=main_keyboard()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_message())


# =====================================
# BUTTON HANDLER
# =====================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "how_to_use":
        await query.edit_message_text(
            how_to_use_message(),
            reply_markup=back_keyboard()
        )

    elif query.data == "back_home":
        await query.edit_message_text(
            welcome_message(),
            reply_markup=main_keyboard()
        )


# =====================================
# DEXSCREENER API WITH CACHE
# =====================================

def get_dex_data(ca):

    now = time.time()

    # Check cache first
    with DEX_LOCK:
        cached = DEX_CACHE.get(ca)

        if cached and now - cached["time"] < DEX_CACHE_TTL:
            print(f"DexScreener cache hit: {ca}")
            return cached["pair"]

    url = (
        "https://api.dexscreener.com/latest/dex/tokens/"
        f"{ca}"
    )

    headers = {
        "Accept": "application/json",
        "User-Agent": "JabirRunner/2.0"
    }

    last_error = None

    # Serialize API calls to reduce request bursts
    with DEX_LOCK:

        # Another request may have filled the cache
        cached = DEX_CACHE.get(ca)

        if cached and time.time() - cached["time"] < DEX_CACHE_TTL:
            return cached["pair"]

        for attempt in range(DEX_MAX_RETRIES + 1):

            try:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=20
                )

                if response.status_code == 429:

                    retry_after = response.headers.get("Retry-After")

                    try:
                        wait = float(retry_after)
                    except (TypeError, ValueError):
                        wait = 2 ** (attempt + 1)

                    if attempt < DEX_MAX_RETRIES:
                        print(
                            f"DexScreener rate limited. "
                            f"Retrying after {wait}s"
                        )
                        time.sleep(min(wait, 15))
                        continue

                    raise RuntimeError(
                        "DexScreener rate limit exceeded. "
                        "Try again later."
                    )

                if response.status_code >= 500:
                    if attempt < DEX_MAX_RETRIES:
                        time.sleep(2 ** attempt)
                        continue

                response.raise_for_status()

                data = response.json()
                pairs = data.get("pairs") or []

                if not pairs:
                    return None

                def get_liquidity(pair):
                    try:
                        return float(
                            (pair.get("liquidity") or {}).get("usd") or 0
                        )
                    except (ValueError, TypeError):
                        return 0

                pair = max(pairs, key=get_liquidity)

                DEX_CACHE[ca] = {
                    "pair": pair,
                    "time": time.time()
                }

                print(f"DexScreener data cached: {ca}")

                return pair

            except Exception as error:
                last_error = error

                if attempt < DEX_MAX_RETRIES:
                    if isinstance(error, requests.RequestException):
                        time.sleep(2 ** attempt)
                        continue

                break

    raise RuntimeError(str(last_error or "DexScreener request failed"))


# =====================================
# DATA HELPERS
# =====================================

def safe_number(value):
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def money(value):
    number = safe_number(value)

    if number >= 1_000_000:
        return f"${number / 1_000_000:.2f}M"

    if number >= 1_000:
        return f"${number / 1_000:.2f}K"

    return f"${number:,.2f}"


def extract_dex_info(pair):

    base = pair.get("baseToken") or {}
    liquidity = (pair.get("liquidity") or {}).get("usd", 0)
    volume = (pair.get("volume") or {}).get("h24", 0)

    info = {
        "name": base.get("name", "Unknown"),
        "ticker": base.get("symbol", "?"),
        "chain": pair.get("chainId", "?"),
        "price": pair.get("priceUsd", "?"),
        "liquidity": safe_number(liquidity),
        "volume_24h": safe_number(volume),
        "fdv": safe_number(pair.get("fdv")),
        "dex": pair.get("dexId", "Unknown"),
        "pair_url": pair.get("url", ""),
        "pair_address": pair.get("pairAddress", ""),
        "base_token_address": base.get("address", ""),
        "quote_token": (pair.get("quoteToken") or {}).get("symbol", "?"),
        "price_change_24h": safe_number(
            (pair.get("priceChange") or {}).get("h24")
        ),
        "buys_24h": safe_number(
            (pair.get("txns") or {}).get("h24", {}).get("buys")
        ),
        "sells_24h": safe_number(
            (pair.get("txns") or {}).get("h24", {}).get("sells")
        ),
    }

    return info


# =====================================
# GROQ NARRATIVE ENGINE
# =====================================

def analyze_with_groq(ca, dex_info):

    if not GROQ_KEY:
        raise ValueError("GROQ_API_KEY is missing.")

    url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    f"Contract Address: {ca}\n\n"
                    f"DexScreener Data:\n"
                    f"{json.dumps(dex_info, ensure_ascii=False)}\n\n"
                    "Yi nazarin bayanan nan. Ka dawo da valid JSON kawai."
                ),
            },
        ],
        "response_format": {
            "type": "json_object"
        },
        "temperature": 0.2,
        "max_tokens": 2200,
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=90,
    )

    if not response.ok:
        raise RuntimeError(
            f"Groq HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    data = response.json()
    choices = data.get("choices") or []

    if not choices:
        raise RuntimeError("Groq returned no choices.")

    content = choices[0].get("message", {}).get("content")

    if not content:
        raise RuntimeError("Groq returned an empty response.")

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        raise RuntimeError("Groq returned invalid JSON.")

    if not isinstance(result, dict):
        raise RuntimeError("Groq returned an invalid report structure.")

    return result


# =====================================
# VALIDATE AND CALCULATE SCORES
# =====================================

def validate_analysis(analysis):

    scores = analysis.get("scores")

    if not isinstance(scores, dict):
        raise ValueError("Missing score structure.")

    validated = {}

    for key in WEIGHTS:

        item = scores.get(key)

        if not isinstance(item, dict):
            raise ValueError(f"Missing pillar: {key}")

        try:
            score = float(item.get("score"))
        except (ValueError, TypeError):
            raise ValueError(f"Invalid score: {key}")

        if not 0 <= score <= 10:
            raise ValueError(f"Score out of range: {key}")

        evidence = str(item.get("evidence") or "Ba a bayar da shaida ba.")

        evidence_type = str(
            item.get("evidence_type") or "Missing"
        )

        if evidence_type not in (
            "Verified",
            "Missing",
            "AI Interpretation"
        ):
            evidence_type = "AI Interpretation"

        validated[key] = {
            "score": score,
            "evidence": evidence,
            "evidence_type": evidence_type,
        }

    # Deterministic weighted calculation
    total = sum(
        validated[key]["score"] * weight / 100
        for key, weight in WEIGHTS.items()
    )

    if total >= 7.5:
        rating = "GREEN"
    elif total >= 5:
        rating = "YELLOW"
    else:
        rating = "RED"

    analysis["scores"] = validated
    analysis["total_score"] = round(total, 2)
    analysis["rating"] = rating

    return analysis


# =====================================
# PROFESSIONAL REPORT UI
# =====================================

def format_report(info, analysis):

    analysis = validate_analysis(analysis)

    name = info["name"]
    ticker = info["ticker"]
    chain = info["chain"]

    total = analysis["total_score"]
    rating = analysis["rating"]

    icons = {
        "GREEN": "🟢",
        "YELLOW": "🟡",
        "RED": "🔴",
    }

    lines = [
        "🚀 JABIR RUNNER REPORT",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🪙 {name} (${ticker})",
        f"⛓️ Chain: {chain}",
        f"📍 DEX: {info['dex']}",
        "",
        "📊 MARKET SNAPSHOT",
        f"💵 Price: ${info['price']}",
        f"💧 Liquidity: {money(info['liquidity'])}",
        f"📈 Volume 24h: {money(info['volume_24h'])}",
        f"🏷️ FDV: {money(info['fdv'])}",
        f"🔄 Price Change 24h: {info['price_change_24h']:+.2f}%",
        f"🟢 Buys: {int(info['buys_24h'])} | "
        f"🔴 Sells: {int(info['sells_24h'])}",
        "",
        "🧠 NARRATIVE INTELLIGENCE",
        str(analysis.get("narrative", "Ba a gano narrative ba.")),
        "",
        "⚖️ PILLAR SCORES",
    ]

    for key, weight in WEIGHTS.items():

        item = analysis["scores"][key]

        lines.extend([
            "",
            f"• {PILLAR_LABELS[key]}: "
            f"{item['score']:.1f}/10 ({weight}%)",
            f"  Evidence: {item['evidence']}",
            f"  Type: {item['evidence_type']}",
        ])

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{icons[rating]} TOTAL SCORE: {total:.2f}/10",
        f"🏁 CLASSIFICATION: {rating}",
        "",
        "📚 CATALYSTS",
    ])

    catalysts = analysis.get("catalysts") or []

    if catalysts:
        for catalyst in catalysts[:5]:
            lines.append(f"• {catalyst}")
    else:
        lines.append("• None verified in supplied data.")

    lifespan = analysis.get("lifespan") or {}

    lines.extend([
        "",
        "⏳ NARRATIVE LIFESPAN",
        f"Estimate: {lifespan.get('estimate', 'Uncertain')}",
        str(lifespan.get("reason", "Ba a samu isasshen bayani ba.")),
        "",
        "⚠️ TOP RISKS",
    ])

    risks = analysis.get("risks") or []

    if risks:
        for index, risk in enumerate(risks[:3], 1):
            lines.append(f"{index}. {risk}")
    else:
        lines.append("Ba a bayar da cikakken risk list ba.")

    lines.extend([
        "",
        "🔎 UNVERIFIED ITEMS",
    ])

    unverified = analysis.get("unverified") or []

    if unverified:
        for item in unverified[:8]:
            lines.append(f"• {item}")
    else:
        lines.append("• No additional items listed.")

    lines.extend([
        "",
        f"🎯 CONFIDENCE: {analysis.get('confidence', 'Low')}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📌 DATA LIMITATION",
        "Wannan bincike ya dogara ne da bayanan DexScreener "
        "da aka samu. Ba a tabbatar da social activity, "
        "holders, dev wallets ko contract security ba.",
        "",
        "⚠️ Kada ka yanke shawarar siye ko sayarwa "
        "bisa wannan report kawai.",
        "",
        f"🔗 DexScreener: {info['pair_url']}",
        "",
        "JABIR RUNNER | See Beyond The Chart."
    ])

    return "\n".join(lines)


# =====================================
# TELEGRAM MESSAGE SENDER
# =====================================

async def send_report(status, report):

    # Keep each Telegram message safely below 4096 chars
    limit = 3800

    chunks = []

    while len(report) > limit:

        split_at = report.rfind("\n", 0, limit)

        if split_at < 1:
            split_at = limit

        chunks.append(report[:split_at])
        report = report[split_at:].lstrip()

    chunks.append(report)

    await status.edit_text(chunks[0])

    for chunk in chunks[1:]:
        await status.get_bot().send_message(
            chat_id=status.chat_id,
            text=chunk,
            disable_web_page_preview=True
        )


# =====================================
# CONTRACT ADDRESS HANDLER
# =====================================

async def handle_ca(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    ca_match = re.search(
        r"(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})",
        text
    )

    if not ca_match:

        await update.message.reply_text(
            "❌ Ban gane CA ba.\n\n"
            "Ka turo Contract Address kawai."
        )

        return

    ca = ca_match.group(1)

    status = await update.message.reply_text(
        "🔍 JABIR RUNNER SCANNER\n\n"
        "✅ An karɓi Contract Address.\n"
        "⏳ Ina ɗauko bayanan DexScreener..."
    )

    # =================================
    # DEXSCREENER
    # =================================

    try:

        pair = await asyncio.to_thread(
            get_dex_data,
            ca
        )

    except Exception as error:

        print(f"DexScreener Error: {error}")

        await status.edit_text(
            "❌ DEXSCREENER ERROR\n\n"
            "An samu matsala wajen ɗauko bayanai.\n"
            "Idan rate limit ne, jira kaɗan kafin sake gwadawa."
        )

        return

    if not pair:

        await status.edit_text(
            "❌ TOKEN NOT FOUND\n\n"
            "Ba a samu token ɗin a DexScreener ba.\n"
            "Ka tabbatar CA ɗin daidai ne."
        )

        return

    # =================================
    # EXTRACT DATA
    # =================================

    info = extract_dex_info(pair)

    # =================================
    # GROQ ANALYSIS
    # =================================

    await status.edit_text(
        "🧠 JABIR RUNNER NARRATIVE ENGINE\n\n"
        "✅ Market data received.\n"
        "⏳ Ana nazarin narrative da evidence..."
    )

    try:

        analysis = await asyncio.to_thread(
            analyze_with_groq,
            ca,
            info
        )

        report = format_report(info, analysis)

        await send_report(status, report)

    except Exception as error:

        print(f"Groq Analysis Error: {error}")

        fallback = (
            "🚀 JABIR RUNNER MARKET REPORT\n\n"
            f"🪙 {info['name']} (${info['ticker']})\n"
            f"⛓️ Chain: {info['chain']}\n\n"
            f"💧 Liquidity: {money(info['liquidity'])}\n"
            f"📈 Volume 24h: {money(info['volume_24h'])}\n"
            f"🏷️ FDV: {money(info['fdv'])}\n\n"
            "⚠️ Narrative Analysis bai yi nasara ba.\n"
            "Ba a samar da narrative score ba.\n\n"
            "Duba Render Logs domin ganin ainihin kuskuren.\n\n"
            f"🔗 {info['pair_url']}"
        )

        await status.edit_text(
            fallback,
            disable_web_page_preview=True
        )


# =====================================
# START BOT
# =====================================

if __name__ == "__main__":

    if not TOKEN:
        raise ValueError("TELEGRAM_TOKEN is missing!")

    if not GROQ_KEY:
        raise ValueError("GROQ_API_KEY is missing!")

    if not RENDER_URL:
        raise ValueError("RENDER_EXTERNAL_URL is missing!")

    if not WEBHOOK_SECRET:
        raise ValueError("WEBHOOK_SECRET is missing!")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("help", help_cmd)
    )

    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_ca
        )
    )

    print("JABIR RUNNER v2 starting with Webhook...")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{RENDER_URL.rstrip('/')}/{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
        bootstrap_retries=-1,
    )
