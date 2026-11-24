
import os
import re
import json
import asyncio
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

GROQ_MODEL = "openai/gpt-oss-120b"

# =====================================
# BOT BRANDING
# =====================================

BOT_NAME = "JABIR RUNNER"
BOT_TAGLINE = "See Beyond The Chart."

# =====================================
# SCORING WEIGHTS
# =====================================

PILLARS = {
    "Meme Simplicity": 15,
    "Viral Post Evidence": 20,
    "Strong News Evidence": 20,
    "Originality": 10,
    "Momentum": 10,
    "Longevity": 10,
    "Community Authenticity": 10,
    "Narrative-Token Connection": 5,
}

# =====================================
# GROQ SYSTEM PROMPT
# =====================================

SYSTEM_PROMPT = """
You are JABIR RUNNER, a careful Solana token narrative analyst.

Your job is to analyze token narratives using the supplied
DexScreener market data.

IMPORTANT SOURCE LIMITATION:

The only factual source supplied to you is DexScreener data.
You have NOT searched X, Telegram, websites, news, exchanges,
holder analytics, blockchain explorers or project documents.

Never pretend that you searched those sources.

Never invent:
- Social media posts or follower counts.
- Partnerships or exchange listings.
- Physical products or brand achievements.
- Community size or authenticity.
- Developer activity or holder distribution.
- Audits or contract safety.
- Historical performance not supplied in the data.

A token name alone is not proof of a real-world brand,
community, cultural influence or established narrative.

If a real-world brand connection is not supported by
the supplied evidence, describe it as unverified.

EVIDENCE TYPES:

Every pillar must have one of these evidence types:

1. Verified:
   Directly supported by supplied DexScreener data.

2. AI Interpretation:
   A cautious interpretation of supplied information.
   It is not an independently verified fact.

3. Missing:
   No meaningful evidence is available.

CRITICAL SCORING RULE:

If evidence is Missing, set that pillar's score to null.
Do NOT give it 0/10.

A missing pillar is excluded from the weighted score.
Do not punish a token for data DexScreener does not provide.

Only score a pillar when there is meaningful evidence.

SCORING PILLARS:

1. Meme Simplicity: 15%
2. Viral Post Evidence: 20%
3. Strong News Evidence: 20%
4. Originality: 10%
5. Momentum: 10%
6. Longevity: 10%
7. Community Authenticity: 10%
8. Narrative-Token Connection: 5%

MOMENTUM RULES:

Use the supplied market data carefully.

Volume and liquidity are denominated in USD.
Never label them as SOL.

Trading volume is not proof of community authenticity.
Buys exceeding sells do not prove organic demand.
Price increase is not proof of a news catalyst.
High volume is not automatically bullish.

You may score Momentum using price change, volume,
liquidity and buy/sell activity, but explain the limitations.

NARRATIVE INTELLIGENCE:

Explain the possible narrative, audience and cultural meaning
only when supported by evidence.

If the data only gives a token name and ticker,
do not invent a detailed brand story.

If external brand information is unavailable,
say that the real-world narrative requires verification.

CATALYSTS:

Only list verified catalysts if actual evidence exists.

A price increase, trading volume or buy/sell activity
is market activity, NOT a verified news catalyst.

CLASSIFICATION:

Do not calculate the final weighted score yourself.
Python will calculate it.

List verified negative evidence separately.
Only include negative evidence that is directly supported
by the supplied data.

Do not label missing data as negative evidence.

RISKS:

List meaningful risks supported by the available data.
Clearly distinguish verified risks from unverified concerns.

NARRATIVE LIFESPAN:

Use "Uncertain" when there is insufficient historical evidence.
Do not predict lifespan confidently from 24-hour data.

HUMOR:

Include one short, clean Hausa joke related to charts,
trading or research.

Keep it friendly and harmless.
Do not joke about financial losses or encourage trading.

LANGUAGE:

Write the analysis in natural Hausa.
Keep technical terms understandable.

OUTPUT:

Return ONLY valid JSON. Do not use Markdown fences.

Use exactly this structure:

{
  "narrative": "Hausa narrative explanation",
  "audience": "Known audience or Unverified",
  "cultural_relevance": "Supported explanation or Unverified",
  "brand_connection": "Supported explanation or Unverified",
  "catalysts": [
    {
      "item": "Catalyst",
      "evidence": "Evidence",
      "type": "Verified"
    }
  ],
  "scores": {
    "Meme Simplicity": {
      "score": 0,
      "evidence": "Explanation",
      "type": "AI Interpretation"
    },
    "Viral Post Evidence": {
      "score": null,
      "evidence": "No social data supplied",
      "type": "Missing"
    },
    "Strong News Evidence": {
      "score": null,
      "evidence": "No news data supplied",
      "type": "Missing"
    },
    "Originality": {
      "score": null,
      "evidence": "Explanation",
      "type": "Missing"
    },
    "Momentum": {
      "score": 0,
      "evidence": "Market data explanation",
      "type": "Verified"
    },
    "Longevity": {
      "score": null,
      "evidence": "No historical data supplied",
      "type": "Missing"
    },
    "Community Authenticity": {
      "score": null,
      "evidence": "No community data supplied",
      "type": "Missing"
    },
    "Narrative-Token Connection": {
      "score": null,
      "evidence": "Explanation",
      "type": "Missing"
    }
  },
  "verified_negative_evidence": [],
  "risks": [
    {
      "item": "Risk",
      "type": "Verified"
    }
  ],
  "unverified_items": [
    "Social media activity",
    "Community authenticity",
    "Holder distribution",
    "Developer wallets",
    "Contract security"
  ],
  "lifespan": {
    "estimate": "Uncertain",
    "reason": "Explanation"
  },
  "humor": "One short Hausa joke",
  "limitations": "Explain what this analysis cannot verify"
}

IMPORTANT:
- Replace example scores with your actual assessment.
- Use null for missing scores.
- Scores must be between 0 and 10.
- Never use 0 just because evidence is missing.
- If a pillar has no meaningful evidence, use null and Missing.
- Never invent a catalyst.
- Never claim a project is safe.
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
# MESSAGE TEMPLATES
# =====================================

def welcome_message():
    return (
        "🚀 <b>JABIR RUNNER</b>\n"
        "<i>See Beyond The Chart.</i>\n\n"
        "🔍 <b>Solana Token Narrative Intelligence</b>\n\n"
        "Barka da zuwa Jabir Runner!\n"
        "Ni ne mataimakinka wajen binciken "
        "narrative da bayanan token.\n\n"
        "📊 <b>Abin da zan iya yi:</b>\n"
        "• Token Narrative Analysis\n"
        "• Evidence-Based Scoring\n"
        "• Market Data from DexScreener\n"
        "• Risk Awareness\n"
        "• Narrative Intelligence\n\n"
        "👇 <b>Yadda za ka fara:</b>\n"
        "Turo min Contract Address (CA).\n\n"
        "⚠️ <i>Narrative score ba ya tabbatar da "
        "cewa token yana da aminci.</i>"
    )


def help_message():
    return (
        "📖 <b>JABIR RUNNER GUIDE</b>\n\n"
        "Yadda nake aiki:\n\n"
        "1. Ka samo Contract Address (CA).\n"
        "2. Ka turo min CA ɗin.\n"
        "3. Zan ɗauko bayanai daga DexScreener.\n"
        "4. Groq AI zai tantance narrative.\n"
        "5. Zan lissafa score bisa bayanan da aka samu.\n\n"
        "📊 <b>Narrative Pillars:</b>\n"
        "Meme, Viral, News, Originality, Momentum, "
        "Longevity, Community da Connection.\n\n"
        "🧠 Missing data ba ya zama zero score.\n"
        "📊 Data Coverage zai nuna yawan bayanan da aka samu.\n\n"
        "⚠️ Wannan Narrative Analysis ne kawai."
    )


def how_to_use_message():
    return (
        "📖 <b>HOW TO USE JABIR RUNNER</b>\n\n"
        "1. Buɗe DexScreener ka samo CA.\n"
        "2. Turo CA ɗin nan.\n"
        "3. Zan ɗauko bayanan token.\n"
        "4. Groq AI zai tantance narrative.\n"
        "5. Za ka samu score, evidence da risks.\n\n"
        "⚠️ Ka tabbatar CA ɗin da ka turo daidai ne."
    )


# =====================================
# TELEGRAM COMMANDS
# =====================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            welcome_message(),
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            help_message(),
            parse_mode="HTML"
        )


# =====================================
# BUTTON HANDLER
# =====================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "how_to_use":
        await query.edit_message_text(
            how_to_use_message(),
            parse_mode="HTML",
            reply_markup=back_keyboard()
        )

    elif query.data == "back_home":
        await query.edit_message_text(
            welcome_message(),
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )


# =====================================
# SAFE DATA HELPERS
# =====================================

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def format_money(value):
    value = safe_float(value)

    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"

    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"

    if value >= 1_000:
        return f"${value / 1_000:.2f}K"

    return f"${value:,.2f}"


def format_price(value):
    value = safe_float(value)

    if value == 0:
        return "Unavailable"

    if value < 0.000001:
        return f"${value:.10f}"

    if value < 0.01:
        return f"${value:.8f}"

    return f"${value:.6f}"


# =====================================
# DEXSCREENER API
# =====================================

def get_dex_data(ca):
    url = (
        "https://api.dexscreener.com/latest/dex/tokens/"
        f"{ca}"
    )

    response = requests.get(
        url,
        timeout=25,
        headers={"User-Agent": "JABIR-RUNNER/1.0"}
    )

    response.raise_for_status()

    data = response.json()
    pairs = data.get("pairs") or []

    if not pairs:
        return None

    # Select pair with highest USD liquidity.
    pair = max(
        pairs,
        key=lambda p: safe_float(
            (p.get("liquidity") or {}).get("usd")
        )
    )

    return pair


# =====================================
# MARKET DATA EXTRACTION
# =====================================

def extract_market_data(ca, pair):
    base_token = pair.get("baseToken") or {}
    quote_token = pair.get("quoteToken") or {}

    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    price_change = pair.get("priceChange") or {}
    txns = pair.get("txns") or {}
    h24 = txns.get("h24") or {}

    return {
        "contract_address": ca,
        "name": base_token.get("name") or "Unknown",
        "ticker": base_token.get("symbol") or "?",
        "chain": pair.get("chainId") or "Unknown",
        "dex": pair.get("dexId") or "Unknown",
        "price_usd": safe_float(pair.get("priceUsd")),
        "liquidity_usd": safe_float(liquidity.get("usd")),
        "volume_24h_usd": safe_float(volume.get("h24")),
        "fdv_usd": safe_float(pair.get("fdv")),
        "market_cap_usd": safe_float(pair.get("marketCap")),
        "price_change_24h": safe_float(price_change.get("h24")),
        "buys_24h": int(safe_float(h24.get("buys"))),
        "sells_24h": int(safe_float(h24.get("sells"))),
        "pair_created_at": pair.get("pairCreatedAt"),
        "base_address": base_token.get("address"),
        "quote_symbol": quote_token.get("symbol"),
        "url": pair.get("url") or "",
    }


# =====================================
# GROQ AI API
# =====================================

def analyze_with_groq(market_data):
    if not GROQ_KEY:
        raise ValueError("GROQ_API_KEY is missing in Render.")

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
                    "Analyze this token using ONLY the supplied data.\n"
                    "Remember: all liquidity and volume values are USD.\n"
                    "Return valid JSON only.\n\n"
                    f"{json.dumps(market_data, ensure_ascii=False)}"
                ),
            },
        ],
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

    content = (
        choices[0]
        .get("message", {})
        .get("content")
    )

    if not content:
        raise RuntimeError("Groq returned an empty response.")

    # Remove accidental Markdown fences if the model adds them.
    content = content.strip()

    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content).strip()
        content = re.sub(r"```$", "", content).strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Groq returned invalid JSON: {content[:300]}"
        ) from exc


# =====================================
# VALIDATE SCORES AND CALCULATE TOTAL
# =====================================

def calculate_score(analysis):
    scores = analysis.get("scores") or {}

    available_weight = 0
    weighted_points = 0
    verified_count = 0
    missing_pillars = []

    clean_scores = {}

    for pillar, weight in PILLARS.items():
        item = scores.get(pillar) or {}

        score = item.get("score")
        evidence = str(item.get("evidence") or "Babu cikakkiyar hujja.")
        evidence_type = str(item.get("type") or "Missing")

        if evidence_type not in (
            "Verified",
            "AI Interpretation",
            "Missing"
        ):
            evidence_type = "Missing"

        # Missing evidence must never become zero.
        if evidence_type == "Missing" or score is None:
            score = None
            evidence_type = "Missing"
            missing_pillars.append(pillar)

        else:
            score = safe_float(score, -1)

            if score < 0 or score > 10:
                score = None
                evidence_type = "Missing"
                missing_pillars.append(pillar)

            else:
                available_weight += weight
                weighted_points += score * weight

                if evidence_type == "Verified":
                    verified_count += 1

        clean_scores[pillar] = {
            "score": score,
            "weight": weight,
            "evidence": evidence,
            "type": evidence_type,
        }

    if available_weight > 0:
        total_score = weighted_points / available_weight
    else:
        total_score = None

    coverage = available_weight

    # Confidence is based on evidence coverage.
    # With DexScreener as the only source, High is not allowed.
    if coverage < 60:
        confidence = "Low"
    else:
        confidence = "Medium"

    verified_negative = analysis.get(
        "verified_negative_evidence"
    ) or []

    # Classification rules:
    # Missing evidence is never a reason for RED.
    if coverage < 60 or total_score is None:
        classification = "NEED MORE DATA"

    elif verified_negative and total_score < 5:
        classification = "RED"

    elif total_score >= 7.5 and coverage >= 75:
        classification = "GREEN"

    else:
        classification = "YELLOW"

    return {
        "scores": clean_scores,
        "total_score": total_score,
        "coverage": coverage,
        "confidence": confidence,
        "classification": classification,
        "missing_pillars": missing_pillars,
        "verified_negative": verified_negative,
        "verified_count": verified_count,
    }


# =====================================
# REPORT FORMATTING
# =====================================

def format_report(market, analysis):
    result = calculate_score(analysis)

    name = market["name"]
    ticker = market["ticker"]
    chain = market["chain"]
    dex = market["dex"]

    price = format_price(market["price_usd"])
    liquidity = format_money(market["liquidity_usd"])
    volume = format_money(market["volume_24h_usd"])
    fdv = format_money(market["fdv_usd"])

    change = market["price_change_24h"]
    change_text = f"{change:+.2f}%"

    buys = market["buys_24h"]
    sells = market["sells_24h"]

    total_score = result["total_score"]

    if total_score is None:
        score_text = "N/A"
    else:
        score_text = f"{total_score:.2f}/10"

    classification = result["classification"]

    if classification == "GREEN":
        class_icon = "🟢"
    elif classification == "RED":
        class_icon = "🔴"
    else:
        class_icon = "🟡"

    lines = [
        "🚀 JABIR RUNNER REPORT",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🪙 {name} (${ticker})",
        f"⛓️ Chain: {chain}",
        f"📍 DEX: {dex}",
        "",
        "📊 MARKET SNAPSHOT",
        f"💵 Price: {price}",
        f"💧 Liquidity: {liquidity} USD",
        f"📈 Volume 24h: {volume} USD",
        f"🏷️ FDV: {fdv}",
        f"🔄 Price Change 24h: {change_text}",
        f"🟢 Buys: {buys} | 🔴 Sells: {sells}",
        "",
        "🧠 NARRATIVE INTELLIGENCE",
        str(analysis.get("narrative") or "Ba a samu cikakken bayani ba."),
        "",
        "👥 AUDIENCE",
        str(analysis.get("audience") or "Unverified"),
        "",
        "🌍 CULTURAL RELEVANCE",
        str(analysis.get("cultural_relevance") or "Unverified"),
        "",
        "🏷️ BRAND CONNECTION",
        str(analysis.get("brand_connection") or "Unverified"),
        "",
        "⚖️ PILLAR SCORES",
        "",
    ]

    for pillar, item in result["scores"].items():
        score = item["score"]
        weight = item["weight"]

        if score is None:
            score_text_pillar = "N/A"
        else:
            score_text_pillar = f"{score:.1f}/10"

        lines.extend([
            f"• {pillar}: {score_text_pillar} ({weight}%)",
            f"  Evidence: {item['evidence']}",
            f"  Type: {item['type']}",
            "",
        ])

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 WEIGHTED SCORE: {score_text}",
        f"📡 DATA COVERAGE: {result['coverage']}%",
        f"{class_icon} CLASSIFICATION: {classification}",
        f"🎯 CONFIDENCE: {result['confidence']}",
        "",
        "📚 CATALYSTS",
    ])

    catalysts = analysis.get("catalysts") or []

    if catalysts:
        for catalyst in catalysts[:5]:
            if isinstance(catalyst, dict):
                item = catalyst.get("item", "Unknown")
                evidence = catalyst.get("evidence", "")
                evidence_type = catalyst.get("type", "Unverified")

                lines.append(f"• {item}")
                if evidence:
                    lines.append(f"  Evidence: {evidence}")
                lines.append(f"  Type: {evidence_type}")
            else:
                lines.append(f"• {catalyst}")
    else:
        lines.append("• Babu verified catalyst a cikin bayanan da aka bayar.")

    lines.extend([
        "",
        "⏳ NARRATIVE LIFESPAN",
    ])

    lifespan = analysis.get("lifespan") or {}

    lines.append(
        f"Estimate: {lifespan.get('estimate', 'Uncertain')}"
    )
    lines.append(
        str(lifespan.get('reason') or "Babu isasshen tarihi.")
    )

    lines.extend([
        "",
        "⚠️ TOP RISKS",
    ])

    risks = analysis.get("risks") or []

    if risks:
        for index, risk in enumerate(risks[:3], start=1):
            if isinstance(risk, dict):
                item = risk.get("item", "Unspecified risk")
                risk_type = risk.get("type", "Unverified")
                lines.append(f"{index}. {item} ({risk_type})")
            else:
                lines.append(f"{index}. {risk}")
    else:
        lines.append("1. Ba a samu cikakken bayanin haɗari ba.")

    lines.extend([
        "",
        "🔎 UNVERIFIED ITEMS",
    ])

    unverified = analysis.get("unverified_items") or []

    # Always include missing pillars.
    for pillar in result["missing_pillars"]:
        label = f"{pillar}: babu isasshiyar hujja"

        if label not in unverified:
            unverified.append(label)

    if unverified:
        for item in unverified[:10]:
            lines.append(f"• {item}")
    else:
        lines.append("• Babu ƙarin abubuwan da aka jera.")

    if result["verified_negative"]:
        lines.extend([
            "",
            "🚨 VERIFIED NEGATIVE EVIDENCE",
        ])

        for item in result["verified_negative"][:5]:
            lines.append(f"• {item}")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📌 DATA LIMITATION",
        str(
            analysis.get("limitations")
            or "Binciken ya dogara da bayanan DexScreener."
        ),
        "",
        "DexScreener data ba ya tabbatar da social activity, "
        "holders, dev wallets ko contract security.",
        "",
        "⚠️ Kada ka yanke shawarar siye ko sayarwa bisa "
        "wannan report kawai.",
        "",
    ])

    humor = analysis.get("humor")

    if humor:
        lines.extend([
            "😄 RUNNER'S CORNER",
            str(humor),
            "",
        ])

    lines.extend([
        f"🔗 DexScreener: {market['url']}",
        "",
        "JABIR RUNNER | See Beyond The Chart.",
    ])

    return "\n".join(lines)


# =====================================
# TELEGRAM REPORT SENDER
# =====================================

def split_message(text, limit=3900):
    chunks = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line

    if current:
        chunks.append(current)

    return chunks


async def send_report(status, report, context):
    chunks = split_message(report)

    await status.edit_text(
        chunks[0],
        disable_web_page_preview=True
    )

    for chunk in chunks[1:]:
        await context.bot.send_message(
            chat_id=status.chat_id,
            text=chunk,
            disable_web_page_preview=True
        )


# =====================================
# CONTRACT ADDRESS HANDLER
# =====================================

async def handle_ca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    message_text = update.message.text.strip()

    ca_match = re.search(
        r"(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})",
        message_text
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

    # FETCH DEXSCREENER DATA

    try:
        pair = await asyncio.to_thread(
            get_dex_data,
            ca
        )

    except Exception as error:
        print(f"DexScreener Error: {error}")

        await status.edit_text(
            "❌ SCAN FAILED\n\n"
            "An samu matsala wajen haɗuwa da DexScreener.\n"
            "Ka sake gwadawa daga baya."
        )
        return

    if not pair:
        await status.edit_text(
            "❌ TOKEN NOT FOUND\n\n"
            "Ban samu token ɗin a DexScreener ba.\n"
            "Ka tabbatar CA ɗin daidai ne."
        )
        return

    # EXTRACT MARKET DATA

    market = extract_market_data(ca, pair)

    await status.edit_text(
        "🧠 JABIR RUNNER NARRATIVE ENGINE\n\n"
        "✅ DexScreener data received.\n"
        "⏳ Groq AI na nazarin narrative..."
    )

    # GROQ ANALYSIS

    try:
        analysis = await asyncio.to_thread(
            analyze_with_groq,
            market
        )

        report = format_report(
            market,
            analysis
        )

        await send_report(
            status,
            report,
            context
        )

    except Exception as error:
        print(f"Groq Analysis Error: {error}")

        fallback = (
            "🚀 JABIR RUNNER REPORT\n\n"
            f"{market['name']} (${market['ticker']})\n"
            f"Chain: {market['chain']}\n"
            f"DEX: {market['dex']}\n\n"
            f"Price: {format_price(market['price_usd'])}\n"
            f"Liquidity: {format_money(market['liquidity_usd'])} USD\n"
            f"Volume 24h: {format_money(market['volume_24h_usd'])} USD\n"
            f"FDV: {format_money(market['fdv_usd'])}\n\n"
            "⚠️ AI Narrative Analysis bai samu nasara ba.\n"
            "Ka duba Render Logs domin ganin ainihin kuskuren.\n\n"
            f"DexScreener: {market['url']}"
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

    print("JABIR RUNNER starting with Webhook...")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{RENDER_URL.rstrip('/')}/{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
        bootstrap_retries=-1,
    )
