
import os
import re
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


# =====================================
# BOT BRANDING
# =====================================

BOT_NAME = "JABIR RUNNER"
BOT_TAGLINE = "See Beyond The Chart."


# =====================================
# GROQ NARRATIVE ENGINE
# =====================================

SYSTEM_PROMPT = """
You are the Narrative Engine of Jabir Runner.

Your job is to analyze DEX-traded tokens and score
narrative strength using only the supplied DexScreener data.

IMPORTANT EVIDENCE RULES:

- Use only the supplied DexScreener data.
- Do not claim to have searched X, Telegram, websites or news.
- Never invent posts, metrics, partnerships, celebrities or news.
- Mark unsupported information as Unverified.
- Lower confidence when evidence is weak.
- Do not confuse trading volume with genuine community interest.
- DexScreener data alone cannot prove community authenticity
  or viral social-media activity.
- If evidence is unavailable, use a conservative score.

STEP 1:
Identify the token narrative in one sentence.
If there is no clear narrative, say so and score low.

STEP 2:
Score these eight pillars from 0 to 10:

1. Meme Simplicity (15%)
2. Viral Post Evidence (20%)
3. Strong News Evidence (20%)
4. Originality (10%)
5. Momentum (10%)
6. Longevity (10%)
7. Community Authenticity (10%)
8. Narrative-Token Connection (5%)

STEP 3:
Calculate the weighted total out of 10 using the weights above.

GREEN: 7.5 - 10
YELLOW: 5.0 - 7.4
RED: below 5.0

OUTPUT FORMAT:

LORE: [Name] ($TICKER) | [Chain]

Narrative: [One sentence]

Catalysts:
- [Verified catalysts or None found]

Scores:
- Meme Simplicity: x/10 | Evidence
- Viral Post Evidence: x/10 | Evidence
- Strong News: x/10 | Evidence
- Originality: x/10 | Evidence
- Momentum: x/10 | Evidence
- Longevity: x/10 | Evidence
- Community Authenticity: x/10 | Evidence
- Narrative-Token Connection: x/10 | Evidence

Total Score: x.x/10 -> GREEN/YELLOW/RED

Narrative Lifespan: Hours/Days/Weeks/Long-term | Reason

Top 3 Risks:
- Risk 1
- Risk 2
- Risk 3

Unverified Items:
- List missing evidence

Confidence: High/Medium/Low

FINAL RULES:

- Answer in Hausa.
- Narrative analysis only, not financial advice.
- Never tell the user to buy or sell.
- Remind the user to check liquidity, holders, dev wallet,
  contract safety and risk management.
- Never describe a token as safe based only on narrative.
- Do not give high scores without credible evidence.
"""


# =====================================
# KEYBOARDS
# =====================================

def main_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "📖 How to Use",
                callback_data="how_to_use"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


def back_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "🏠 Back to Home",
                callback_data="back_home"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


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
        "• Narrative Strength Scoring\n"
        "• Market Data from DexScreener\n"
        "• Risk Awareness\n\n"

        "👇 <b>Yadda za ka fara:</b>\n"
        "Turo min Solana Contract Address (CA).\n\n"

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
        "5. Za ka samu score da bayanin haɗari.\n\n"

        "📊 <b>Narrative Pillars:</b>\n"
        "Meme, Viral, News, Originality, Momentum, "
        "Longevity, Community da Connection.\n\n"

        "⚠️ Wannan Narrative Analysis ne kawai, "
        "ba shawarar siye ko sayarwa ba."
    )


def how_to_use_message():

    return (
        "📖 <b>HOW TO USE JABIR RUNNER</b>\n\n"

        "1. Buɗe DexScreener ka samo CA.\n"
        "2. Turo CA ɗin nan.\n"
        "3. Zan ɗauko bayanan token.\n"
        "4. Groq AI zai tantance narrative.\n"
        "5. Za ka samu score da bayanin haɗari.\n\n"

        "⚠️ Ka tabbatar CA ɗin da ka turo daidai ne."
    )


# =====================================
# TELEGRAM COMMANDS
# =====================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        welcome_message(),
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


