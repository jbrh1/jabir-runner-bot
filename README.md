JABIR RUNNER V3
Solana token scanner using DexScreener, Helius, Groq and SQLite.
Files
main.py: Telegram bot application.
requirements.txt: Python dependencies.
render.yaml: Render service configuration.
Render environment variables
Required:
BOT_TOKEN (or TELEGRAM_TOKEN)
RENDER_EXTERNAL_URL
WEBHOOK_SECRET
Optional:
HELIUS_API_KEY: supply, mint/freeze authority and Deep Scan holder data.
GROQ_API_KEY: two-line Hausa narrative.
GROQ_MODEL: defaults to openai/gpt-oss-120b.
DB_PATH: defaults to jabir_runner.db; Render disk path is /var/data/jabir_runner.db.
Deploy
Upload main.py, requirements.txt, render.yaml and optionally this README to the GitHub repository root.
In Render, create a Blueprint from the repository, or create a Web Service manually.
If deploying manually, use:
Build: pip install -r requirements.txt
Start: python main.py
Add the environment variables in the Render dashboard.
Attach a persistent disk mounted at /var/data if using SQLite history.
Deploy and inspect Render Logs.
Important limitations
Holder concentration is shown only when Helius holder-account pagination completes.
Mint/freeze authority parsing currently supports the standard SPL Token mint layout. Token-2022 extension parsing is not fully implemented.
Bundle %, dev wallet attribution, sniper %, insider %, fake-holder %, and airdrop attribution are not implemented. They remain N/A, not zero.
ATH means the highest price observed since this bot first scanned the token. It is not the token's lifetime ATH.
The bot does not issue an automatic BUY verdict. WATCH is not a safety guarantee.
A CA must match a Solana pair where the mint is the base token.
Aggregated liquidity/volume/transaction counts combine matching DexScreener pools. Pool data can overlap or be incomplete, so treat these as market estimates, not audited totals.
This is a screening tool, not a contract audit or financial advice.
