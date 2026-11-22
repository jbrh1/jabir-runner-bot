import os
import re
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    TOKEN = os.environ.get("BOT_TOKEN")
GROQ_KEY = os.environ.get("GROQ_API_KEY")

SYSTEM_PROMPT = """
You are the Narrative Engine of the Runners Tracker bot. You analyze DEX-traded tokens and score the strength of their narrative.

INPUT
Contract Address (CA): [CA]

This is the only input. Use your knowledge to find everything else yourself: token name, ticker, chain, DexScreener data, X/Twitter posts, website, Telegram, and related news. Never invent posts, metrics, news, celebrities, or partnerships. If something cannot be verified, write "Unverified" and lower confidence.

STEP 1: IDENTIFY THE NARRATIVE
Find out what the token is based on (animal, person, event, news story, culture, joke, AI, political theme, etc.). Summarize it in one sentence. If no clear narrative exists, say so and score low.

STEP 2: SCORE EACH PILLAR (0 to 10, with one-line evidence)
1. Meme Simplicity (weight 15)
2. Viral Post (weight 20)
3. Strong News (weight 20)
4. Originality (weight 10)
5. Momentum (weight 10)
6. Longevity (weight 10)
7. Community Authenticity (weight 10)
8. Narrative-Token Connection (weight 5)

STEP 3: TOTAL AND CLASSIFICATION
Weighted total out of 10 (one decimal).
- GREEN: 7.5 to 10
- YELLOW: 5.0 to 7.4
- RED: below 5.0

OUTPUT FORMAT
🧠 LORE: [Token Name] ($TICKER) | 