async def help_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        help_message(),
        parse_mode="HTML"
    )


# =====================================
# BUTTON HANDLER
# =====================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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
# DEXSCREENER API
# =====================================

def get_dex_data(ca):

    url = (
        "https://api.dexscreener.com/latest/dex/tokens/"
        f"{ca}"
    )

    response = requests.get(
        url,
        timeout=20
    )

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

    # Select pair with highest USD liquidity
    pair = max(
        pairs,
        key=get_liquidity
    )

    return pair


# =====================================
# GROQ AI API
# =====================================

def analyze_with_groq(ca, dex_info):

    if not GROQ_KEY:
        raise ValueError(
            "GROQ_API_KEY is missing in Render."
        )

    url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "openai/gpt-oss-120b",

        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    f"Contract Address: {ca}\n\n"
                    f"DexScreener Data:\n{dex_info}\n\n"
                    "Yi cikakken Narrative Analysis yanzu."
                ),
            },
        ],

        "max_tokens": 1800,
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
        raise RuntimeError(
            "Groq returned no choices."
        )

    content = choices[0].get(
        "message", {}
    ).get("content")

    if not content:
        raise RuntimeError(
            "Groq returned an empty response."
        )

    return content


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
    # FETCH DEXSCREENER DATA
    # =================================

    try:

        pair = await asyncio.to_thread(
            get_dex_data,
            ca
        )

    except Exception as e:

        print(f"DexScreener Error: {e}")

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

    # =================================
    # EXTRACT TOKEN INFORMATION
    # =================================

    base_token = pair.get("baseToken") or {}

    name = base_token.get("name", "Unknown")
    ticker = base_token.get("symbol", "?")
    chain = pair.get("chainId", "?")
    price = pair.get("priceUsd", "?")

    liquidity = (
        pair.get("liquidity") or {}
    ).get("usd", 0)

    volume = (
        pair.get("volume") or {}
    ).get("h24", 0)

    fdv = pair.get("fdv", 0)

    dex_info = (
        f"Name: {name}\n"
        f"Ticker: {ticker}\n"
        f"Chain: {chain}\n"
        f"Price: ${price}\n"
        f"Liquidity: ${liquidity}\n"
        f"Volume 24h: ${volume}\n"
        f"FDV: ${fdv}\n"
        f"DEX: {pair.get('dexId')}\n"
        f"URL: {pair.get('url')}"
    )

    # =================================
    # GROQ ANALYSIS
    # =================================

    await status.edit_text(
        "🧠 JABIR RUNNER NARRATIVE ENGINE\n\n"
        "✅ DexScreener data received.\n"
        "⏳ Groq AI na nazarin narrative..."
    )

    try:

        result = await asyncio.to_thread(
            analyze_with_groq,
            ca,
            dex_info
        )

        report = (
            "🚀 JABIR RUNNER REPORT\n\n"
            f"{result}"
        )

        # Telegram text limit is 4096 characters
        if len(report) > 3900:

            report = (
                report[:3850]
                + "\n\n[Report shortened to fit Telegram.]"
            )

        await status.edit_text(
            report,
            disable_web_page_preview=True
        )

    except Exception as e:

        print(f"Groq Analysis Error: {e}")

        fallback = (
            "🚀 JABIR RUNNER REPORT\n\n"

            f"{name} (${ticker})\n"
            f"Chain: {chain}\n\n"

            f"Price: ${price}\n"
            f"Liquidity: ${liquidity}\n"
            f"Volume 24h: ${volume}\n"
            f"FDV: ${fdv}\n\n"

            "⚠️ AI Narrative Analysis bai samu nasara ba.\n"
            "Ka duba Render Logs domin ganin ainihin kuskuren.\n\n"

            f"DexScreener: {pair.get('url', 'Unavailable')}"
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
        raise ValueError(
            "TELEGRAM_TOKEN is missing!"
        )

    if not RENDER_URL:
        raise ValueError(
            "RENDER_EXTERNAL_URL is missing!"
        )

    if not WEBHOOK_SECRET:
        raise ValueError(
            "WEBHOOK_SECRET is missing!"
        )

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
    
