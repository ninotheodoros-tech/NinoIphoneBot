#!/usr/bin/env python3
"""
OLX.bg + Bazar.bg iPhone Bot
Monitors iPhone 11–17. DEAL alert if price ≤ threshold, normal alert otherwise.
All prices in EUR. Sends every listing (no price filtering).
"""

import requests
from bs4 import BeautifulSoup
import json, re, time, os, logging, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Secrets ──────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE      = "seen_listings.json"
CHECK_INTERVAL = 10   # seconds between scrape runs

EUR_TO_BGN = 1.956   # fixed ECB rate

# ── Deal thresholds (EUR) ─────────────────────────────────────────────────────────
# List MUST be longest model name first to avoid partial matches.
# threshold=None means: always notify normally, no "deal" threshold.
DEAL_THRESHOLDS: list[tuple[str, int | None]] = [
    # iPhone 11 family
    ("iphone 11 pro max", 100),
    ("iphone 11 pro",     100),
    ("iphone 11",         100),
    # iPhone 12 family
    ("iphone 12 pro max", 130),
    ("iphone 12 pro",     100),
    ("iphone 12 mini",    100),
    ("iphone 12",         100),
    # iPhone 13 family
    ("iphone 13 pro max", 250),
    ("iphone 13 pro",     200),
    ("iphone 13 mini",    120),
    ("iphone 13",         160),
    # iPhone 14 family
    ("iphone 14 pro max", 300),
    ("iphone 14 pro",     250),
    ("iphone 14 plus",    220),
    ("iphone 14",         200),
    # iPhone 15 family
    ("iphone 15 pro max", 300),
    ("iphone 15 pro",     300),
    ("iphone 15 plus",    300),
    ("iphone 15",         300),
    # iPhone 16 family — notify all, no specific deal threshold
    ("iphone 16 pro max", None),
    ("iphone 16 pro",     None),
    ("iphone 16 plus",    None),
    ("iphone 16",         None),
    # iPhone 17 family
    ("iphone 17 pro max", None),
    ("iphone 17 pro",     None),
    ("iphone 17 plus",    None),
    ("iphone 17",         None),
    # Cyrillic variants
    ("айфон 11",          100),
    ("айфон 12",          100),
    ("айфон 13",          160),
    ("айфон 14",          200),
    ("айфон 15",          300),
    ("айфон 16",          None),
    ("айфон 17",          None),
]

def get_model_threshold(title: str) -> tuple[str, int | None] | None:
    """Return (model_name, threshold_eur) or None if not a tracked iPhone."""
    t = title.lower()
    for model, threshold in DEAL_THRESHOLDS:
        if model in t:
            return model, threshold
    return None

# ── Search price range ────────────────────────────────────────────────────────────
# Wide range — covers iPhone 11 at ~50€ up to iPhone 15 Pro Max at ~600€
_MIN_BGN = 80    # ~41€
_MAX_BGN = 1300  # ~665€

SEARCH_URL = (
    f"https://www.olx.bg/ads/q-iphone/"
    f"?search[filter_float_price:from]={_MIN_BGN}"
    f"&search[filter_float_price:to]={_MAX_BGN}"
    f"&currency=BGN"
    f"&search[order]=created_at:desc"   # newest first → fastest alerts
)
BASE_URL = "https://www.olx.bg"

_BAZAR_EUR_MIN = round(_MIN_BGN / EUR_TO_BGN)
_BAZAR_EUR_MAX = round(_MAX_BGN / EUR_TO_BGN)
BAZAR_URL = (
    f"https://bazar.bg/obiavi"
    f"?q=iphone&price_from={_BAZAR_EUR_MIN}&price_to={_BAZAR_EUR_MAX}&sort=newest"
)
BAZAR_BASE = "https://bazar.bg"

# ── Accessory/junk exclusions ─────────────────────────────────────────────────────
EXCLUDE_KEYWORDS = [
    # Accessories
    "airpods", "air pods", "кейс", "case", "калъф", "кабел", "cable",
    "зарядно", "charger", "слушалки", "earphones", "стъкло", "протектор",
    "screen protector", "tempered glass", "watch", "ipad", "macbook",
    "apple tv", "apple watch", "mac mini",
    # Parts sold alone
    "дисплей за", "екран за", "display for", "screen for",
    "дисплей само", "само дисплей", "корпус само", "само корпус",
    "батерия само", "само батерия", "части за", "за части", "spare parts",
    "резервни части", "ремонт на", "за ремонт", "сервиз",
    # Shop "we buy iPhones" ads
    "изкупуваме", "купувам iphone", "купуваме", "търся iphone",
    "търсим iphone", "изкупувам", "вземам iphone",
    # Parts variants in title (e.g. "iPhone 13-части", "на части")
    "-части", "/части", "на части",
    # Bundles
    "лот телефони", "lot телефони", "няколко телефона",
    # Other junk
    "книга", "book", "аксесоар", "accessory", "accessories",
    "стойка", "holder", "mount", "grip", "pop socket",
    "power bank", "powerbank", "hub", "adapter", "адаптер",
    "sim card", "sim карта", "карта памет", "memory card",
]

