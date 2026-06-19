"""Worker che gira in background: ad ogni ciclo controlla monitor e bundle attivi,
confronta con i prodotti già visti (DB) e notifica solo le novità reali.
"""

import json
import time
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from db import get_db, now_iso, add_log, get_settings
from scraper import build_search_url, fetch_page, parse_results
from notifier import send_telegram, format_product_message

log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_stop_event = threading.Event()


def _expand_source(source_type: str, row: dict) -> list[dict]:
    """Espande un monitor/bundle in una lista di (marketplace, url) da controllare."""
    jobs = []
    if source_type == "monitor" and row["type"] == "url":
        jobs.append({"marketplace": "custom", "url": row["url"]})
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


def _check_source(source_type: str, source_id: str, name: str, row: dict, settings: dict):
    jobs = _expand_source(source_type, row)
    search_type = row["search_type"] if source_type == "bundle" or row.get("type") == "keyword" else "normal"
    kw = row.get("keyword") if (source_type == "bundle" or row.get("type") == "keyword") else None

    # Fase 1: fetch parallele — marketplace diversi = server diversi, sicuro fare in parallelo
    def fetch_job(job):
        return job, fetch_page(job["url"])

    had_error = False
    parsed = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_job, job): job for job in jobs}
        for future in as_completed(futures):
            job, html = future.result()
            if html is None:
                had_error = True
                continue
            products = parse_results(html, job["url"], search_type, keyword=kw)
            parsed.append((job["marketplace"], products))

    # Fase 2: scrittura DB — connessione breve, nessuna rete aperta
    total_new = 0
    new_for_notify = []

    conn = get_db()
    for marketplace, products in parsed:
        for p in products:
            already_seen = conn.execute(
                "SELECT 1 FROM seen_products WHERE source_type=? AND source_id=? AND marketplace=? AND asin=?",
                (source_type, str(source_id), marketplace, p["asin"]),
            ).fetchone()
            if already_seen:
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
    conn.commit()
    conn.close()

    # Fase 3: notifiche Telegram — nessuna connessione DB aperta
    for marketplace, p, seen_at in new_for_notify:
        msg = format_product_message(name, marketplace, p, seen_at)
        ok = send_telegram(settings.get("telegram_token", ""), settings.get("telegram_chat_id", ""), msg)
        add_log("found", f"{name} [{marketplace}] → {p['title'][:60]}")
        if not ok:
            add_log("error", f"Notifica Telegram fallita per {name}")

    # Fase 4: aggiorna stato sorgente
    status = "error" if had_error and total_new == 0 else ("found" if total_new > 0 else "watching")
    table = "monitors" if source_type == "monitor" else "bundles"
    conn = get_db()
    conn.execute(
        f"UPDATE {table} SET last_check=?, last_status=?, found_count=found_count+? WHERE id=?",
        (now_iso(), status, total_new, source_id),
    )
    conn.commit()
    conn.close()

    return total_new


def _is_due(row: dict, global_interval: int) -> bool:
    """Controlla se un monitor/bundle è in scadenza per il prossimo check."""
    interval = row.get("poll_interval_seconds") or global_interval
    last_check = row.get("last_check")
    if not last_check:
        return True
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(last_check)).total_seconds()
        return elapsed >= interval
    except Exception:
        return True


def run_cycle(global_interval: int):
    conn = get_db()
    settings = get_settings()
    monitors = [dict(r) for r in conn.execute("SELECT * FROM monitors WHERE enabled=1").fetchall()]
    bundles = [dict(r) for r in conn.execute("SELECT * FROM bundles WHERE enabled=1").fetchall()]
    conn.close()

    checked = 0
    found_total = 0

    for m in monitors:
        if not _is_due(m, global_interval):
            continue
        checked += 1
        found_total += _check_source("monitor", str(m["id"]), m["name"], m, settings)

    for b in bundles:
        if not _is_due(b, global_interval):
            continue
        checked += 1
        found_total += _check_source("bundle", b["id"], b["name"], b, settings)

    if checked > 0:
        add_log("check", f"Ciclo completato — {checked} sorgenti controllate, {found_total} nuovi prodotti")
        log.info(f"Ciclo completato — {checked} sorgenti, {found_total} nuovi prodotti")


def run_worker_loop():
    add_log("info", "Worker avviato")
    log.info("Worker avviato")
    # Ciclo base ogni 15s: ogni monitor decide autonomamente se è in scadenza
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
