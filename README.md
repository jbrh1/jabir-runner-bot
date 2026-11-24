JABIR RUNNER V2
Render environment variables
Required:
BOT_TOKEN (or TELEGRAM_TOKEN)
RENDER_EXTERNAL_URL (your deployed Render URL, e.g. https://your-service.onrender.com)
WEBHOOK_SECRET (random secret token, 1-256 chars; use letters/numbers/underscore/hyphen)
Optional:
HELIUS_API_KEY for mint/freeze authority, supply and deep holder scan
GROQ_API_KEY for two-line Hausa narrative
GROQ_MODEL (default openai/gpt-oss-120b)
DB_PATH (recommended /var/data/jabir_runner.db when using a Render persistent disk)
Deploy
Upload main.py and requirements.txt to your GitHub repository.
Create a Render Web Service connected to that repository.
Set Build Command: pip install -r requirements.txt
Set Start Command: python main.py
Add the environment variables above.
Attach a persistent disk and set DB_PATH=/var/data/jabir_runner.db.
Deploy.
Important limitations
Bundle %, dev wallet sold/remaining, sniper %, insider %, fake-holder %, and airdrop attribution are NOT implemented. They are not guessed; report says N/A.
Deep Scan requests Helius holder accounts, but holder concentration is only reported when pagination is complete.
Mint/freeze parsing supports the standard SPL Token mint layout. Token-2022 extension parsing is not fully implemented.
ATH means the highest value observed since this bot's first scan, not the token's lifetime ATH.
The bot intentionally does not issue an automatic BUY verdict. WATCH is not a safety guarantee.
A CA must match a Solana pair where the mint is the base token. It will not silently mislabel a quote-token pair.
