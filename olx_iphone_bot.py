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
import re

BATTERY_COST = 40   # лв — average battery replacement cost in Bulgaria
BEST_DEALS_FILE = "best_deals.json"

# Base resale price (лв) for 128GB working iPhone in good condition on OLX.bg
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

# Storage size adjusts resale price
STORAGE_ADJUSTMENTS = {
    "512": +0.20,   # +20%
    "256": +0.10,   # +10%
    "128": +0.00,   # baseline
    "64":  -0.10,   # -10%
    "32":  -0.15,   # -15%
}

# Condition issues that reduce what you can resell for
# Each entry: (keyword, multiplier, warning label)
CONDITION_FLAGS = [
    (["icloud", "активационно", "заключен"], 0.0,  "🔴 iCLOUD LOCKED — cannot be used, avoid!"),
    (["счупен дисплей", "чупен екран", "cracked screen", "пукнат"], 0.70, "⚠️ Cracked screen (-30% resale)"),
    (["счупено стъкло", "пукнато стъкло"],   0.75, "⚠️ Cracked back glass (-25% resale)"),
    (["вода", "water", "окислен"],            0.65, "⚠️ Water damage (-35% resale)"),
    (["face id", "face id не работи"],        0.80, "⚠️ Face ID issue (-20% resale)"),
    (["за части", "spares only"],             0.40, "⚠️ For parts only (-60% resale)"),
]

def get_model(title: str):
    t = title.lower()
    for model in RESALE_PRICES:
        if model in t:
            return model
    return None

def get_storage(title: str):
    t = title.lower()
    for size in ["512", "256", "128", "64", "32"]:
        if f"{size}gb" in t or f"{size} gb" in t:
            return size
    return "128"  # assume 128GB if not mentioned

def get_condition_flags(title: str):
    t = title.lower()
    flags = []
    for keywords, multiplier, label in CONDITION_FLAGS:
        if any(k in t for k in keywords):
            flags.append((multiplier, label))
    return flags

def estimate_profit(title: str, listing_price_text: str):
    model = get_model(title)
    if not model:
        return None
    nums = re.findall(r"\d+(?:[.,]\d+)?", listing_price_text.replace(",", "."))
    if not nums:
        return None
    buy_price = float(nums[0])

    base_min, base_max = RESALE_PRICES[model]

    # Adjust for storage
    storage = get_storage(title)
    storage_adj = STORAGE_ADJUSTMENTS.get(storage, 0.0)
    adj_min = base_min * (1 + storage_adj)
    adj_max = base_max * (1 + storage_adj)

    # Adjust for condition issues
    condition_flags = get_condition_flags(title)
    condition_mult = 1.0
    for mult, _ in condition_flags:
        condition_mult = min(condition_mult, mult)
    adj_min = adj_min * condition_mult
    adj_max = adj_max * condition_mult

    min_profit = round(adj_min - buy_price - BATTERY_COST)
    max_profit = round(adj_max - buy_price - BATTERY_COST)

    return {
        "model":     model.title(),
        "storage":   storage,
        "buy":       buy_price,
        "battery_cost": BATTERY_COST,
        "min_sell":  round(adj_min),
        "max_sell":  round(adj_max),
        "min_profit": min_profit,
        "max_profit": max_profit,
        "warnings":  [label for _, label in condition_flags],
    }

# ── Best Deals Tracker ──────────────────────────────────────────────────────────

def load_best_deals() -> list:
    if os.path.exists(BEST_DEALS_FILE):
        with open(BEST_DEALS_FILE) as f:
            return json.load(f)
    return []

def save_best_deal(listing: dict, profit: dict):
    deals = load_best_deals()
    deals.append({
        "title":      listing["title"],
        "price":      listing["price"],
        "link":       listing["link"],
        "location":   listing["location"],
        "max_profit": profit["max_profit"],
        "min_profit": profit["min_profit"],
        "storage":    profit["storage"],
        "model":      profit["model"],
        "timestamp":  datetime.now().isoformat(),
    })
    # Keep only last 24 hours of deals
    cutoff = datetime.now().timestamp() - 86400
    deals = [d for d in deals if datetime.fromisoformat(d["timestamp"]).timestamp() > cutoff]
    # Keep top 20 by max_profit
    deals = sorted(deals, key=lambda x: x["max_profit"], reverse=True)[:20]
    with open(BEST_DEALS_FILE, "w") as f:
        json.dump(deals, f, indent=2, ensure_ascii=False)

