"""Bot Telegram: long polling, comandi /lista /stato /watch /help."""

import re
import json
import logging
import threading
import time

import requests

from db import get_db, get_settings, add_log, now_iso

log = logging.getLogger("bot")

_stop_event = threading.Event()
_pending: dict[str, dict] = {}  # chat_id → azione pendente (confirm watch/delete)
_PENDING_TIMEOUT = 300  # secondi — la conferma scade dopo 5 minuti

ALL_MARKETPLACES = ["JP", "IT", "DE", "FR", "UK", "US"]
ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")
POLL_TIMEOUT = 30  # secondi — Telegram tiene aperta la connessione fino a questo valore

_KW_STOPWORDS = {"beyblade", "x", "and", "the", "di", "e", "a", "la", "il"}


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


_LISTA_PAGE_SIZE = 16


_WATCH_BUTTONS = [[
    {"text": "✅ Sì", "callback_data": "confirm"},
    {"text": "❌ No", "callback_data": "cancel"},
    {"text": "🔄 Sostituisci", "callback_data": "replace"},
]]
_DELETE_BUTTONS = [[
    {"text": "✅ Sì, elimina", "callback_data": "confirm"},
    {"text": "❌ No", "callback_data": "cancel"},
]]


def _send(token: str, chat_id, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"sendMessage error: {e}")


def _send_buttons(token: str, chat_id, text: str, buttons: list):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": buttons},
            },
            timeout=10,
        )
    except Exception as e:
        log.warning(f"sendMessage (buttons) error: {e}")


