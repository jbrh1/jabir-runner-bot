
import os
import re
import asyncio
import requests

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==============================
# ENVIRONMENT VARIABLES
# ==============================

TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("BOT_TOKEN")
GROQ_KEY = os.environ.get("GROQ_API_KEY")

RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
WEBHOOK_PATH = "telegram-webhook"

# ==============================
# NARRATIVE ENGINE
# ==============================

SYSTEM_PROMPT = """
You are the Narrative Engine of Runners Tracker Bot.

Your job is to analyze DEX-traded tokens and score narrative strength.

IMPORTANT EVIDENCE RULES:
- Use the supplied DexScreener data.
- Do not claim to have searched X, Telegram, websites, or news.
- You do not have guaranteed live access to those sources.
- Never invent posts, metrics, partnerships, celebrities, or news.
- Mark unsupported information as Unverified.
- Lower confidence when evidence is weak.

STEP 1: Identify the token narrative in one sentence.
If there is no clear narrative, say so and score low.

STEP 2: Score these eight pillars from 0 to 10.
1. Meme Simplicity (15%)
2. Viral Post Evidence (20%)
3. Strong News Evidence (20%)
4. Originality (10%)
5. Momentum (10%)
6. Longevity (10%)
7. Community Authenticity (10%)
8. Narrative-Token Connection (5%)

For unavailable evidence, use a conservative score.
Do not confuse trading volume with genuine community interest.

STEP 3: Calculate weighted total out of 10.
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
  contract safety, and risk management.
- Do not give high scores without credible evidence.
"""

# ==============================
# TELEGRAM COMMANDS
# ==============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Sannu! Ni ne Runners Tracker Bot. 🤖\n\n"
        "Tura min Contract Address (CA) na token.\n"
        "Zan binciki Narrative dinsa in ba ka Score.\n\n"
        "Rubuta /help don ƙarin bayani."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Yadda nake aiki:\n\n"
        "1. Ka tura min CA.\n"
        "2. Zan ɗauko bayanai daga DexScreener.\n"
        "3. Zan tantance Narrative.\n"
        "4. Zan ba ka Score daga 0 zuwa 10.\n\n"
        "Pillars: Meme, Viral, News, Originality, "
        "Momentum, Longevity, Community da Connection.\n\n"
        "Wannan Narrative analysis ne kawai, "
        "ba shawarar siye ko sayarwa ba."
    )


# ==============================
# DEXSCREENER API
# ==============================

def get_dex_data(ca):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"

    response = requests.get(url, timeout=20)
    response.raise_for_status()

    data = response.json()
    pairs = data.get("pairs") or []

    if not pairs:
        return None

    # Select pair with highest USD liquidity
    pair = sorted(
        pairs,
        key=lambda x: float(
            (x.get("liquidity") or {}).get("usd") or 0
        ),
        reverse=True
    )[0]

    return pair


# ==============================
# GROQ AI API
# ==============================

def analyze_with_groq(ca, dex_info):

    if not GROQ_KEY:
        raise ValueError("GROQ_API_KEY is missing in Render.")

    url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "llama-3.3-70b-versatile",
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
                    "Yi cikakken Narrative analysis yanzu."
                ),
            },
        ],
        "temperature": 0.3,
        "max_tokens": 1800,
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=60,
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

    return content


# ==============================
# CONTRACT ADDRESS HANDLER
# ==============================

async def handle_ca(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text.strip()

    ca_match = re.search(
        r"(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})",
        text
    )

    if not ca_match:
        await update.message.reply_text(
            "Ban gane CA ba. Ka tura Contract Address kawai."
        )
        return

    ca = ca_match.group(1)

    await update.message.reply_text(
        f"Na karɓa CA: {ca}\n\n"
        "Ina binciken Narrative... jira kaɗan 🔍"
    )

    # Fetch DexScreener data without blocking Telegram's event loop
    try:
        pair = await asyncio.to_thread(get_dex_data, ca)

    except Exception as e:
        print(f"DexScreener Error: {e}")

        await update.message.reply_text(
            "An samu matsala wajen haɗuwa da DexScreener. "
            "Ka sake gwadawa daga baya."
        )
        return

    if not pair:
        await update.message.reply_text(
            "Ban samu token ɗin a DexScreener ba. "
            "Ka tabbatar CA ɗin daidai ne."
        )
        return

    # Extract token information
    name = pair.get("baseToken", {}).get("name", "Unknown")
    ticker = pair.get("baseToken", {}).get("symbol", "?")
    chain = pair.get("chainId", "?")
    price = pair.get("priceUsd", "?")

    liquidity = (pair.get("liquidity") or {}).get("usd", 0)
    volume = (pair.get("volume") or {}).get("h24", 0)
    fdv = pair.get("fdv", 0)

    dex_info = (
        f"Name: {name} (${ticker})\n"
        f"Chain: {chain}\n"
        f"Price: ${price}\n"
        f"Liquidity: ${liquidity}\n"
        f"Volume 24h: ${volume}\n"
        f"FDV: ${fdv}\n"
        f"DEX: {pair.get('dexId')}\n"
        f"URL: {pair.get('url')}"
    )

    # Run Groq analysis
    try:
        result = await asyncio.to_thread(
            analyze_with_groq,
            ca,
            dex_info
        )

        await update.message.reply_text(result[:4000])

    except Exception as e:
        print(f"Groq Analysis Error: {e}")

        await update.message.reply_text(
            f"LORE: {name} (${ticker}) | {chain}\n\n"
            f"Price: ${price}\n"
            f"Liquidity: ${liquidity}\n"
            f"Vol 24h: ${volume}\n"
            f"FDV: ${fdv}\n\n"
            "AI Narrative Analysis bai samu nasara ba.\n"
            "Duba Render Logs domin ganin ainihin kuskuren.\n\n"
            f"{pair.get('url')}"
        )


# ==============================
# START BOT
# ==============================

if __name__ == "__main__":

    if not TOKEN:
        raise ValueError("TELEGRAM_TOKEN is missing!")

    if not RENDER_URL:
        raise ValueError("RENDER_EXTERNAL_URL is missing!")

    if not WEBHOOK_SECRET:
        raise ValueError("WEBHOOK_SECRET is missing!")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_ca
        )
    )

    print("Runners Tracker Bot starting with Webhook...")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{RENDER_URL.rstrip('/')}/{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
        bootstrap_retries=-1,
    )
