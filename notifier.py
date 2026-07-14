"""Invio notifiche Telegram. Pushover si aggiunge qui in futuro con la stessa interfaccia."""

import logging
import requests

log = logging.getLogger("notifier")


def send_telegram(token: str, chat_id: str, message: str) -> bool:
    if not token or not chat_id:
        log.warning("Telegram non configurato (token/chat_id mancanti)")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram API error {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram send error: {e}")
        return False


def format_product_message(source_name: str, marketplace: str, product: dict, seen_at: str = "", is_priority: bool = False) -> str:
    is_deal = product.get("is_deal")
    ts = f" · {seen_at[11:16]}" if seen_at else ""

    if is_deal:
        header = f"⚡ <b>{source_name} · {marketplace} — OFFERTA</b>"
    elif is_priority:
        header = f"🚨🚨 <b>{source_name} · {marketplace}</b>"
    else:
        header = f"🛒 <b>{source_name} · {marketplace}</b>"

    return (
        f"{header}\n\n"
        f"🏷 {product['title']}\n"
        f"💰 {product['price']}\n"
        f"🔗 <a href=\"{product['url']}\">Apri su Amazon</a>{ts}"
    )


# ── Pushover (predisposto, da attivare in futuro) ────────────────────────────
# def send_pushover(user_key: str, app_token: str, message: str, priority: int = 0) -> bool:
#     url = "https://api.pushover.net/1/messages.json"
#     payload = {"token": app_token, "user": user_key, "message": message, "priority": priority}
#     r = requests.post(url, data=payload, timeout=10)
#     return r.status_code == 200