def is_valid_listing(title: str) -> bool:
    """True if this is a real iPhone 11-17 listing (not accessory/junk)."""
    t = title.lower()
    # Must contain iPhone keyword
    if not any(k in t for k in ("iphone", "айфон")):
        return False
    # Must not be an accessory or junk ad
    if any(k in t for k in EXCLUDE_KEYWORDS):
        return False
    # Must match a tracked model (11–17)
    if get_model_threshold(t) is None:
        return False
    return True

# ── Price parsing ─────────────────────────────────────────────────────────────────
def parse_eur(price_text: str) -> float | None:
    """Parse any price string to EUR. Converts BGN automatically."""
    if not price_text:
        return None
    clean = price_text.replace("\xa0", " ").replace(",", ".").lower().strip()
    nums = re.findall(r"\d+(?:\.\d+)?", clean)
    if not nums:
        return None
    val = float(nums[0])
    # If labelled as BGN/лв → convert to EUR
    if "лв" in price_text or "bgn" in clean:
        val = round(val / EUR_TO_BGN, 1)
    return val

def fmt_eur(val: float) -> str:
    return f"{val:.0f}€"

# ── GitHub seen-list sync ─────────────────────────────────────────────────────────
_GH_TOKEN = os.environ.get("GITHUB_PAT") or os.environ.get("GITHUB_TOKEN", "")
_GH_REPO  = "ninotheodoros-tech/NinoIphoneBot"
_GH_API   = f"https://api.github.com/repos/{_GH_REPO}/contents/{SEEN_FILE}"
_GH_HEADS = {
    "Authorization": f"token {_GH_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

def _gh_pull_seen() -> dict | None:
    if not _GH_TOKEN:
        return None
    try:
        import base64
        r = requests.get(_GH_API, headers=_GH_HEADS, timeout=10)
        if r.status_code == 404:
            return {}
        if not r.ok:
            return None
        data = json.loads(base64.b64decode(r.json()["content"]).decode())
        if isinstance(data, list):
            return {id_: None for id_ in data}
        return data
    except Exception as e:
        log.warning(f"GitHub pull failed: {e}")
        return None

def _gh_push_seen(seen: dict):
    if not _GH_TOKEN:
        return
    try:
        import base64
        content = base64.b64encode(json.dumps(seen, indent=2).encode()).decode()
        r = requests.get(_GH_API, headers=_GH_HEADS, timeout=10)
        payload: dict = {"message": "chore: update seen listings", "content": content}
        if r.ok:
            payload["sha"] = r.json()["sha"]
        requests.put(_GH_API, headers=_GH_HEADS, json=payload, timeout=15)
    except Exception as e:
        log.warning(f"GitHub push failed: {e}")

def load_seen() -> dict:
    remote = _gh_pull_seen()
    if remote is not None:
        log.info(f"Seen list: {len(remote)} IDs (from GitHub).")
        with open(SEEN_FILE, "w") as f:
            json.dump(remote, f, indent=2)
        return remote
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return {id_: None for id_ in data}
        return data
    return {}

def save_seen(seen: dict):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)
    _gh_push_seen(seen)

# ── Telegram ──────────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    """Send message. Returns True if sent, False if all attempts failed."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.ok:
                return True
            log.error(f"Telegram HTTP {r.status_code}: {r.text[:120]}")
            return False   # HTTP error → no retry
        except Exception as e:
            log.warning(f"Telegram attempt {attempt+1}/3: {e}")
            if attempt < 2:
                time.sleep(3)
    log.error("Telegram: all 3 attempts failed — will retry next cycle.")
    return False

# ── Notification formatters ───────────────────────────────────────────────────────

def _src(listing: dict) -> str:
    src = listing.get("source", "OLX")
    return {"Facebook": "📘 Facebook", "Bazar": "🔵 Bazar.bg"}.get(src, "🟠 OLX.bg")

def format_deal(listing: dict, model: str, price_eur: float, threshold: int) -> str:
    saving = round(threshold - price_eur)
    return (
        f"🔥🔥🔥 <b>ИЗГОДНА ЦЕНА!</b> 🔥🔥🔥\n"
        f"{_src(listing)}\n\n"
        f"📱 <b>{listing['title']}</b>\n"
        f"💰 <b>{fmt_eur(price_eur)}</b>  "
        f"<i>(праг {threshold}€, спестяваш ~{saving}€)</i>\n"
        f"📍 {listing['location']}\n\n"
        f"⚡️ <b>БЪРЗАЙ — много изгодно!</b>\n\n"
        f"🔗 <a href='{listing['link']}'>Отвори обявата</a>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}"
    )

