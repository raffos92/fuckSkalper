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
            "ASIN: /watch B0FH795GZ8 Scale Shark\n"
            "Keyword: /watch valor bison beyblade x"
        )

    mkts_json = json.dumps(ALL_MARKETPLACES)
    mkts_str = "/".join(ALL_MARKETPLACES)
    conn = get_db()

    # Se il primo token è un ASIN, il resto è il nome opzionale
    first_token = query.split()[0].upper()
    if ASIN_RE.match(first_token):
        asin = first_token
        name = query[len(first_token):].strip() or asin
        conn.execute(
            """INSERT INTO monitors
               (name, type, keyword, url, marketplaces, sold_by_amazon, search_type,
                enabled, created_at, last_status, poll_interval_seconds)
               VALUES (?, 'asin', ?, '', ?, 1, 'normal', 1, ?, 'watching', NULL)""",
            (name, asin, mkts_json, now_iso()),
        )
        conn.commit()
        conn.close()
        add_log("info", f"Bot: monitor ASIN {asin} ({name}) aggiunto")
        return f"✅ Monitor aggiunto\nNome: {name}\nTipo: ASIN · {asin}\nMercati: {mkts_str}"
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
        return f"✅ Monitor aggiunto\nNome: {query}\nTipo: keyword · \"{query}\"\nMercati: {mkts_str}"


def _cmd_blacklist(msg: dict, name: str = "") -> str:
    reply_to = msg.get("reply_to_message")
    if not reply_to:
        return (
            "❌ Rispondi a una notifica del bot con /blacklist per bloccare quell'ASIN.\n"
            "Puoi anche aggiungere ASIN manualmente dal pannello web → Blacklist."
        )

    entities = reply_to.get("entities", [])
    text = reply_to.get("text", "")

    asin = None
    for ent in entities:
        if ent.get("type") == "text_link":
            url = ent.get("url", "")
            m = re.search(r"/dp/([A-Z0-9]{10})", url)
            if m and ASIN_RE.match(m.group(1)):
                asin = m.group(1)
                break

    if not asin:
        return "❌ Non riesco a trovare l'ASIN nella notifica. Assicurati di rispondere a un messaggio di prodotto del bot."

    title = None
    for line in text.split("\n"):
        if line.startswith("🏷 "):
            title = line[len("🏷 "):]
            break

    conn = get_db()
    final_title = name.strip() or title or asin
    conn.execute(
        "INSERT OR IGNORE INTO blacklist (asin, title, added_at) VALUES (?, ?, ?)",
        (asin, final_title, now_iso()),
    )
    conn.commit()
    conn.close()
    add_log("info", f"Blacklist: ASIN {asin} aggiunto via bot")
    label_line = f"\n📦 {final_title}" if final_title != asin else ""
    return f"🚫 ASIN {asin} bloccato.{label_line}\nNon riceverai più notifiche per questo prodotto."


def _cmd_keeppriority() -> str:
    conn = get_db()
    conn.execute(
        "UPDATE monitors SET priority_last_reminded_at=? WHERE priority=1 AND priority_last_found_at IS NOT NULL",
        (now_iso(),),
    )
    conn.commit()
    conn.close()
    return "✅ Priorità mantenute. Sarai ricontattato al prossimo prodotto trovato."


def _cmd_removepriority(name: str) -> str:
    name = name.strip()
    if not name:
        return "❌ Specifica il nome del monitor: /removepriority Scale Shark"
    conn = get_db()
    result = conn.execute(
        "UPDATE monitors SET priority=0, priority_last_found_at=NULL, priority_last_reminded_at=NULL "
        "WHERE priority=1 AND name LIKE ?",
        (f"%{name}%",),
    )
    affected = result.rowcount
    conn.commit()
    conn.close()
    if affected:
        add_log("info", f"Bot: priorità rimossa da '{name}'")
        return f"✅ Priorità rimossa da '{name}'."
    return f"❌ Nessun monitor prioritario trovato con nome '{name}'."


_HELP = (
    "Comandi disponibili:\n\n"
    "/lista — monitor e pacchetti attivi\n"
    "/stato — worker status + log recenti\n"
    "/help — questo messaggio\n\n"
    "/watch — aggiungi monitor rapido:\n"
    "  ASIN:    /watch B0FH795GZ8 Scale Shark\n"
    "           (nome opzionale dopo l'ASIN)\n"
    "  Keyword: /watch valor bison beyblade x\n"
    "           (il nome coincide con la keyword)\n\n"
    "/blacklist — rispondi a una notifica del bot\n"
    "           per bloccare quell'ASIN\n\n"
    "/keeppriority — mantieni tutti i monitor prioritari\n"
    "/removepriority <nome> — rimuovi la priorità da un monitor"
)


def _dispatch(token: str, chat_id, allowed_chat_id: str, text: str, msg: dict | None = None):
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
    elif cmd == "/blacklist":
        _send(token, chat_id, _cmd_blacklist(msg or {}, arg))
    elif cmd == "/keeppriority":
        _send(token, chat_id, _cmd_keeppriority())
    elif cmd == "/removepriority":
        _send(token, chat_id, _cmd_removepriority(arg))
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
                    _dispatch(token, from_chat, chat_id, text, msg)
        except Exception as e:
            log.exception(f"Errore bot loop: {e}")
            _stop_event.wait(5)


def start_bot_thread():
    t = threading.Thread(target=run_bot_loop, daemon=True)
    t.start()
    return t


def stop_bot():
    _stop_event.set()