def _answer_callback(token: str, callback_id: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": callback_id},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"answerCallbackQuery error: {e}")


def _edit_buttons(token: str, chat_id, message_id: int, buttons: list):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
            json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": buttons}},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"editMessageReplyMarkup error: {e}")


def _build_lista_text(monitors: list, bundles: list, priority_slots: int) -> str:
    n_priority = sum(1 for m in monitors if m["priority"])
    priority_names = [m["name"] for m in monitors if m["priority"]]
    lines = [f"📋 {len(monitors)} monitor attivi\n"]
    if priority_names:
        lines.append(f"★ Preferiti ({n_priority}/{priority_slots} slot):")
        for name in priority_names:
            lines.append(f"  • {name}")
        lines.append("")
    lines.append("📦 Pacchetti:")
    for b in bundles:
        mkts = "/".join(json.loads(b["marketplaces"] or "[]"))
        status = "" if b["enabled"] else " · ⏸ disabilitato"
        lines.append(f"• {b['icon']} {b['name']} · {mkts}{status}")
    return "\n".join(lines)


def _build_lista_buttons(monitors: list, page: int) -> list:
    start = page * _LISTA_PAGE_SIZE
    page_monitors = monitors[start:start + _LISTA_PAGE_SIZE]
    total_pages = (len(monitors) + _LISTA_PAGE_SIZE - 1) // _LISTA_PAGE_SIZE

    buttons = []
    for m in page_monitors:
        mkts = "/".join(json.loads(m["marketplaces"] or "[]"))
        if m["type"] == "asin":
            detail = f"ASIN {m['keyword']}"
        elif m["type"] == "keyword":
            detail = f"\"{m['keyword']}\""
        else:
            detail = "URL"
        star = "★ " if m["priority"] else ""
        info_label = f"{star}{m['name']} — {detail} · {mkts}"
        prio_label = "★ Togli priorità" if m["priority"] else "☆ Dai priorità"
        buttons.append([{"text": info_label[:60], "callback_data": "noop"}])
        buttons.append([
            {"text": "🗑 Elimina", "callback_data": f"del:{m['id']}"},
            {"text": prio_label, "callback_data": f"prio:{m['id']}"},
        ])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append({"text": "← Prec", "callback_data": f"lista:{page - 1}"})
        nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": "noop"})
        if page < total_pages - 1:
            nav.append({"text": "Succ →", "callback_data": f"lista:{page + 1}"})
        buttons.append(nav)

    return buttons


def _cmd_lista(page: int = 0) -> tuple[str, list]:
    conn = get_db()
    monitors = [dict(r) for r in conn.execute(
        "SELECT id, name, type, keyword, marketplaces, priority FROM monitors WHERE enabled=1 ORDER BY priority DESC, id"
    ).fetchall()]
    bundles = [dict(r) for r in conn.execute(
        "SELECT * FROM bundles ORDER BY rowid"
    ).fetchall()]
    conn.close()

    settings = get_settings()
    budget = int(settings.get("budget_per_cycle", "15") or 15)
    priority_slots = max(1, budget // 3)

    text = _build_lista_text(monitors, bundles, priority_slots)
    buttons = _build_lista_buttons(monitors, page)
    return text, buttons


def _cmd_stato() -> tuple[str, list]:
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

    buttons = [[{"text": "🔄 Aggiorna", "callback_data": "stato"}]]
    return "\n".join(lines), buttons


def _kw_words(kw: str) -> set[str]:
    return {w for w in kw.lower().split() if w not in _KW_STOPWORDS and len(w) > 1}


def _find_similar_keyword(new_kw: str, existing: list[dict]) -> dict | None:
    """Ritorna il primo monitor esistente con keyword simile.

    Criteri (in ordine):
    - Match esatto
    - Una contiene l'altra come sottostringa
    - Overlap relativo ≥50%: overlap / min(len_a, len_b) >= 0.5
      (con keyword corte basta 1 parola in comune su 2 per scattare)
    """
    norm_new = new_kw.lower().strip()
    words_new = _kw_words(new_kw)
    for m in existing:
        if m["type"] != "keyword":
            continue
        norm_ex = (m["keyword"] or "").lower().strip()
        if norm_ex == norm_new:
            return m
        if norm_new in norm_ex or norm_ex in norm_new:
            return m
        words_ex = _kw_words(m["keyword"])
        min_len = min(len(words_new), len(words_ex))
        if min_len > 0 and len(words_new & words_ex) / min_len >= 0.5:
            return m
    return None


def _do_add_monitor(name: str, mtype: str, keyword: str, mkts_json: str) -> str:
    mkts_str = "/".join(ALL_MARKETPLACES)
    conn = get_db()
    conn.execute(
        """INSERT INTO monitors
           (name, type, keyword, url, marketplaces, sold_by_amazon, search_type,
            enabled, created_at, last_status, poll_interval_seconds)
           VALUES (?, ?, ?, '', ?, 1, 'normal', 1, ?, 'watching', NULL)""",
        (name, mtype, keyword, mkts_json, now_iso()),
    )
    conn.commit()
    conn.close()
    if mtype == "asin":
        add_log("info", f"Bot: monitor ASIN {keyword} ({name}) aggiunto")
        return f"✅ Monitor aggiunto\nNome: {name}\nTipo: ASIN · {keyword}\nMercati: {mkts_str}"
    else:
        add_log("info", f"Bot: monitor keyword '{keyword}' aggiunto")
        return f"✅ Monitor aggiunto\nNome: {name}\nTipo: keyword · \"{keyword}\"\nMercati: {mkts_str}"


def _cmd_watch(query: str, chat_id: str) -> str:
    query = query.strip()
    if not query:
        return (
            "❌ Specifica un ASIN o una keyword.\n"
            "ASIN: /watch B0FH795GZ8 Scale Shark\n"
            "Keyword: /watch valor bison beyblade x"
        )

    mkts_json = json.dumps(ALL_MARKETPLACES)
    conn = get_db()
    existing = [dict(r) for r in conn.execute(
        "SELECT id, name, type, keyword FROM monitors WHERE enabled=1"
    ).fetchall()]
    conn.close()

    first_token = query.split()[0].upper()
    if ASIN_RE.match(first_token):
        asin = first_token
        name = query[len(first_token):].strip() or asin
        dup = next((m for m in existing if m["type"] == "asin" and m["keyword"].upper() == asin), None)
        if dup:
            _pending[chat_id] = {"action": "watch", "name": name, "mtype": "asin", "keyword": asin, "mkts_json": mkts_json, "dup_id": dup["id"], "ts": time.time()}
            return f"⚠️ ASIN già monitorato come \"{dup['name']}\".", _WATCH_BUTTONS
        return _do_add_monitor(name, "asin", asin, mkts_json), None
    else:
        similar = _find_similar_keyword(query, existing)
        if similar:
            _pending[chat_id] = {"action": "watch", "name": query, "mtype": "keyword", "keyword": query, "mkts_json": mkts_json, "dup_id": similar["id"], "ts": time.time()}
            return (
                f"⚠️ Trovato monitor simile: \"{similar['name']}\"\n"
                f"Keyword esistente: \"{similar['keyword']}\""
            ), _WATCH_BUTTONS
        return _do_add_monitor(query, "keyword", query, mkts_json), None


def _cmd_delete(name: str, chat_id: str) -> str:
    name = name.strip()
    if not name:
        return "❌ Specifica il nome del monitor: /delete Scale Shark"

    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, type, keyword FROM monitors WHERE name LIKE ?",
        (f"%{name}%",),
    ).fetchall()
    conn.close()

    if not rows:
        return f"❌ Nessun monitor trovato con nome \"{name}\".", None

    if len(rows) > 1:
        names = "\n".join(f"• {r['name']}" for r in rows)
        return f"⚠️ Trovati {len(rows)} monitor. Specifica il nome esatto:\n{names}", None

    m = dict(rows[0])
    detail = f"ASIN {m['keyword']}" if m["type"] == "asin" else f"\"{m['keyword']}\""
    _pending[chat_id] = {"action": "delete", "monitor_id": m["id"], "name": m["name"], "ts": time.time()}
    return f"⚠️ Eliminare \"{m['name']}\" ({detail})?", _DELETE_BUTTONS


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


def _cmd_removepriority() -> tuple[str, list | None]:
    conn = get_db()
    monitors = [dict(r) for r in conn.execute(
        "SELECT id, name FROM monitors WHERE enabled=1 AND priority=1 ORDER BY id"
    ).fetchall()]
    conn.close()

    if not monitors:
        return "ℹ️ Nessun monitor prioritario attivo.", None

    buttons = [
        [{"text": f"✖ {m['name'][:30]}", "callback_data": f"rmprio:{m['id']}"}]
        for m in monitors
    ]
    buttons.append([{"text": "❌ Annulla", "callback_data": "cancel_noop"}])
    text = f"★ Monitor prioritari ({len(monitors)}) — tocca per rimuovere la priorità:"
    return text, buttons


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
    "/delete <nome> — elimina un monitor\n\n"
    "/blacklist — rispondi a una notifica del bot\n"
    "           per bloccare quell'ASIN\n\n"
    "/keeppriority — mantieni tutti i monitor prioritari\n"
    "/removepriority <nome> — rimuovi la priorità da un monitor"
)