def send_morning_summary():
    deals = load_best_deals()
    if not deals:
        send_telegram("☀️ <b>Good morning!</b>\n\nNo new iPhone deals were found in the last 24 hours. Keep watching!")
        return
    top = deals[:3]
    msg = "☀️ <b>Good morning! Top iPhone Deals (Last 24h)</b>\n\n"
    for i, d in enumerate(top, 1):
        emoji = ["🥇", "🥈", "🥉"][i - 1]
        msg += (
            f"{emoji} <b>{d['model']} {d['storage']}GB</b>\n"
            f"   💰 Buy: {d['price']}\n"
            f"   🟢 Profit: {d['min_profit']}–{d['max_profit']} лв\n"
            f"   🔗 <a href='{d['link']}'>Open listing</a>\n\n"
        )
    msg += f"Total deals found: <b>{len(deals)}</b>"
    send_telegram(msg)
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

# Profit threshold for a MEGA DEAL alert (phone buzzes twice, dramatic format)
MEGA_DEAL_MIN_PROFIT = 150  # лв

def is_mega_deal(profit: dict | None) -> bool:
    return profit is not None and profit["min_profit"] >= MEGA_DEAL_MIN_PROFIT

def source_label(listing: dict) -> str:
    src = listing.get("source", "OLX")
    return "📘 Facebook Marketplace" if src == "Facebook" else "🟠 OLX.bg"

def format_mega_deal(listing: dict, profit: dict) -> str:
    return (
        f"🚨🔴🚨🔴🚨🔴🚨🔴🚨\n"
        f"‼️ <b>MEGA DEAL — BUY NOW!!!</b> ‼️\n"
        f"🚨🔴🚨🔴🚨🔴🚨🔴🚨\n\n"
        f"{source_label(listing)}\n"
        f"🔥 <b>{listing['title']}</b>\n"
        f"💰 <b>{listing['price']}</b>\n"
        f"📍 {listing['location']}\n\n"
        f"💵 Buy:    <b>{profit['buy']:.0f} лв</b>\n"
        f"🔋 Battery: <b>~{profit['battery_cost']} лв</b>\n"
        f"📈 Resell: <b>{profit['min_sell']}–{profit['max_sell']} лв</b>\n\n"
        f"🟢🟢 <b>PROFIT: {profit['min_profit']}–{profit['max_profit']} лв</b> 🟢🟢\n\n"
        f"⚡️ <b>This is a rare one — open it immediately!</b>\n\n"
        f"🔗 <a href='{listing['link']}'>OPEN LISTING NOW</a>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}"
    )

def format_alert(listing: dict, profit: dict | None) -> str:
    battery = is_battery_flip(listing["title"])
    src = source_label(listing)
    header = f"🔋 <b>BATTERY FLIP OPPORTUNITY!</b>\n{src}" if battery else f"📱 <b>New iPhone!</b>\n{src}"

    if profit:
        warnings_text = "\n".join(profit["warnings"])
        warn_section = f"\n{warnings_text}" if warnings_text else ""

        if any("iCLOUD LOCKED" in w for w in profit["warnings"]):
            profit_section = f"\n{warn_section}\n⛔ <b>SKIP THIS — iCloud locked phones cannot be activated!</b>"
        elif profit["max_profit"] <= 0:
            profit_section = f"{warn_section}\n\n🔴 <b>Not worth it</b> — costs more than you can sell for."
        else:
            color = "🟢" if profit["min_profit"] > 50 else "🟡"
            profit_section = (
                f"{warn_section}\n\n"
                f"📊 <b>Profit Estimate ({profit['storage']}GB)</b>\n"
                f"  Buy:      <b>{profit['buy']:.0f} лв</b>\n"
                f"  Battery:  <b>~{profit['battery_cost']} лв</b>\n"
                f"  Resell:   <b>{profit['min_sell']}–{profit['max_sell']} лв</b>\n"
                f"  {color} Profit: <b>{profit['min_profit']}–{profit['max_profit']} лв</b>"
            )
    else:
        profit_section = ""

    return (
        f"{header}\n\n"
        f"<b>{listing['title']}</b>\n"
        f"💰 <b>{listing['price']}</b>\n"
        f"📍 {listing['location']}"
        f"{profit_section}\n\n"
        f"🔗 <a href='{listing['link']}'>Open listing</a>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}"
    )


# ── Facebook Marketplace Scraper ───────────────────────────────────────────────
# Requires FB_C_USER and FB_XS secrets (from a dedicated bot Facebook account).
# Bot works fine without them — Facebook scraping is skipped until cookies are set.

FB_C_USER = os.environ.get("FB_C_USER", "")
FB_XS     = os.environ.get("FB_XS", "")

# Facebook Marketplace search URL for Bulgaria (iPhones in price range)
FB_SEARCH_URL = (
    f"https://www.facebook.com/marketplace/search"
    f"/?query=iphone&minPrice={MIN_PRICE}&maxPrice={MAX_PRICE}"
    f"&daysSinceListed=1&sortBy=creation_time_descend"
)

