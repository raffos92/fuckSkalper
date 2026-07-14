"""Worker che gira in background: ad ogni ciclo controlla monitor e bundle attivi,
confronta con i prodotti già visti (DB) e notifica solo le novità reali.
"""

import json
import time
import random
import logging
import threading
from datetime import datetime

from db import (
    get_db, now_iso, add_log, get_settings,
    get_marketplace_health, update_marketplace_health,
    add_cycle_run, get_cycle_runs,
)
from scraper import build_search_url, build_asin_url, fetch_page, parse_results, parse_product_page, is_captcha_page, parse_price_eur
from notifier import send_telegram, format_product_message

log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_stop_event = threading.Event()


def _price_ok(price_str: str, marketplace: str, max_eur: float) -> bool:
    eur = parse_price_eur(price_str, marketplace)
    return eur is None or eur <= max_eur


def _expand_source(source_type: str, row: dict) -> list[dict]:
    """Espande un monitor/bundle in una lista di (marketplace, url) da controllare."""
    jobs = []
    if source_type == "monitor" and row["type"] == "url":
        jobs.append({"marketplace": "custom", "url": row["url"]})
    elif source_type == "monitor" and row["type"] == "asin":
        marketplaces = json.loads(row["marketplaces"] or "[]")
        for mkt in marketplaces:
            url = build_asin_url(row["keyword"], mkt)
            jobs.append({"marketplace": mkt, "url": url, "asin": row["keyword"]})
    else:
        marketplaces = json.loads(row["marketplaces"] or "[]")
        for mkt in marketplaces:
            url = build_search_url(
                keyword=row["keyword"],
                marketplace_code=mkt,
                sold_by_amazon=bool(row["sold_by_amazon"]),
                search_type=row["search_type"],
            )
            jobs.append({"marketplace": mkt, "url": url})
    return jobs