def _dispatch(token: str, chat_id, allowed_chat_id: str, text: str, msg: dict | None = None):
    if str(chat_id) != str(allowed_chat_id):
        return

    cid_str = str(chat_id)

    # strip @BotName suffix (comandi inviati in gruppi)
    parts = text.strip().split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    def _reply(text_or_tuple):
        if isinstance(text_or_tuple, tuple):
            text, buttons = text_or_tuple
            if buttons:
                _send_buttons(token, chat_id, text, buttons)
            else:
                _send(token, chat_id, text)
        else:
            _send(token, chat_id, text_or_tuple)

    if cmd == "/lista":
        _reply(_cmd_lista())
    elif cmd == "/stato":
        _reply(_cmd_stato())
    elif cmd == "/watch":
        _reply(_cmd_watch(arg, cid_str))
    elif cmd == "/blacklist":
        _send(token, chat_id, _cmd_blacklist(msg or {}, arg))
    elif cmd == "/keeppriority":
        _send(token, chat_id, _cmd_keeppriority())
    elif cmd == "/removepriority":
        _reply(_cmd_removepriority())
    elif cmd == "/delete":
        _reply(_cmd_delete(arg, cid_str))
    elif cmd in ("/help", "/start"):
        _send(token, chat_id, _HELP)