FB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
}

def fetch_facebook_listings() -> list:
    if not FB_C_USER or not FB_XS:
        log.debug("Facebook cookies not set — skipping Facebook Marketplace.")
        return []

    cookies = {"c_user": FB_C_USER, "xs": FB_XS}
    try:
        r = requests.get(FB_SEARCH_URL, headers=FB_HEADERS, cookies=cookies, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch Facebook Marketplace: {e}")
        return []

    # Facebook embeds all listing data as JSON inside <script> tags.
    # We look for listing objects containing marketplace_listing_title.
    raw = r.text
    listings = []
    seen_ids = set()

    # Extract all JSON blobs from <script type="application/json"> tags
    soup = BeautifulSoup(raw, "html.parser")
    scripts = soup.find_all("script", {"type": "application/json"})

    # Also search for inline JSON patterns in regular script tags
    all_json_sources = [s.string for s in scripts if s.string]
    for tag in soup.find_all("script"):
        if tag.string and "marketplace_listing_title" in (tag.string or ""):
            all_json_sources.append(tag.string)

    # Use regex to pull out individual listing objects
    listing_pattern = re.compile(
        r'"marketplace_listing_title"\s*:\s*"([^"]+)"'
        r'.*?"amount"\s*:\s*"(\d+)"'
        r'.*?"id"\s*:\s*"(\d{10,})"',
        re.DOTALL
    )
    location_pattern = re.compile(r'"city"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"')

    for source in all_json_sources:
        if not source:
            continue
        for m in listing_pattern.finditer(source):
            title    = m.group(1).encode().decode("unicode_escape") if "\\" in m.group(1) else m.group(1)
            price_lv = m.group(2)
            lid      = m.group(3)

            if lid in seen_ids:
                continue
            seen_ids.add(lid)

            # Try to extract city
            loc_m = location_pattern.search(source[max(0, m.start()-2000): m.end()+2000])
            location = loc_m.group(1) if loc_m else "Bulgaria"

            try:
                price_int = int(price_lv)
            except ValueError:
                continue
            if not (MIN_PRICE <= price_int <= MAX_PRICE):
                continue

            listings.append({
                "id":       f"fb_{lid}",
                "title":    title,
                "price":    f"{price_lv} лв",
                "link":     f"https://www.facebook.com/marketplace/item/{lid}/",
                "location": location,
                "source":   "Facebook",
            })

    log.info(f"Facebook Marketplace: {len(listings)} listings in price range.")
    return listings

# ── OLX Scraper ─────────────────────────────────────────────────────────────────

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
    olx_listings = fetch_listings()
    fb_listings  = fetch_facebook_listings()
    listings = olx_listings + fb_listings
    new_count = 0
    skipped = 0

    for listing in listings:
        if not is_iphone(listing["title"]):
            skipped += 1
            log.debug(f"  Skipped (not iPhone): {listing['title']}")
            continue
        if listing["id"] not in seen:
            profit = estimate_profit(listing["title"], listing["price"])
            flip = "🔋 BATTERY FLIP" if is_battery_flip(listing["title"]) else "📱 iPhone"
            profit_str = f" | profit {profit['min_profit']}–{profit['max_profit']} лв" if profit else ""
            log.info(f"  {flip}: {listing['title']} — {listing['price']}{profit_str}")
            if is_mega_deal(profit):
                log.info(f"  🚨 MEGA DEAL detected!")
                # Send twice so phone buzzes twice and it stands out
                send_telegram(format_mega_deal(listing, profit))
                time.sleep(2)
                send_telegram(format_mega_deal(listing, profit))
            else:
                send_telegram(format_alert(listing, profit))
            if profit and profit["max_profit"] > 0:
                save_best_deal(listing, profit)
            seen.add(listing["id"])
            new_count += 1
            time.sleep(1.5)

    save_seen(seen)
    log.info(f"Done — {len(listings)} total, {skipped} filtered out, {new_count} new alerts sent.")


def main():
    log.info("=" * 55)
    log.info("  OLX.bg iPhone Flipper Bot — Starting up")
    log.info(f"  Price range : {MIN_PRICE}–{MAX_PRICE} лв (~$60–$140)")
    log.info(f"  Search URL  : {SEARCH_URL}")
    log.info("=" * 55)

    run_once = os.environ.get("GITHUB_ACTIONS") == "true"
    is_summary = os.environ.get("SEND_MORNING_SUMMARY") == "true"

    if is_summary:
        log.info("Sending morning summary...")
        send_morning_summary()
        return

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
