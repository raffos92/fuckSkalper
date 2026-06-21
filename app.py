"""App Flask: serve il pannello web e espone le API REST per gestire
monitor, bundle e impostazioni. Avvia anche il worker in background.
"""

import os
import json
import logging
from flask import Flask, request, jsonify, send_from_directory

from db import init_db, get_db, now_iso, get_settings, set_setting
from marketplaces import MARKETPLACES
from worker import start_worker_thread
from bot import start_bot_thread

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("app")

app = Flask(__name__, static_folder="static", static_url_path="")


def row_to_monitor(row) -> dict:
    d = dict(row)
    d["marketplaces"] = json.loads(d["marketplaces"] or "[]")
    d["last_marketplace_errors"] = json.loads(d.get("last_marketplace_errors") or "[]")
    d["sold_by_amazon"] = bool(d["sold_by_amazon"])
    d["enabled"] = bool(d["enabled"])
    return d


def row_to_bundle(row) -> dict:
    d = dict(row)
    d["marketplaces"] = json.loads(d["marketplaces"] or "[]")
    d["last_marketplace_errors"] = json.loads(d.get("last_marketplace_errors") or "[]")
    d["sold_by_amazon"] = bool(d["sold_by_amazon"])
    d["enabled"] = bool(d["enabled"])
    return d


# ── Static ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── Marketplaces ──────────────────────────────────────────────────────────────

@app.route("/api/marketplaces")
def api_marketplaces():
    return jsonify(MARKETPLACES)


# ── Monitors ──────────────────────────────────────────────────────────────────

