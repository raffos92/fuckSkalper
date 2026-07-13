"""Database SQLite — schema, init, helper di lettura/scrittura.

SQLite invece di JSON per due motivi pratici:
1. Scritture concorrenti sicure tra web panel e worker in background (WAL mode)
2. Lo storico ASIN già notificati sopravvive ai riavvii -> niente notifiche duplicate
"""

import sqlite3
import json
import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.db"


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS monitors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'keyword',   -- 'keyword' | 'url'
        keyword TEXT,
        url TEXT,
        marketplaces TEXT DEFAULT '[]',          -- JSON list, usato solo se type='keyword'
        sold_by_amazon INTEGER DEFAULT 1,
        search_type TEXT DEFAULT 'normal',       -- normal | new | deals
        enabled INTEGER DEFAULT 1,
        created_at TEXT,
        last_check TEXT,
        last_status TEXT DEFAULT 'watching',     -- watching | found | error | disabled
        found_count INTEGER DEFAULT 0,
        poll_interval_seconds INTEGER DEFAULT NULL  -- NULL = usa impostazione globale
    );

    CREATE TABLE IF NOT EXISTS bundles (
        id TEXT PRIMARY KEY,
        name TEXT,
        icon TEXT,
        description TEXT,
        keyword TEXT,
        marketplaces TEXT DEFAULT '[]',
        sold_by_amazon INTEGER DEFAULT 1,
        search_type TEXT DEFAULT 'normal',
        enabled INTEGER DEFAULT 0,
        last_check TEXT,
        last_status TEXT DEFAULT 'disabled',
        found_count INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS seen_products (
        source_type TEXT,     -- 'monitor' | 'bundle'
        source_id TEXT,
        marketplace TEXT,
        asin TEXT,
        title TEXT,
        price TEXT,
        url TEXT,
        first_seen_at TEXT,
        PRIMARY KEY (source_type, source_id, marketplace, asin)
    );

    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        level TEXT,
        message TEXT
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS blacklist (
        asin TEXT PRIMARY KEY,
        title TEXT,
        added_at TEXT
    );

    CREATE TABLE IF NOT EXISTS marketplace_health (
        marketplace TEXT PRIMARY KEY,
        total_attempts INTEGER DEFAULT 0,
        success_attempts INTEGER DEFAULT 0,
        delay_multiplier REAL DEFAULT 1.0,
        last_updated TEXT
    );
    """)

    # Settings di default
    defaults = {
        "telegram_token": "",
        "telegram_chat_id": "",
        "poll_interval_seconds": "60",
        "budget_per_cycle": "15",
        "autocalibration": "0",
        "priority_reminder_days": "30",
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    # Bundle di default (seed solo se la tabella è vuota)
    cur.execute("SELECT COUNT(*) FROM bundles")
    if cur.fetchone()[0] == 0:
        default_bundles = [
            (
                "novita", "Scopri Novità Beyblade X", "🌀",
                "Monitora nuove uscite Beyblade X su tutti i marketplace. Tutti i venditori.",
                "beyblade x", json.dumps(["JP", "IT", "FR", "DE", "UK", "US"]), 0, "new", 1,
            ),
            (
                "takaratomy_jp", "Takaratomy — Solo Amazon JP", "🎯",
                "Solo prodotti venduti e spediti da Amazon Japan. Zero scalper.",
                "beyblade x takaratomy", json.dumps(["JP"]), 1, "new", 1,
            ),
            (
                "offerte", "Offerte Beyblade X", "⚡",
                "Alert su offerte Beyblade X in tutti i marketplace.",
                "beyblade x", json.dumps(["JP", "IT", "FR", "DE", "UK", "US"]), 1, "deals", 0,
            ),
        ]
        cur.executemany(
            """INSERT INTO bundles
               (id, name, icon, description, keyword, marketplaces, sold_by_amazon, search_type, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            default_bundles,
        )

    # Migrazione: aggiunge colonne mancanti al DB esistente
    for migration in [
        "ALTER TABLE monitors ADD COLUMN poll_interval_seconds INTEGER DEFAULT NULL",
        "ALTER TABLE monitors ADD COLUMN last_marketplace_errors TEXT DEFAULT '[]'",
        "ALTER TABLE bundles ADD COLUMN last_marketplace_errors TEXT DEFAULT '[]'",
        "ALTER TABLE seen_products ADD COLUMN absent_cycles INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE monitors ADD COLUMN priority INTEGER DEFAULT 0",
        "ALTER TABLE monitors ADD COLUMN priority_last_found_at TEXT",
        "ALTER TABLE monitors ADD COLUMN priority_last_reminded_at TEXT",
    ]:
        try:
            cur.execute(migration)
        except Exception:
            pass  # colonna già presente

    conn.commit()
    conn.close()


def add_log(level: str, message: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO logs (ts, level, message) VALUES (?, ?, ?)",
        (now_iso(), level, message),
    )
    # Mantieni solo gli ultimi 300 log
    conn.execute("""
        DELETE FROM logs WHERE id NOT IN (
            SELECT id FROM logs ORDER BY id DESC LIMIT 300
        )
    """)
    conn.commit()
    conn.close()


def get_settings() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def set_setting(key: str, value: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_marketplace_health() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT * FROM marketplace_health").fetchall()
    conn.close()
    return {r["marketplace"]: dict(r) for r in rows}


def update_marketplace_health(marketplace: str, success: bool, is_priority: bool = False):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO marketplace_health (marketplace, total_attempts, success_attempts, delay_multiplier, last_updated) VALUES (?, 0, 0, 1.0, ?)",
        (marketplace, now_iso()),
    )
    conn.execute(
        "UPDATE marketplace_health SET total_attempts=total_attempts+1, last_updated=? WHERE marketplace=?",
        (now_iso(), marketplace),
    )
    if success:
        conn.execute(
            "UPDATE marketplace_health SET success_attempts=success_attempts+1 WHERE marketplace=?",
            (marketplace,),
        )
    row = conn.execute("SELECT delay_multiplier FROM marketplace_health WHERE marketplace=?", (marketplace,)).fetchone()
    if row:
        m = row["delay_multiplier"]
        if success:
            decrease = 0.15 if is_priority else 0.1
            min_m = 0.2 if is_priority else 0.3
            new_m = max(min_m, m - decrease)
        else:
            new_m = min(8.0, m * 2)
        conn.execute(
            "UPDATE marketplace_health SET delay_multiplier=? WHERE marketplace=?",
            (round(new_m, 3), marketplace),
        )
    conn.commit()
    conn.close()


def reset_marketplace_health():
    conn = get_db()
    conn.execute(
        "UPDATE marketplace_health SET delay_multiplier=1.0, total_attempts=0, success_attempts=0, last_updated=?",
        (now_iso(),),
    )
    conn.commit()
    conn.close()