def format_normal(listing: dict, model: str, price_eur: float, threshold: int | None) -> str:
    thresh_line = f"📌 Изгодна цена би била: под {threshold}€\n" if threshold else ""
    return (
        f"📱 <b>Нова обява</b>\n"
        f"{_src(listing)}\n\n"
        f"<b>{listing['title']}</b>\n"
        f"💰 <b>{fmt_eur(price_eur)}</b>\n"
        f"📍 {listing['location']}\n"
        f"{thresh_line}\n"
        f"🔗 <a href='{listing['link']}'>Отвори обявата</a>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}"
    )

def format_price_drop(listing: dict, old_eur: float, new_eur: float,
                      threshold: int | None) -> str:
    drop = round(old_eur - new_eur)
    is_deal = threshold is not None and new_eur <= threshold
    deal_line = f"\n🔥 <b>Вече е изгодна цена!</b> (праг: {threshold}€)" if is_deal else ""
    return (
        f"📉 <b>НАМАЛЕНА ЦЕНА!</b>\n"
        f"{_src(listing)}\n\n"
        f"<b>{listing['title']}</b>\n"
        f"📍 {listing['location']}\n\n"
        f"  Беше: <s>{old_eur:.0f}€</s>\n"
        f"  Сега: <b>{new_eur:.0f}€</b>\n"
        f"  Намалена с: <b>↓{drop}€</b>"
        f"{deal_line}\n\n"
        f"🔗 <a href='{listing['link']}'>Отвори обявата</a>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}"
    )

