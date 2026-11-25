# alerts.py - Jabir Runner Alert System
import asyncio, time, sqlite3, os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

DB_PATH = os.getenv("DB_PATH", "jabir_runner.db")
_alert_waiting = {}

def _db():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    return db

def init_alerts_table():
    with _db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, user_id INTEGER, mint TEXT,
                symbol TEXT, alert_type TEXT, target REAL,
                base_price REAL, base_mc REAL, created INTEGER, sent INTEGER DEFAULT 0
            )
        """)

def add_alert(chat_id, user_id, mint, symbol, a_type, target, bp, bmc):
    with _db() as db:
        cur = db.execute(
            "INSERT INTO alerts(chat_id,user_id,mint,symbol,alert_type,target,base_price,base_mc,created) VALUES(?,?,?,?,?,?,?,?,?)",
            (chat_id, user_id, mint, symbol, a_type, target, bp, bmc, int(time.time())),
        )
        return cur.lastrowid

def list_alerts(user_id):
    with _db() as db:
        return db.execute("SELECT id,mint,symbol,alert_type,target,base_price,base_mc FROM alerts WHERE user_id=? AND sent=0", (user_id,)).fetchall()

def delete_alert(aid, user_id):
    with _db() as db:
        db.execute("DELETE FROM alerts WHERE id=? AND user_id=?", (aid, user_id))

def pending_alerts():
    with _db() as db:
        return db.execute("SELECT id,chat_id,mint,symbol,alert_type,target,base_price,base_mc FROM alerts WHERE sent=0").fetchall()

def mark_sent(aid):
    with _db() as db:
        db.execute("UPDATE alerts SET sent=1 WHERE id=?", (aid,))