def _handle_callback(token: str, cb: dict, allowed_chat_id: str):
    callback_id = cb["id"]
    from_chat = cb.get("message", {}).get("chat", {}).get("id")
    cid_str = str(from_chat)
    data = cb.get("data", "")

    _answer_callback(token, callback_id)

    if str(from_chat) != str(allowed_chat_id):
        return

    # ── Callback stateless (non richiedono _pending) ──────────────────────────

    if data in ("noop", "cancel_noop"):
        if data == "cancel_noop":
            _send(token, from_chat, "❌ Annullato.")
        return

    if data == "stato":
        text, buttons = _cmd_stato()
        _send_buttons(token, from_chat, text, buttons)
        return

    if data.startswith("lista:"):
        page = int(data.split(":")[1])
        conn = get_db()
        monitors = [dict(r) for r in conn.execute(
            "SELECT id, name, type, keyword, marketplaces, priority FROM monitors WHERE enabled=1 ORDER BY priority DESC, id"
        ).fetchall()]
        conn.close()
        buttons = _build_lista_buttons(monitors, page)
        message_id = cb.get("message", {}).get("message_id")
        if message_id:
            _edit_buttons(token, from_chat, message_id, buttons)
        return

    if data.startswith("rmprio:"):
        monitor_id = int(data.split(":")[1])
        conn = get_db()
        row = conn.execute("SELECT name FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE monitors SET priority=0, priority_last_found_at=NULL, priority_last_reminded_at=NULL WHERE id=?",
                (monitor_id,),
            )
            conn.commit()
            add_log("info", f"Bot: priorità rimossa da '{row['name']}'")
            _send(token, from_chat, f"✅ Priorità rimossa da \"{row['name']}\".")
        conn.close()
        return

    if data.startswith("prio:"):
        monitor_id = int(data.split(":")[1])
        conn = get_db()
        row = conn.execute("SELECT name, priority FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        if row:
            new_prio = 0 if row["priority"] else 1
            conn.execute("UPDATE monitors SET priority=? WHERE id=?", (new_prio, monitor_id))
            conn.commit()
            state = "aggiunta" if new_prio else "rimossa"
            add_log("info", f"Bot: priorità {state} per '{row['name']}'")
            icon = "★" if new_prio else "☆"
            _send(token, from_chat, f"{icon} Priorità {state} per \"{row['name']}\".")
        conn.close()
        return

    if data.startswith("del:"):
        monitor_id = int(data.split(":")[1])
        conn = get_db()
        row = conn.execute("SELECT id, name, type, keyword FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        conn.close()
        if not row:
            _send(token, from_chat, "❌ Monitor non trovato.")
            return
        m = dict(row)
        detail = f"ASIN {m['keyword']}" if m["type"] == "asin" else f"\"{m['keyword']}\""
        _pending[cid_str] = {"action": "delete", "monitor_id": m["id"], "name": m["name"], "ts": time.time()}
        _send_buttons(token, from_chat, f"⚠️ Eliminare \"{m['name']}\" ({detail})?", _DELETE_BUTTONS)
        return

    # ── Callback stateful (richiedono _pending) ────────────────────────────────

    if cid_str not in _pending:
        _send(token, from_chat, "⏱ Sessione scaduta. Ripeti il comando.")
        return

    pending = _pending.pop(cid_str)
    if time.time() - pending.get("ts", 0) > _PENDING_TIMEOUT:
        _send(token, from_chat, "⏱ Conferma scaduta. Ripeti il comando.")
        return

    if data == "cancel":
        _send(token, from_chat, "❌ Operazione annullata.")
        return

    if pending["action"] == "delete":
        conn = get_db()
        conn.execute("DELETE FROM monitors WHERE id=?", (pending["monitor_id"],))
        conn.execute("DELETE FROM seen_products WHERE source_type='monitor' AND source_id=?", (str(pending["monitor_id"]),))
        conn.commit()
        conn.close()
        add_log("info", f"Bot: monitor \"{pending['name']}\" eliminato")
        _send(token, from_chat, f"✅ Monitor \"{pending['name']}\" eliminato.")

    elif pending["action"] == "watch":
        if data == "replace":
            dup_id = pending.get("dup_id")
            if dup_id:
                conn = get_db()
                conn.execute("DELETE FROM monitors WHERE id=?", (dup_id,))
                conn.execute("DELETE FROM seen_products WHERE source_type='monitor' AND source_id=?", (str(dup_id),))
                conn.commit()
                conn.close()
        _send(token, from_chat, _do_add_monitor(
            pending["name"], pending["mtype"], pending["keyword"], pending["mkts_json"]
        ))


def run_bot_loop():
    log.info("Bot Telegram avviato (long polling)")
    offset = 0
    startup_done = False

    while not _stop_event.is_set():
        settings = get_settings()
        token = settings.get("telegram_token", "")
        chat_id = settings.get("telegram_chat_id", "")

        if not token or not chat_id:
            _stop_event.wait(10)
            continue

        if not startup_done:
            startup_done = True
            conn = get_db()
            n_monitors = conn.execute("SELECT COUNT(*) FROM monitors WHERE enabled=1").fetchone()[0]
            n_priority = conn.execute("SELECT COUNT(*) FROM monitors WHERE enabled=1 AND priority=1").fetchone()[0]
            conn.close()
            _send(token, chat_id, f"🟢 Amazon Monitor avviato\n📡 {n_monitors} monitor attivi · {n_priority} prioritari")

        try:
            updates = _get_updates(token, offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    _handle_callback(token, upd["callback_query"], chat_id)
                else:
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
