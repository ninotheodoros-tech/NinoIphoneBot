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

SEARCH_QUERY = "iphone"
SEEN_FILE    = "seen_listings.json"

# ── Profit Estimator ───────────────────────────────────────────────────────────
BATTERY_COST = 40  # лв — average battery replacement cost in Bulgaria

# Typical resale price (лв) for a working iPhone in good condition on OLX.bg
# Format: model_keyword -> (min_resale, max_resale)
RESALE_PRICES = {
    "iphone 16 pro max": (1400, 1700),
    "iphone 16 pro":     (1200, 1500),
    "iphone 16 plus":    (1000, 1200),
    "iphone 16":         (900,  1100),
    "iphone 15 pro max": (1200, 1500),
    "iphone 15 pro":     (1000, 1300),
    "iphone 15 plus":    (850,  1050),
    "iphone 15":         (750,  950),
    "iphone 14 pro max": (950,  1150),
    "iphone 14 pro":     (800,  1000),
    "iphone 14 plus":    (700,  850),
    "iphone 14":         (600,  780),
    "iphone 13 pro max": (750,  900),
    "iphone 13 pro":     (650,  800),
    "iphone 13 mini":    (400,  520),
    "iphone 13":         (480,  620),
    "iphone 12 pro max": (550,  700),
    "iphone 12 pro":     (480,  620),
    "iphone 12 mini":    (320,  430),
    "iphone 12":         (380,  500),
    "iphone 11 pro max": (420,  550),
    "iphone 11 pro":     (360,  480),
    "iphone 11":         (280,  390),
    "iphone xr":         (210,  290),
    "iphone xs max":     (230,  300),
    "iphone xs":         (190,  260),
    "iphone x":          (170,  240),
    "iphone se 3":       (300,  400),
    "iphone se 2":       (200,  280),
    "iphone se":         (160,  230),
    "iphone 8 plus":     (160,  220),
    "iphone 8":          (140,  190),
    "iphone 7 plus":     (120,  170),
    "iphone 7":          (100,  150),
}

def get_model(title: str):
    t = title.lower()
    for model in RESALE_PRICES:
        if model in t:
            return model
    return None

def estimate_profit(title: str, listing_price_text: str):
    model = get_model(title)
    if not model:
        return None
    # Parse the listing price (take the BGN value before лв or /)
    import re
    nums = re.findall(r"[\d]+(?:[.,]\d+)?", listing_price_text.replace(",", "."))
    if not nums:
        return None
    buy_price = float(nums[0])
    min_sell, max_sell = RESALE_PRICES[model]
    min_profit = round(min_sell - buy_price - BATTERY_COST)
    max_profit = round(max_sell - buy_price - BATTERY_COST)
    return {
        "model": model.title(),
        "buy": buy_price,
        "battery_cost": BATTERY_COST,
        "min_sell": min_sell,
        "max_sell": max_sell,
        "min_profit": min_profit,
        "max_profit": max_profit,
    }
# ───────────────────────────────────────────────────────────────────────────────

# ── Filtering ───────────────────────────────────────────────────────────────────
# Title must contain one of these to be considered a real iPhone listing
IPHONE_KEYWORDS = ["iphone", "айфон"]

# Listings containing any of these words are skipped (accessories, not phones)
EXCLUDE_KEYWORDS = [
    "airpods", "air pods", "кейс", "case", "калъф", "кабел", "cable",
    "зарядно", "charger", "слушалки", "стъкло", "протектор", "screen protector",
    "батерия само", "watch", "ipad", "macbook", "части", "spare parts",
    "дисплей само", "корпус само"
]

# If title contains any of these — flag it as a battery-flip opportunity
BATTERY_KEYWORDS = [
    "батерия", "battery", "батер", "зарежда", "не зарежда", "не държи",
    "бърза разредка", "swap", "смяна", "троши", "счупен", "повреден",
    "damage", "broken", "за ремонт", "ремонт", "за части", "spares",
    "не работи", "проблем"
]
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


def is_iphone(title: str) -> bool:
    t = title.lower()
    if not any(k in t for k in IPHONE_KEYWORDS):
        return False
    if any(k in t for k in EXCLUDE_KEYWORDS):
        return False
    return True

def is_battery_flip(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in BATTERY_KEYWORDS)

def format_alert(listing: dict) -> str:
    battery = is_battery_flip(listing["title"])
    header = "🔋 <b>BATTERY FLIP OPPORTUNITY!</b>" if battery else "📱 <b>New iPhone on OLX.bg!</b>"

    profit = estimate_profit(listing["title"], listing["price"])
    if profit:
        if profit["max_profit"] > 0:
            profit_color = "🟢" if profit["min_profit"] > 50 else "🟡"
            profit_section = (
                f"\n\n📊 <b>Profit Estimate</b>\n"
                f"  Buy:      <b>{profit['buy']:.0f} лв</b>\n"
                f"  Battery:  <b>~{profit['battery_cost']} лв</b>\n"
                f"  Resell:   <b>{profit['min_sell']}–{profit['max_sell']} лв</b>\n"
                f"  {profit_color} Profit:  <b>{profit['min_profit']}–{profit['max_profit']} лв</b>"
            )
        else:
            profit_section = f"\n\n🔴 <b>Likely not worth it</b> — resale price too close to buy price."
    else:
        profit_section = ""

    return (
        f"{header}\n\n"
        f"<b>{listing['title']}</b>\n"
        f"💰 <b>{listing['price']}</b>\n"
        f"📍 {listing['location']}"
        f"{profit_section}\n\n"
        f"🔗 <a href='{listing['link']}'>Open listing</a>\n"
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
                card.select_one("h4")
                or card.select_one("h6")
                or card.select_one("h3")
                or card.select_one(".title-cell h3")
            )
            if not title_el:
                img = card.select_one("img[alt]")
                title = img["alt"] if img else "Unknown title"
            else:
                title = title_el.get_text(strip=True)

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
    skipped = 0

    for listing in listings:
        if not is_iphone(listing["title"]):
            skipped += 1
            log.debug(f"  Skipped (not iPhone): {listing['title']}")
            continue
        if listing["id"] not in seen:
            flip = "🔋 BATTERY FLIP" if is_battery_flip(listing["title"]) else "📱 iPhone"
            log.info(f"  {flip}: {listing['title']} — {listing['price']}")
            send_telegram(format_alert(listing))
            seen.add(listing["id"])
            new_count += 1
            time.sleep(1.5)

    save_seen(seen)
    log.info(
        f"Done — {len(listings)} total, {skipped} filtered out, {new_count} new alerts sent."
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