def _check_source(
    source_type: str,
    source_id: str,
    name: str,
    row: dict,
    settings: dict,
    autocalibration: bool = False,
    is_priority: bool = False,
) -> tuple:
    source_start = time.time()
    jobs = _expand_source(source_type, row)
    search_type = row["search_type"] if source_type == "bundle" or row.get("type") == "keyword" else "normal"
    kw = row.get("keyword") if (source_type == "bundle" or row.get("type") == "keyword") else None

    mkt_health = get_marketplace_health() if autocalibration else {}

    had_error = False
    parsed = []
    fetch_errors = []
    captcha_errors = []
    timeout_errors = []
    asin_unavailable = []
    mkt_timings = []  # [{marketplace, delay_applied, fetch_ms, multiplier, outcome}]

    for i, job in enumerate(jobs):
        delay_applied = 0.0
        multiplier = 1.0
        if i > 0:
            base_delay = random.uniform(2.0, 4.5)
            if autocalibration:
                multiplier = mkt_health.get(job["marketplace"], {}).get("delay_multiplier", 1.0)
            else:
                multiplier = 1.0
            delay_applied = round(base_delay * multiplier, 2)
            time.sleep(delay_applied)

        fetch_start = time.time()
        html = fetch_page(job["url"])
        fetch_ms = int((time.time() - fetch_start) * 1000)
        if html is None:
            had_error = True
            fetch_errors.append(job["marketplace"])
            timeout_errors.append(job["marketplace"])
            mkt_timings.append({"mkt": job["marketplace"], "delay_s": delay_applied, "fetch_ms": fetch_ms, "multiplier": multiplier, "outcome": "timeout"})
            if autocalibration:
                update_marketplace_health(job["marketplace"], success=False, is_priority=is_priority)
            continue
        if is_captcha_page(html):
            log.warning(f"CAPTCHA rilevato su {job['marketplace']} — trattato come fetch fallita")
            had_error = True
            fetch_errors.append(job["marketplace"])
            captcha_errors.append(job["marketplace"])
            mkt_timings.append({"mkt": job["marketplace"], "delay_s": delay_applied, "fetch_ms": fetch_ms, "multiplier": multiplier, "outcome": "captcha"})
            if autocalibration:
                update_marketplace_health(job["marketplace"], success=False, is_priority=is_priority)
            continue

        mkt_timings.append({"mkt": job["marketplace"], "delay_s": delay_applied, "fetch_ms": fetch_ms, "multiplier": multiplier, "outcome": "ok"})
        if autocalibration:
            update_marketplace_health(job["marketplace"], success=True, is_priority=is_priority)

        if "asin" in job:
            p = parse_product_page(html, job["url"], job["asin"])
            products = [p] if p else []
            if not products:
                asin_unavailable.append((job["marketplace"], job["asin"]))
        else:
            products = parse_results(html, job["url"], search_type, keyword=kw)
        parsed.append((job["marketplace"], products))

    # Filtro prezzo (se price_max configurato)
    price_max_eur = row.get("price_max")
    if price_max_eur:
        parsed = [
            (mkt, [p for p in prods if _price_ok(p["price"], mkt, price_max_eur)])
            for mkt, prods in parsed
        ]

    # Fase 2: scrittura DB
    total_new = 0
    new_for_notify = []

    conn = get_db()
    is_asin_monitor = source_type == "monitor" and row.get("type") == "asin"
    absent_threshold = 1 if is_priority else 3

    for marketplace, asin in asin_unavailable:
        conn.execute(
            "UPDATE seen_products SET absent_cycles=absent_cycles+1 WHERE source_type=? AND source_id=? AND marketplace=? AND asin=?",
            (source_type, str(source_id), marketplace, asin),
        )
        conn.execute(
            "DELETE FROM seen_products WHERE source_type=? AND source_id=? AND marketplace=? AND asin=? AND absent_cycles >= ?",
            (source_type, str(source_id), marketplace, asin, absent_threshold),
        )

    if not is_asin_monitor:
        for marketplace, products in parsed:
            if not products:
                continue
            current_asins = tuple(p["asin"] for p in products)
            placeholders = ",".join("?" * len(current_asins))
            conn.execute(
                f"UPDATE seen_products SET absent_cycles=0 WHERE source_type=? AND source_id=? AND marketplace=? AND asin IN ({placeholders})",
                (source_type, str(source_id), marketplace) + current_asins,
            )
            conn.execute(
                f"UPDATE seen_products SET absent_cycles=absent_cycles+1 WHERE source_type=? AND source_id=? AND marketplace=? AND asin NOT IN ({placeholders})",
                (source_type, str(source_id), marketplace) + current_asins,
            )
            conn.execute(
                "DELETE FROM seen_products WHERE source_type=? AND source_id=? AND marketplace=? AND absent_cycles >= ?",
                (source_type, str(source_id), marketplace, absent_threshold),
            )

    for marketplace, products in parsed:
        for p in products:
            already_seen = conn.execute(
                "SELECT 1 FROM seen_products WHERE source_type=? AND source_id=? AND marketplace=? AND asin=?",
                (source_type, str(source_id), marketplace, p["asin"]),
            ).fetchone()
            if already_seen:
                if is_asin_monitor:
                    conn.execute(
                        "UPDATE seen_products SET absent_cycles=0 WHERE source_type=? AND source_id=? AND marketplace=? AND asin=?",
                        (source_type, str(source_id), marketplace, p["asin"]),
                    )
                continue
            blacklisted = conn.execute(
                "SELECT 1 FROM blacklist WHERE asin=?", (p["asin"],)
            ).fetchone()
            if blacklisted:
                continue
            seen_at = now_iso()
            conn.execute(
                """INSERT INTO seen_products
                   (source_type, source_id, marketplace, asin, title, price, url, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_type, str(source_id), marketplace, p["asin"],
                 p["title"], p["price"], p["url"], seen_at),
            )
            new_for_notify.append((marketplace, p, seen_at))
            total_new += 1

    # Aggiorna priority_last_found_at se monitor prioritario ha trovato qualcosa
    if is_priority and total_new > 0 and source_type == "monitor":
        conn.execute(
            "UPDATE monitors SET priority_last_found_at=? WHERE id=?",
            (now_iso(), source_id),
        )

    conn.commit()
    conn.close()

    total_raw_products = sum(len(products) for _, products in parsed)

    # Fase 3: log errori + notifiche
    for mkt in timeout_errors:
        add_log("error", f"{name} [{mkt}] → fetch fallita — timeout/connessione")
    for mkt in captcha_errors:
        add_log("warning", f"{name} [{mkt}] → fetch fallita — CAPTCHA (Amazon blocca)")

    for marketplace, p, seen_at in new_for_notify:
        msg = format_product_message(name, marketplace, p, seen_at, is_priority=is_priority)
        ok = send_telegram(settings.get("telegram_token", ""), settings.get("telegram_chat_id", ""), msg)
        add_log("found", f"{name} [{marketplace}] → {p['title'][:60]}")
        if not ok:
            add_log("error", f"Notifica Telegram fallita per {name}")

    # Fase 4: aggiorna stato sorgente
    successful_fetches = len(jobs) - len(fetch_errors)
    all_failed = bool(fetch_errors) and len(fetch_errors) == len(jobs)
    some_failed = bool(fetch_errors) and not all_failed
    if total_new > 0:
        status = "found"
    elif all_failed:
        status = "error"
    elif some_failed:
        status = "warning"
    elif total_raw_products == 0 and successful_fetches > 0:
        status = "empty"
    else:
        status = "watching"
    table = "monitors" if source_type == "monitor" else "bundles"
    conn = get_db()
    conn.execute(
        f"UPDATE {table} SET last_check=?, last_status=?, found_count=found_count+?, last_marketplace_errors=? WHERE id=?",
        (now_iso(), status, total_new, json.dumps(fetch_errors), source_id),
    )
    conn.commit()
    conn.close()

    result = {
        "name": name,
        "source_type": source_type,
        "monitor_type": row.get("type", "keyword"),
        "priority": is_priority,
        "skipped": False,
        "mkts_ok": [t["mkt"] for t in mkt_timings if t["outcome"] == "ok"],
        "mkts_captcha": captcha_errors[:],
        "mkts_timeout": timeout_errors[:],
        "products_raw": total_raw_products,
        "products_new": total_new,
        "duration_ms": int((time.time() - source_start) * 1000),
        "mkt_timings": mkt_timings,
    }
    return total_new, result


def _is_due(row: dict, global_interval: int) -> bool:
    interval = row.get("poll_interval_seconds") or global_interval
    last_check = row.get("last_check")
    if not last_check:
        return True
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(last_check)).total_seconds()
        return elapsed >= interval
    except Exception:
        return True


def _check_priority_reminders(settings: dict):
    reminder_days = int(settings.get("priority_reminder_days", "30") or 30)
    conn = get_db()
    monitors = [dict(r) for r in conn.execute(
        "SELECT * FROM monitors WHERE enabled=1 AND priority=1 AND priority_last_found_at IS NOT NULL"
    ).fetchall()]
    conn.close()

    to_remind = []
    for m in monitors:
        try:
            elapsed_days = (datetime.now() - datetime.fromisoformat(m["priority_last_found_at"])).days
        except Exception:
            continue
        if elapsed_days < reminder_days:
            continue
        last_reminded = m.get("priority_last_reminded_at")
        if last_reminded:
            try:
                # Già rimandato dopo l'ultimo trovato → skip
                if datetime.fromisoformat(last_reminded) > datetime.fromisoformat(m["priority_last_found_at"]):
                    continue
            except Exception:
                pass
        to_remind.append(m)

    if not to_remind:
        return

    token = settings.get("telegram_token", "")
    chat_id = settings.get("telegram_chat_id", "")
    if not token or not chat_id:
        return

    lines = [f"🔔 <b>Promemoria priorità</b>\n"]
    lines.append(f"Questi monitor hanno trovato prodotti oltre {reminder_days} giorni fa:\n")
    for m in to_remind:
        found_date = m["priority_last_found_at"][:10]
        lines.append(f"• <b>{m['name']}</b> (trovato il {found_date})")
    lines.append("\nVuoi mantenerli come prioritari?")
    lines.append("/keeppriority — mantieni tutti")
    lines.append("/removepriority &lt;nome&gt; — rimuovi uno specifico")

    send_telegram(token, chat_id, "\n".join(lines))

    conn = get_db()
    for m in to_remind:
        conn.execute(
            "UPDATE monitors SET priority_last_reminded_at=? WHERE id=?",
            (now_iso(), m["id"]),
        )
    conn.commit()
    conn.close()
    add_log("info", f"Promemoria priorità inviato per {len(to_remind)} monitor")


def run_cycle(global_interval: int):
    conn = get_db()
    settings = get_settings()
    monitors = [dict(r) for r in conn.execute("SELECT * FROM monitors WHERE enabled=1").fetchall()]
    bundles = [dict(r) for r in conn.execute("SELECT * FROM bundles WHERE enabled=1").fetchall()]
    conn.close()

    budget = int(settings.get("budget_per_cycle", "15") or 15)
    priority_slots = max(1, budget // 3)
    normal_slots = budget - priority_slots
    autocalibration = settings.get("autocalibration", "0") == "1"

    # Monitor prioritari in scadenza
    priority_due = [m for m in monitors if m.get("priority") and _is_due(m, global_interval)]

    # Monitor normali + bundle in scadenza, ordinati per staleness (più vecchi prima)
    normal_monitors_due = [m for m in monitors if not m.get("priority") and _is_due(m, global_interval)]
    bundles_due = [b for b in bundles if _is_due(b, global_interval)]

    def staleness_key(row):
        lc = row.get("last_check")
        return lc if lc else ""  # stringa vuota → prima (mai controllato)

    normal_monitors_due.sort(key=staleness_key)
    bundles_due.sort(key=staleness_key)

    # Costruisci lista normale come (source_type, source_id, name, row)
    normal_due = (
        [("monitor", str(m["id"]), m["name"], m) for m in normal_monitors_due] +
        [("bundle", b["id"], b["name"], b) for b in bundles_due]
    )

    # Budget: slot inutilizzati dai prioritari vanno alla rotazione normale
    priority_to_run = priority_due[:priority_slots]
    extra = priority_slots - len(priority_to_run)
    normal_to_run = normal_due[:normal_slots + extra]

    checked = 0
    found_total = 0
    sources_detail = []
    cycle_start = time.time()

    for i, m in enumerate(priority_to_run):
        if i > 0:
            time.sleep(random.uniform(1.5, 3.0))  # inter-monitor delay (Fix 1)
        checked += 1
        n, result = _check_source(
            "monitor", str(m["id"]), m["name"], m, settings,
            autocalibration=autocalibration, is_priority=True,
        )
        found_total += n
        sources_detail.append(result)

    for i, (src_type, src_id, name, row) in enumerate(normal_to_run):
        time.sleep(random.uniform(1.5, 3.0))  # inter-monitor delay (Fix 1)
        checked += 1
        n, result = _check_source(
            src_type, src_id, name, row, settings,
            autocalibration=autocalibration, is_priority=False,
        )
        found_total += n
        sources_detail.append(result)

    # monitor/bundle saltati per budget esaurito
    for m in priority_due[priority_slots:]:
        sources_detail.append({"name": m["name"], "priority": True, "skipped": True})
    for _, _, name, row in normal_due[normal_slots + extra:]:
        sources_detail.append({"name": name, "priority": bool(row.get("priority")), "skipped": True})

    if checked > 0:
        priority_label = f", {len(priority_to_run)} prioritari" if priority_to_run else ""
        total_skipped = len(priority_due[priority_slots:]) + len(normal_due[normal_slots + extra:])
        skip_label = f", {total_skipped} saltati" if total_skipped else ""
        add_log("check", f"Ciclo completato — {checked}/{budget} sorgenti controllate{priority_label}{skip_label}, {found_total} nuovi prodotti")
        log.info(f"Ciclo completato — {checked}/{budget} sorgenti{priority_label}{skip_label}, {found_total} nuovi prodotti")

        cycle_end = time.time()
        add_cycle_run({
            "started_at": datetime.fromtimestamp(cycle_start).isoformat(timespec="seconds"),
            "ended_at": datetime.fromtimestamp(cycle_end).isoformat(timespec="seconds"),
            "duration_seconds": round(cycle_end - cycle_start, 1),
            "budget": budget,
            "priority_slots": priority_slots,
            "priority_ran": len(priority_to_run),
            "normal_ran": len(normal_to_run),
            "total_checked": checked,
            "total_skipped": total_skipped if checked > 0 else 0,
            "total_found": found_total,
            "sources_detail": json.dumps(sources_detail),
        })

    _check_priority_reminders(settings)


def run_worker_loop():
    add_log("info", "Worker avviato")
    log.info("Worker avviato")
    TICK = 15
    while not _stop_event.is_set():
        try:
            settings = get_settings()
            global_interval = int(settings.get("poll_interval_seconds", "60") or 60)
            run_cycle(global_interval)
        except Exception as e:
            log.exception("Errore nel ciclo di check")
            add_log("error", f"Errore ciclo: {e}")
        _stop_event.wait(TICK)


def start_worker_thread():
    t = threading.Thread(target=run_worker_loop, daemon=True)
    t.start()
    return t


def stop_worker():
    _stop_event.set()