@app.route("/api/monitors", methods=["GET"])
def list_monitors():
    conn = get_db()
    rows = conn.execute("SELECT * FROM monitors ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([row_to_monitor(r) for r in rows])


@app.route("/api/monitors", methods=["POST"])
def create_monitor():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    mtype = data.get("type", "keyword")

    if not name:
        return jsonify({"error": "Il nome è obbligatorio"}), 400
    if mtype == "keyword" and not (data.get("keyword") or "").strip():
        return jsonify({"error": "La parola chiave è obbligatoria"}), 400
    if mtype == "keyword" and not data.get("marketplaces"):
        return jsonify({"error": "Seleziona almeno un marketplace"}), 400
    if mtype == "url" and not (data.get("url") or "").strip():
        return jsonify({"error": "L'URL è obbligatoria"}), 400
    if mtype == "asin" and not (data.get("keyword") or "").strip():
        return jsonify({"error": "L'ASIN è obbligatorio"}), 400
    if mtype == "asin" and not data.get("marketplaces"):
        return jsonify({"error": "Seleziona almeno un marketplace"}), 400

    conn = get_db()
    poll_interval = data.get("poll_interval_seconds")
    poll_interval = int(poll_interval) if poll_interval else None

    cur = conn.execute(
        """INSERT INTO monitors
           (name, type, keyword, url, marketplaces, sold_by_amazon, search_type, enabled, created_at, last_status, poll_interval_seconds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name, mtype,
            data.get("keyword", ""), data.get("url", ""),
            json.dumps(data.get("marketplaces", [])),
            int(bool(data.get("sold_by_amazon", True))),
            data.get("search_type", "normal"),
            int(bool(data.get("enabled", True))),
            now_iso(),
            "watching",
            poll_interval,
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"id": new_id}), 201


@app.route("/api/monitors/<int:monitor_id>", methods=["PUT"])
def update_monitor(monitor_id):
    data = request.get_json(force=True)
    conn = get_db()
    existing = conn.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "Monitor non trovato"}), 404

    fields = dict(existing)
    for key in ["name", "type", "keyword", "url", "search_type"]:
        if key in data:
            fields[key] = data[key]
    if "marketplaces" in data:
        fields["marketplaces"] = json.dumps(data["marketplaces"])
    if "sold_by_amazon" in data:
        fields["sold_by_amazon"] = int(bool(data["sold_by_amazon"]))
    if "enabled" in data:
        fields["enabled"] = int(bool(data["enabled"]))
    if "poll_interval_seconds" in data:
        fields["poll_interval_seconds"] = int(data["poll_interval_seconds"]) if data["poll_interval_seconds"] else None

    conn.execute(
        """UPDATE monitors SET name=?, type=?, keyword=?, url=?, marketplaces=?,
           sold_by_amazon=?, search_type=?, enabled=?, poll_interval_seconds=? WHERE id=?""",
        (fields["name"], fields["type"], fields["keyword"], fields["url"], fields["marketplaces"],
         fields["sold_by_amazon"], fields["search_type"], fields["enabled"],
         fields.get("poll_interval_seconds"), monitor_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/monitors/<int:monitor_id>", methods=["DELETE"])
def delete_monitor(monitor_id):
    conn = get_db()
    conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))
    conn.execute("DELETE FROM seen_products WHERE source_type='monitor' AND source_id=?", (str(monitor_id),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Bundles ───────────────────────────────────────────────────────────────────

@app.route("/api/bundles", methods=["GET"])
def list_bundles():
    conn = get_db()
    rows = conn.execute("SELECT * FROM bundles ORDER BY rowid").fetchall()
    conn.close()
    return jsonify([row_to_bundle(r) for r in rows])


@app.route("/api/bundles/<bundle_id>", methods=["PUT"])
def update_bundle(bundle_id):
    data = request.get_json(force=True)
    conn = get_db()
    existing = conn.execute("SELECT * FROM bundles WHERE id=?", (bundle_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "Bundle non trovato"}), 404
    if "enabled" in data:
        conn.execute("UPDATE bundles SET enabled=? WHERE id=?", (int(bool(data["enabled"])), bundle_id))
    if "marketplaces" in data:
        conn.execute("UPDATE bundles SET marketplaces=? WHERE id=?", (json.dumps(data["marketplaces"]), bundle_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(get_settings())


@app.route("/api/settings", methods=["PUT"])
def api_set_settings():
    data = request.get_json(force=True)
    for key in ["telegram_token", "telegram_chat_id", "poll_interval_seconds"]:
        if key in data:
            set_setting(key, str(data[key]))
    return jsonify({"ok": True})


@app.route("/api/settings/test-telegram", methods=["POST"])
def test_telegram():
    from notifier import send_telegram
    data = request.get_json(force=True, silent=True) or {}
    settings = get_settings()
    token = data.get("telegram_token") or settings.get("telegram_token", "")
    chat_id = data.get("telegram_chat_id") or settings.get("telegram_chat_id", "")
    ok = send_telegram(token, chat_id, "✅ Test riuscito! Amazon Monitor è collegato correttamente.")
    return jsonify({"ok": ok})


# ── Logs & Stats ──────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    limit = int(request.args.get("limit", 50))
    conn = get_db()
    rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    active_monitors = conn.execute("SELECT COUNT(*) c FROM monitors WHERE enabled=1").fetchone()["c"]
    total_monitors = conn.execute("SELECT COUNT(*) c FROM monitors").fetchone()["c"]
    active_bundles = conn.execute("SELECT COUNT(*) c FROM bundles WHERE enabled=1").fetchone()["c"]
    found_total = conn.execute("SELECT COUNT(*) c FROM seen_products").fetchone()["c"]
    settings = get_settings()
    conn.close()
    return jsonify({
        "active_monitors": active_monitors,
        "total_monitors": total_monitors,
        "active_bundles": active_bundles,
        "found_total": found_total,
        "poll_interval_seconds": int(settings.get("poll_interval_seconds", 60)),
    })


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def create_app():
    init_db()
    return app


if __name__ == "__main__":
    init_db()
    # Evita doppio avvio del worker col reloader di Flask in debug mode
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        start_worker_thread()
        start_bot_thread()
        log.info("Worker e bot Telegram avviati")
    app.run(host="0.0.0.0", port=5050, debug=False)
