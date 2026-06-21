"""Bot Telegram: long polling, comandi /lista /stato /watch /help."""

import re
import json
import logging
import threading

import requests

from db import get_db, get_settings, add_log, now_iso

log = logging.getLogger("bot")

_stop_event = threading.Event()

ALL_MARKETPLACES = ["JP", "IT", "DE", "FR", "UK", "US"]
ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")
POLL_TIMEOUT = 30  # secondi — Telegram tiene aperta la connessione fino a questo valore


def _get_updates(token: str, offset: int) -> list[dict]:
    r = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"offset": offset, "timeout": POLL_TIMEOUT},
        timeout=POLL_TIMEOUT + 5,
    )
    if r.status_code == 200:
        return r.json().get("result", [])
    log.warning(f"getUpdates HTTP {r.status_code}")
    return []


def _send(token: str, chat_id, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"sendMessage error: {e}")


def _cmd_lista() -> str:
    conn = get_db()
    monitors = [dict(r) for r in conn.execute(
        "SELECT * FROM monitors WHERE enabled=1 ORDER BY id"
    ).fetchall()]
    bundles = [dict(r) for r in conn.execute(
        "SELECT * FROM bundles WHERE enabled=1 ORDER BY rowid"
    ).fetchall()]
    conn.close()

    lines = [f"📋 Monitor attivi ({len(monitors)}):"]
    for m in monitors:
        mkts = "/".join(json.loads(m["marketplaces"] or "[]"))
        if m["type"] == "asin":
            detail = f"ASIN {m['keyword']}"
        elif m["type"] == "keyword":
            detail = f"\"{m['keyword']}\""
        else:
            detail = "URL"
        lines.append(f"• {m['name']} — {detail} · {mkts}")

    lines.append(f"\n📦 Pacchetti ({len(bundles)}):")
    for b in bundles:
        mkts = "/".join(json.loads(b["marketplaces"] or "[]"))
        lines.append(f"• {b['icon']} {b['name']} · {mkts}")

    return "\n".join(lines)


def _cmd_stato() -> str:
    conn = get_db()
    logs = [dict(r) for r in conn.execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT 8"
    ).fetchall()]
    n_monitors = conn.execute("SELECT COUNT(*) FROM monitors WHERE enabled=1").fetchone()[0]
    n_bundles = conn.execute("SELECT COUNT(*) FROM bundles WHERE enabled=1").fetchone()[0]
    conn.close()

    lines = [
        "🟢 Worker attivo",
        f"📡 {n_monitors} monitor · {n_bundles} pacchetti\n",
        "📋 Log recenti:",
    ]
    icons = {"check": "✓", "found": "🔔", "error": "⚠️", "warning": "⚠️", "info": "ℹ️"}
    for entry in reversed(logs):
        icon = icons.get(entry["level"], "•")
        ts = entry["created_at"][11:19] if entry.get("created_at") else ""
        lines.append(f"{icon} [{ts}] {entry['message']}")

    return "\n".join(lines)


def _cmd_watch(query: str) -> str:
    query = query.strip()
    if not query:
        return (
            "❌ Specifica un ASIN o una keyword.\n"
            "Es: /watch B0FH795GZ8\n"
            "Es: /watch valor bison beyblade x"
        )

    mkts_json = json.dumps(ALL_MARKETPLACES)
    mkts_str = "/".join(ALL_MARKETPLACES)
    conn = get_db()

    if ASIN_RE.match(query.upper()):
        asin = query.upper()
        conn.execute(
            """INSERT INTO monitors
               (name, type, keyword, url, marketplaces, sold_by_amazon, search_type,
                enabled, created_at, last_status, poll_interval_seconds)
               VALUES (?, 'asin', ?, '', ?, 1, 'normal', 1, ?, 'watching', NULL)""",
            (asin, asin, mkts_json, now_iso()),
        )
        conn.commit()
        conn.close()
        add_log("info", f"Bot: monitor ASIN {asin} aggiunto")
        return f"✅ Monitor aggiunto\nTipo: ASIN · {asin}\nMercati: {mkts_str}"
    else:
        conn.execute(
            """INSERT INTO monitors
               (name, type, keyword, url, marketplaces, sold_by_amazon, search_type,
                enabled, created_at, last_status, poll_interval_seconds)
               VALUES (?, 'keyword', ?, '', ?, 1, 'normal', 1, ?, 'watching', NULL)""",
            (query, query, mkts_json, now_iso()),
        )
        conn.commit()
        conn.close()
        add_log("info", f"Bot: monitor keyword '{query}' aggiunto")
        return f"✅ Monitor aggiunto\nTipo: keyword · \"{query}\"\nMercati: {mkts_str}"


_HELP = (
    "Comandi disponibili:\n"
    "/lista — monitor e pacchetti attivi\n"
    "/stato — worker status + log recenti\n"
    "/watch <ASIN o keyword> — aggiungi monitor rapido\n"
    "/help — questo messaggio"
)


def _dispatch(token: str, chat_id, allowed_chat_id: str, text: str):
    if str(chat_id) != str(allowed_chat_id):
        return

    # strip @BotName suffix (comandi inviati in gruppi)
    parts = text.strip().split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/lista":
        _send(token, chat_id, _cmd_lista())
    elif cmd == "/stato":
        _send(token, chat_id, _cmd_stato())
    elif cmd == "/watch":
        _send(token, chat_id, _cmd_watch(arg))
    elif cmd in ("/help", "/start"):
        _send(token, chat_id, _HELP)


def run_bot_loop():
    log.info("Bot Telegram avviato (long polling)")
    offset = 0

    while not _stop_event.is_set():
        settings = get_settings()
        token = settings.get("telegram_token", "")
        chat_id = settings.get("telegram_chat_id", "")

        if not token or not chat_id:
            _stop_event.wait(10)
            continue

        try:
            updates = _get_updates(token, offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = msg.get("text", "")
                from_chat = msg.get("chat", {}).get("id")
                if text and from_chat:
                    _dispatch(token, from_chat, chat_id, text)
        except Exception as e:
            log.exception(f"Errore bot loop: {e}")
            _stop_event.wait(5)


def start_bot_thread():
    t = threading.Thread(target=run_bot_loop, daemon=True)
    t.start()
    return t


def stop_bot():
    _stop_event.set()
