import os
import re
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    TOKEN = os.environ.get("BOT_TOKEN")
GROQ_KEY = os.environ.get("GROQ_API_KEY")

SYSTEM_PROMPT = (
    "You are the Narrative Engine of the Runners Tracker bot. You analyze DEX-traded tokens and score narrative strength.\n"
    "INPUT: Contract Address (CA) only. Use your knowledge to find token name, ticker, chain, DexScreener data, X posts, website, Telegram, news. Never invent posts, metrics, news, celebrities, or partnerships. If unverified, write Unverified and lower confidence.\n"
    "STEP 1: Identify narrative in one sentence. If no clear narrative, say so and score low.\n"
    "STEP 2: Score each pillar 0-10 with one-line evidence: 1 Meme Simplicity (15), 2 Viral Post (20), 3 Strong News (20), 4 Originality (10), 5 Momentum (10), 6 Longevity (10), 7 Community Authenticity (10), 8 Narrative-Token Connection (5).\n"
    "STEP 3: Weighted total out of 10, one decimal. GREEN 7.5-10, YELLOW 5.0-7.4, RED below 5.0.\n"
    "OUTPUT FORMAT:\n"
    "LORE: [Name] ($TICKER) | [Chain]\n"
    "Narrative: [one sentence]\n"
    "Catalysts: [bullets or None found]\n"
    "Scores: each pillar x/10 | evidence\n"
    "Total Score: x.x/10 -> GREEN/YELLOW/RED\n"
    "Narrative Lifespan: Hours/Days/Weeks/Long-term with reason\n"
    "Top 3 Risks: three bullets\n"
    "Unverified Items: list\n"
    "Confidence: High/Medium/Low\n"
    "RULES: Only verifiable info. Narrative analysis only, not financial advice. Never say buy or sell. Remind user to combine with on-chain checks (liquidity, holders, dev wallet, contract safety) and risk management. Answer in Hausa language."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sannu! Ni ne Runners Tracker Bot.\n\nTura min Contract Address (CA) na token, zan bincika Narrative dinsa na ba ka Score.\n\nRubuta /help don ganin yadda nake aiki.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Yadda nake aiki:\n\n1. Ka tura min CA kadai\n2. Zan dauko DexScreener data\n3. Zan bincika Narrative: Meme, Viral, News, Originality, Momentum, Longevity, Community\n4. Zan ba ka Total Score\n\nNarrative analysis ne kawai, ba shawarar siye/sayarwa ba.")

def get_dex_data(ca):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=15)
        data = r.json()
        pairs = data.get("pairs", [])
        if not pairs:
            return None
        p = sorted(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0), reverse=True)[0]
        return p
    except Exception:
        return None

def analyze_with_groq(ca, dex_info):
    if not GROQ_KEY:
        return None
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"CA: {ca}\n\nDexScreener Data:\n{dex_info}\n\nYi analysis yanzu."}
            ],
            "temperature": 0.3
        }
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Groq Error: {e}")
        return None

async def handle_ca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ca_match = re.search(r'(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})', text)
    if not ca_match:
        await update.message.reply_text("Ban gane CA ba. Ka tura Contract Address kadai.")
        return
    ca = ca_match.group(1)
    await update.message.reply_text(f"Na karba CA: {ca}\n\nIna binciken Narrative... jira kadan")
    pair = get_dex_data(ca)
    if not pair:
        await update.message.reply_text("Ban samu token din a DexScreener ba. Ka tabbatar CA din daidai ne.")
        return
    name = pair.get("baseToken", {}).get("name", "Unknown")
    ticker = pair.get("baseToken", {}).get("symbol", "?")
    chain = pair.get("chainId", "?")
    price = pair.get("priceUsd", "?")
    liq = pair.get("liquidity", {}).get("usd", 0)
    vol = pair.get("volume", {}).get("h24", 0)
    mcap = pair.get("fdv", 0)
    dex_info = f"Name: {name} (${ticker}) | Chain: {chain} | Price: ${price} | Liquidity: ${liq} | Vol 24h: ${vol} | FDV: ${mcap} | Dex: {pair.get('dexId')} | URL: {pair.get('url')}"
    result = analyze_with_groq(ca, dex_info)
    if result:
        await update.message.reply_text(result[:4000])
    else:
        await update.message.reply_text(f"LORE: {name} (${ticker}) | {chain}\n\nPrice: ${price}\nLiquidity: ${liq}\nVol 24h: ${vol}\nFDV: ${mcap}\n\nBan samu GROQ_API_KEY ba. Ka saka shi a Render.\n\n{pair.get('url')}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ca))
    print("Runners Tracker Bot is running...")
    app.run_polling()
