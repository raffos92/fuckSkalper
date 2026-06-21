"""Costruzione URL di ricerca Amazon, fetch pagina, parsing risultati.

Note oneste sui limiti:
- "new" usa il sort ufficiale Amazon per data (s=date-desc-rank) -> affidabile
- "deals" rileva badge sconto nell'HTML -> euristico, Amazon può cambiare il markup
  in qualsiasi momento. Se in uso reale produce troppo rumore o troppo silenzio,
  va rivisto.
"""

import re
import time
import random
import logging
from urllib.parse import quote
import requests
from bs4 import BeautifulSoup

from marketplaces import MARKETPLACES

log = logging.getLogger("scraper")

HEADERS_POOL = [
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Accept-Language": "en-US,en;q=0.9",
    },
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

DEAL_BADGE_SELECTORS = [
    ".s-coupon-highlight-color",
    "[class*='deal-badge']",
    ".a-badge-text",
]
DEAL_KEYWORDS = re.compile(r"(\d+%|offerta|deal|sale|割引|セール|sconto)", re.IGNORECASE)

SPONSORED_TEXTS = frozenset({
    "sponsored", "sponsorizzato", "gesponsert", "sponsorisé", "スポンサー",
})


def build_asin_url(asin: str, marketplace_code: str) -> str:
    mkt = MARKETPLACES[marketplace_code]
    return f"https://www.{mkt['domain']}/dp/{asin}"


def parse_product_page(html: str, base_url: str, asin: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("#productTitle")
    if not title_el:
        return None
    title = title_el.get_text(strip=True)
    if not title:
        return None

    price_el = soup.select_one(".a-price .a-offscreen") or soup.select_one("#priceblock_ourprice")
    price = price_el.get_text(strip=True) if price_el else "—"

    avail_el = soup.select_one("#availability span")
    if avail_el:
        avail_text = avail_el.get_text(strip=True).lower()
        unavailable = ["currently unavailable", "non disponibile", "nicht verfügbar",
                       "actuellement indisponible", "現在在庫切れ", "取り扱いがありません"]
        if any(k in avail_text for k in unavailable):
            log.debug(f"ASIN {asin} non disponibile: {avail_text}")
            return None

    domain = base_url.split("/dp/")[0]
    return {
        "asin": asin,
        "title": title,
        "price": price,
        "url": f"{domain}/dp/{asin}",
        "is_deal": False,
    }


def build_search_url(keyword: str, marketplace_code: str, sold_by_amazon: bool, search_type: str) -> str:
    mkt = MARKETPLACES[marketplace_code]
    url = f"https://www.{mkt['domain']}/s?k={quote(keyword)}"
    if sold_by_amazon:
        url += f"&emi={mkt['seller_id']}"
    if search_type == "new":
        url += "&s=date-desc-rank"
    return url


_CAPTCHA_MARKERS = (
    "validateCaptcha",
    "Robot Check",
    "Type the characters you see",
    "Enter the characters you see",
    "we just need to make sure you're not a robot",
    "Skriv tegnene du ser",  # amazon.se
    "Scrivi i caratteri che vedi",  # amazon.it
    "Saisissez les caractères",  # amazon.fr
    "Geben Sie die Zeichen ein",  # amazon.de
)


def is_captcha_page(html: str) -> bool:
    for marker in _CAPTCHA_MARKERS:
        if marker in html:
            return True
    return False


def fetch_page(url: str, max_retries: int = 2) -> str | None:
    for attempt in range(max_retries + 1):
        headers = {**BASE_HEADERS, **random.choice(HEADERS_POOL)}
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                return r.text
            if r.status_code in (503, 429):
                if attempt < max_retries:
                    wait = 5 * (2 ** attempt)  # 5s poi 10s
                    log.warning(f"Status {r.status_code} su {url[:60]}, retry tra {wait}s (tentativo {attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                log.warning(f"Status {r.status_code} su {url[:60]} dopo {max_retries+1} tentativi")
            else:
                log.warning(f"Status {r.status_code} su {url[:60]}")
            return None
        except requests.exceptions.Timeout:
            if attempt < max_retries:
                log.warning(f"Timeout su {url[:60]}, retry {attempt+1}/{max_retries}")
                time.sleep(3)
                continue
            log.error(f"Timeout definitivo su {url[:60]}")
            return None
        except Exception as e:
            log.error(f"Fetch error su {url[:60]}: {e}")
            return None
    return None


def _has_deal_badge(item) -> bool:
    for sel in DEAL_BADGE_SELECTORS:
        el = item.select_one(sel)
        if el and DEAL_KEYWORDS.search(el.get_text(strip=True)):
            return True
    return False


def _is_sponsored(item) -> bool:
    for el in item.select("span, div"):
        text = el.get_text(strip=True)
        if len(text) <= 20 and text.lower() in SPONSORED_TEXTS:
            return True
    return False


def _title_matches_keyword(title: str, keyword: str) -> bool:
    """Verifica che il titolo contenga almeno una parola significativa della keyword.
    Usa word boundary per evitare che 'valor' matchi 'valore' o 'valoroso'.
    """
    words = [w for w in keyword.lower().split() if len(w) >= 3]
    if not words:
        return True
    title_lower = title.lower()
    return all(re.search(r'\b' + re.escape(w) + r'\b', title_lower) for w in words)


def parse_results(html: str, base_url: str, search_type: str = "normal", keyword: str | None = None) -> list[dict]:
    """Estrae prodotti validi dalla pagina di ricerca Amazon.

    Scarta esplicitamente risultati senza ASIN o senza titolo: meglio un
    falso negativo (prodotto perso per un ciclo) che un falso positivo
    (notifica vuota o sbagliata).
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    domain = base_url.split("/s?")[0]

    items = soup.select('[data-component-type="s-search-result"]')

    for item in items:
        asin = item.get("data-asin", "").strip()
        if not asin:
            continue

        if _is_sponsored(item):
            continue

        title_el = item.select_one("h2 span") or item.select_one(".a-size-medium")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        if keyword and not _title_matches_keyword(title, keyword):
            log.debug(f"Scartato per titolo non pertinente: {title[:60]}")
            continue

        price_el = item.select_one(".a-price .a-offscreen") or item.select_one(".a-price")
        price = price_el.get_text(strip=True) if price_el else "—"

        link_el = item.select_one("h2 a")
        product_url = domain + link_el["href"] if link_el and link_el.get("href") else f"{domain}/dp/{asin}"

        is_deal = _has_deal_badge(item)

        if search_type == "deals" and not is_deal:
            continue

        results.append({
            "asin": asin,
            "title": title,
            "price": price,
            "url": product_url,
            "is_deal": is_deal,
        })

    return results
