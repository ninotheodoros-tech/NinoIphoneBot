#!/usr/bin/env python3
"""
OLX.bg iPhone Flipper Bot
Monitors olx.bg for iPhone listings in your price range and sends
Telegram alerts instantly when a new one appears.
"""

import requests
from bs4 import BeautifulSoup
import json
import time
import os
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── YOUR SETTINGS ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# Price range in BGN (Bulgarian Lev)
# ~$60  = 108 лв  |  ~$140 = 252 лв
MIN_PRICE = 108
MAX_PRICE = 252

SEARCH_QUERY    = "iphone"
CHECK_EVERY_SEC = 300        # How often to check (seconds). 300 = every 5 min.
SEEN_FILE       = "seen_listings.json"
# ───────────────────────────────────────────────────────────────────────────────

BASE_URL   = "https://www.olx.bg"
SEARCH_URL = (
    f"{BASE_URL}/ads/q-{SEARCH_QUERY}/"
    f"?search[filter_float_price:from]={MIN_PRICE}"
    f"&search[filter_float_price:to]={MAX_PRICE}"
    f"&currency=BGN"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "bg-BG,bg;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── Persistence ────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f, indent=2)


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            log.error(f"Telegram error {r.status_code}: {r.text}")
        r.raise_for_status()
    except requests.HTTPError:
        pass  # already logged above
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def format_alert(listing: dict) -> str:
    return (
        f"📱 <b>New iPhone Deal on OLX.bg!</b>\n\n"
        f"<b>{listing['title']}</b>\n"
        f"💰 <b>{listing['price']}</b>\n"
        f"📍 {listing['location']}\n"
        f"🔗 <a href='{listing['link']}'>Open listing</a>\n\n"
        f"🕐 Found at {datetime.now().strftime('%H:%M:%S')}"
    )


# ── Scraper ────────────────────────────────────────────────────────────────────

def fetch_listings() -> list:
    try:
        r = requests.get(SEARCH_URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch OLX page: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    listings = []

    # Primary selector (OLX new layout)
    cards = soup.select("[data-testid='l-card']")

    # Fallback selectors for older OLX layouts
    if not cards:
        cards = soup.select("div.offer-wrapper") or soup.select("li.offer")

    if not cards:
        log.warning("No listing cards found — OLX may have changed its HTML structure.")
        return []

    for card in cards:
        try:
            # Title
            title_el = (
                card.select_one("h6")
                or card.select_one("h3")
                or card.select_one(".title-cell h3")
            )
            title = title_el.get_text(strip=True) if title_el else "Unknown title"

            # Price
            price_el = (
                card.select_one("[data-testid='ad-price']")
                or card.select_one(".price strong")
                or card.select_one("p.price")
            )
            price_text = price_el.get_text(strip=True) if price_el else "Price unknown"

            # Link
            link_el = card.select_one("a[href]")
            href = link_el["href"] if link_el else ""
            link = href if href.startswith("http") else BASE_URL + href

            # Location / date
            loc_el = (
                card.select_one("[data-testid='location-date']")
                or card.select_one(".city-name")
                or card.select_one("p.city-name")
            )
            location = loc_el.get_text(strip=True) if loc_el else "Location unknown"

            # Unique ID from the URL slug (last segment before query string)
            slug = href.split("?")[0].rstrip("/")
            listing_id = slug.split("-")[-1] if slug else (title + price_text)

            listings.append({
                "id": listing_id,
                "title": title,
                "price": price_text,
                "link": link,
                "location": location,
            })

        except Exception as e:
            log.debug(f"Skipped a card due to parse error: {e}")
            continue

    return listings


# ── Main loop ──────────────────────────────────────────────────────────────────

def check_and_notify():
    seen = load_seen()
    listings = fetch_listings()
    new_count = 0

    for listing in listings:
        if listing["id"] not in seen:
            log.info(f"  🆕 {listing['title']} — {listing['price']}")
            send_telegram(format_alert(listing))
            seen.add(listing["id"])
            new_count += 1
            time.sleep(1.5)  # small delay so Telegram doesn't rate-limit us

    save_seen(seen)
    log.info(
        f"Check complete — {len(listings)} total listings, {new_count} new alerts sent."
    )


def main():
    log.info("=" * 55)
    log.info("  OLX.bg iPhone Flipper Bot — Starting up")
    log.info(f"  Price range : {MIN_PRICE}–{MAX_PRICE} лв (~$60–$140)")
    log.info(f"  Search URL  : {SEARCH_URL}")
    log.info("=" * 55)

    # When run by GitHub Actions, CHECK_EVERY_SEC is 0 — run once and exit
    # When run locally on Replit, loop every 60 seconds
    run_once = os.environ.get("GITHUB_ACTIONS") == "true"

    while True:
        log.info(f"Checking OLX.bg at {datetime.now().strftime('%H:%M:%S')} ...")
        try:
            check_and_notify()
        except Exception as e:
            log.error(f"Unexpected error during check: {e}")
        if run_once:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