# ── OLX Scraper ───────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "bg-BG,bg;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def fetch_olx_listings() -> list:
    try:
        r = requests.get(SEARCH_URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.error(f"OLX fetch failed: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    cards = (
        soup.select("[data-testid='l-card']")
        or soup.select("div.offer-wrapper")
        or soup.select("li.offer")
    )
    if not cards:
        log.warning("OLX: No listing cards found — site structure may have changed.")
        return []

    listings = []
    for card in cards:
        try:
            title_el = (
                card.select_one("h4")
                or card.select_one("h6")
                or card.select_one("h3")
            )
            if not title_el:
                img = card.select_one("img[alt]")
                title = img["alt"] if img else ""
            else:
                title = title_el.get_text(strip=True)
            if not title:
                continue

            price_el = (
                card.select_one("[data-testid='ad-price']")
                or card.select_one(".price strong")
                or card.select_one("p.price")
            )
            price_text = price_el.get_text(strip=True) if price_el else ""

            link_el = card.select_one("a[href]")
            href = link_el["href"] if link_el else ""
            link = href if href.startswith("http") else BASE_URL + href

            loc_el = (
                card.select_one("[data-testid='location-date']")
                or card.select_one(".city-name")
            )
            location = loc_el.get_text(strip=True) if loc_el else "—"

            slug = href.split("?")[0].rstrip("/")
            listing_id = "olx_" + (slug.split("-")[-1] if slug else title[:20].replace(" ", "_"))

            listings.append({
                "id": listing_id,
                "title": title,
                "price": price_text,
                "link": link,
                "location": location,
                "source": "OLX",
            })
        except Exception as e:
            log.debug(f"OLX card error: {e}")

    log.info(f"OLX: {len(listings)} listings fetched.")
    return listings

# ── Bazar.bg Scraper ──────────────────────────────────────────────────────────────
def fetch_bazar_listings() -> list:
    try:
        r = requests.get(BAZAR_URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Bazar.bg fetch failed: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select("div.listItemContainer")
    if not cards:
        log.warning("Bazar.bg: No cards found — structure may have changed.")
        return []

    listings = []
    for card in cards:
        try:
            link_el = card.select_one("a.listItemLink")
            if not link_el:
                continue
            title_el = link_el.select_one("span.title")
            title = title_el.get_text(strip=True) if title_el else link_el.get("title", "")
            if not title:
                continue

            href = link_el.get("href", "")
            link = href if href.startswith("http") else BAZAR_BASE + href
            listing_id = "bz_" + link_el.get("data-id", href.split("/")[-1])

            loc_el = link_el.select_one("span.location")
            location = loc_el.get_text(strip=True) if loc_el else "България"

            # Prefer BGN price span, fall back to first price span
            price_text = ""
            price_spans = link_el.select("span.price")
            for span in price_spans:
                currency = span.select_one("span.currency")
                if currency and ("лв" in currency.get_text() or "€" in currency.get_text()):
                    price_text = span.get_text().strip()
                    break
            if not price_text and price_spans:
                price_text = price_spans[0].get_text().strip()
            if not price_text:
                continue

            listings.append({
                "id": listing_id,
                "title": title,
                "price": price_text,
                "link": link,
                "location": location,
                "source": "Bazar",
            })
        except Exception as e:
            log.debug(f"Bazar card error: {e}")

    log.info(f"Bazar.bg: {len(listings)} listings fetched.")
    return listings

# ── Main check ────────────────────────────────────────────────────────────────────
PRICE_DROP_MIN_EUR = 3   # minimum drop to trigger a price-drop alert

def check_and_notify():
    seen = load_seen()
    listings = fetch_olx_listings() + fetch_bazar_listings()

    new_count  = 0
    drop_count = 0
    skipped    = 0

    for listing in listings:
        title = listing["title"]

        if not is_valid_listing(title):
            skipped += 1
            log.debug(f"  Skipped: {title}")
            continue

        result = get_model_threshold(title.lower())
        if result is None:
            skipped += 1
            continue
        model, threshold = result

        price_eur = parse_eur(listing["price"])
        lid = listing["id"]

        if lid in seen:
            # ── Price drop check ───────────────────────────────────────
            stored = seen[lid]
            if (
                stored is not None
                and price_eur is not None
                and price_eur <= stored - PRICE_DROP_MIN_EUR
            ):
                drop = round(stored - price_eur)
                log.info(f"  📉 PRICE DROP: {title} — {stored:.0f}€ → {price_eur:.0f}€ (↓{drop}€)")
                msg = format_price_drop(listing, stored, price_eur, threshold)
                if send_telegram(msg):
                    seen[lid] = price_eur
                    drop_count += 1
                time.sleep(1)
            continue

        # ── New listing ────────────────────────────────────────────────
        if price_eur is None:
            log.warning(f"  ⚠️ Could not parse price for: {title} ({listing['price']})")

        is_deal = (
            threshold is not None
            and price_eur is not None
            and price_eur <= threshold
        )

        if is_deal:
            msg = format_deal(listing, model, price_eur, threshold)
            log.info(f"  🔥 DEAL: {title} — {price_eur}€ (≤{threshold}€)")
        else:
            msg = format_normal(listing, model, price_eur or 0, threshold)
            log.info(f"  📱 Normal: {title} — {price_eur}€")

        sent = send_telegram(msg)
        if sent:
            # Only mark as seen AFTER successful Telegram delivery
            seen[lid] = price_eur
            new_count += 1
            time.sleep(1)
        else:
            # Telegram failed → do NOT mark as seen → will retry next cycle
            log.warning(f"  ❌ Not marked seen — will retry next cycle.")

    save_seen(seen)
    log.info(
        f"Done — {len(listings)} fetched, {skipped} skipped, "
        f"{new_count} new alerts, {drop_count} price drops."
    )

# ── Keepalive HTTP server ──────────────────────────────────────────────────────────
class _KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"iPhone Bot running OK")
    def log_message(self, *args):
        pass

def _start_keepalive_server():
    for port in [8082, 8083, 8084, 8085]:
        try:
            server = HTTPServer(("0.0.0.0", port), _KeepAliveHandler)
            log.info(f"Keepalive server on port {port}")
            server.serve_forever()
            return
        except OSError:
            continue
    log.warning("Keepalive: no free port found — bot still runs normally.")

# ── Entry point ───────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("  iPhone Bot — Starting up")
    log.info(f"  Models    : iPhone 11, 12, 13, 14, 15, 16, 17 (all variants)")
    log.info(f"  Interval  : every {CHECK_INTERVAL}s")
    log.info(f"  OLX URL   : {SEARCH_URL}")
    log.info("=" * 60)

    on_github = os.environ.get("GITHUB_ACTIONS") == "true"
    loop_seconds = int(os.environ.get("LOOP_FOR_SECONDS", "0"))

    if not on_github:
        # Replit: start keepalive server and loop forever
        t = threading.Thread(target=_start_keepalive_server, daemon=True)
        t.start()
        while True:
            log.info(f"Checking at {datetime.now().strftime('%H:%M:%S')} ...")
            try:
                check_and_notify()
            except Exception as e:
                log.error(f"Unexpected error: {e}")
            time.sleep(CHECK_INTERVAL)
    else:
        # GitHub Actions: loop for LOOP_FOR_SECONDS then exit cleanly
        # This way each 5-minute cron run checks every 10s for ~4.5 minutes
        deadline = time.time() + (loop_seconds if loop_seconds > 0 else 0)
        while True:
            log.info(f"Checking at {datetime.now().strftime('%H:%M:%S')} ...")
            try:
                check_and_notify()
            except Exception as e:
                log.error(f"Unexpected error: {e}")
            if loop_seconds == 0:
                break   # run-once mode (no LOOP_FOR_SECONDS set)
            remaining = deadline - time.time()
            if remaining <= CHECK_INTERVAL:
                log.info(f"Loop complete — exiting for next scheduled run.")
                break
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
