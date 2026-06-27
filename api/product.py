from http.server import BaseHTTPRequestHandler
import json
import re
import time
import random
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

def get_headers():
    ua = random.choice(USER_AGENTS)
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

def is_blocked(html):
    if not html or len(html) < 500:
        return True
    low = html.lower()
    return any(x in low for x in [
        "captcha", "robot check", "validatecaptcha",
        "enter the characters", "not a robot",
        "api-services-support@amazon.com",
        "automated access",
    ])

def is_dead_page(html):
    if not html:
        return False
    low = html.lower()
    return any(x in low for x in [
        "looking for something",
        "not a functioning page",
        "page on our site",
        "dogs of amazon",
        "we couldn't find that page",
        "the web address you entered is not",
    ])

def clean_price(text):
    if not text:
        return None
    try:
        cleaned = re.sub(r"[^\d.]", "", str(text).replace(",", ""))
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = parts[0]
        val = float(cleaned)
        return val if 1 <= val <= 500000 else None
    except Exception:
        return None

def fetch_with_retry(url, max_attempts=6):
    session = requests.Session()
    for attempt in range(max_attempts):
        try:
            if attempt > 0:
                time.sleep(random.uniform(0.5, 1.5) * attempt)

            session.headers.update(get_headers())
            resp = session.get(url, timeout=12, allow_redirects=True)

            if resp.status_code == 404:
                return None, "Product not found. Check the ASIN."
            if resp.status_code in (503, 429, 403):
                continue
            if resp.status_code != 200:
                continue

            html = resp.text

            if is_dead_page(html):
                return None, "DEAD_PAGE"

            if is_blocked(html):
                continue

            return html, None

        except requests.exceptions.Timeout:
            continue
        except requests.exceptions.ConnectionError:
            continue
        except Exception as e:
            if attempt == max_attempts - 1:
                return None, f"Request failed: {str(e)}"
            continue

    return None, "Amazon blocked all attempts. Try again in a moment."


# ── Availability constants ────────────────────────────────────────────────────
AVAIL_IN_STOCK          = "In Stock"
AVAIL_LOW_STOCK         = "Low Stock"
AVAIL_TEMP_OOS          = "Temporarily Out of Stock"
AVAIL_CURRENTLY_UNAVAIL = "Currently Unavailable"
AVAIL_OUT_OF_STOCK      = "Out of Stock"
AVAIL_UNKNOWN           = "Unknown"

# Phrases Amazon uses and what they map to
_PHRASE_MAP = [
    # Temporarily out of stock (working hard to restock)
    ("temporarily out of stock",                              AVAIL_TEMP_OOS),
    ("we are working hard to be back in stock",               AVAIL_TEMP_OOS),
    ("working hard to be back in stock",                      AVAIL_TEMP_OOS),
    # Currently unavailable (no restock signal)
    ("currently unavailable",                                 AVAIL_CURRENTLY_UNAVAIL),
    ("we don't know when or if this item will be back",       AVAIL_CURRENTLY_UNAVAIL),
    ("we do not know when or if this item will be back",      AVAIL_CURRENTLY_UNAVAIL),
    # In stock variants
    ("in stock",                                              AVAIL_IN_STOCK),
    # Low stock
    ("only",                                                  None),   # handled separately (needs "left")
    # Out of stock
    ("out of stock",                                          AVAIL_OUT_OF_STOCK),
    ("not available",                                         AVAIL_OUT_OF_STOCK),
]

def _phrase_to_avail(txt):
    """Map a lowercased text blob to an availability constant, or None."""
    for phrase, status in _PHRASE_MAP:
        if phrase in txt:
            if phrase == "only":
                if "left" in txt:
                    return AVAIL_LOW_STOCK
                continue
            return status
    return None


def detect_availability(soup, price):
    """
    Multi-layer availability detection.
    Priority: explicit DOM signals > buybox buttons > full-page text > price fallback.
    """

    # ── Layer 1: #availability div (most reliable) ────────────────────────────
    avail_div = soup.find("div", {"id": "availability"})
    if avail_div:
        txt = avail_div.get_text(separator=" ", strip=True).lower()
        result = _phrase_to_avail(txt)
        if result:
            return result

    # ── Layer 2: Add-to-Cart / Buy-Now buttons present → definitely In Stock ──
    if soup.find("input", {"id": "add-to-cart-button"}) or \
       soup.find("input", {"id": "buy-now-button"}):
        return AVAIL_IN_STOCK

    # ── Layer 3: merchant-info block ──────────────────────────────────────────
    merchant = soup.find("div", {"id": "merchant-info"})
    if merchant:
        txt = merchant.get_text(separator=" ", strip=True).lower()
        result = _phrase_to_avail(txt)
        if result:
            return result

    # ── Layer 4: outOfStock div ───────────────────────────────────────────────
    oos_div = soup.find("div", {"id": "outOfStock"})
    if oos_div:
        txt = oos_div.get_text(separator=" ", strip=True).lower()
        result = _phrase_to_avail(txt)
        if result:
            return result
        # div exists but no matching phrase → treat as unavailable
        return AVAIL_OUT_OF_STOCK

    # ── Layer 5: Full-page text sweep ─────────────────────────────────────────
    page_text = soup.get_text(separator=" ", strip=True).lower()
    result = _phrase_to_avail(page_text)
    if result:
        return result

    # ── Layer 6: Price fallback ───────────────────────────────────────────────
    if price:
        return AVAIL_IN_STOCK

    return AVAIL_UNKNOWN


def parse(html, asin, domain):
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # ── Title ─────────────────────────────────────────────────────────────────
    title = None
    try:
        tag = soup.find("span", {"id": "productTitle"})
        if tag:
            title = tag.get_text(strip=True)
    except Exception:
        pass

    # ── Price (sale price only, skip MRP/strikethrough) ───────────────────────
    price = None
    try:
        core = (
            soup.find("div", {"id": "corePriceDisplay_desktop_feature_div"}) or
            soup.find("div", {"id": "corePrice_desktop"}) or
            soup.find("div", {"id": "apex_desktop"})
        )
        if core:
            for block in core.find_all("span", {"class": "a-price"}):
                cls = " ".join(block.get("class", []))
                if any(x in cls for x in ["a-text-strike", "basisPrice", "a-text-price"]):
                    continue
                off = block.find("span", {"class": "a-offscreen"})
                if off:
                    p = clean_price(off.get_text())
                    if p:
                        price = p
                        break

        if not price:
            for block in soup.find_all("span", {"class": "a-price"}):
                cls = " ".join(block.get("class", []))
                if any(x in cls for x in ["a-text-strike", "basisPrice", "a-text-price"]):
                    continue
                off = block.find("span", {"class": "a-offscreen"})
                if off:
                    p = clean_price(off.get_text())
                    if p:
                        price = p
                        break

        if not price:
            whole = soup.find("span", {"class": "a-price-whole"})
            frac  = soup.find("span", {"class": "a-price-fraction"})
            if whole:
                w = re.sub(r"[^\d]", "", whole.get_text())
                f = re.sub(r"[^\d]", "", frac.get_text() if frac else "00")
                try:
                    price = float(f"{w}.{f}") if w else None
                except Exception:
                    pass

        if not price:
            for pid in ["priceblock_ourprice", "priceblock_dealprice", "priceblock_saleprice"]:
                tag = soup.find("span", {"id": pid})
                if tag:
                    p = clean_price(tag.get_text())
                    if p:
                        price = p
                        break

    except Exception:
        pass

    if price and price > 500000:
        price = None

    # ── Availability ──────────────────────────────────────────────────────────
    availability = detect_availability(soup, price)

    # Nullify price for unavailable/OOS products to avoid leaking MRP from DOM
    if availability in (AVAIL_CURRENTLY_UNAVAIL, AVAIL_TEMP_OOS, AVAIL_OUT_OF_STOCK):
        price = None

    # ── Rating (score) ────────────────────────────────────────────────────────
    rating = None
    try:
        tag = soup.find("span", {"class": "a-icon-alt"})
        if tag:
            m = re.search(r"([\d.]+)\s*out\s*of\s*5", tag.get_text())
            if m:
                rating = float(m.group(1))
        if not rating:
            tag = soup.find("i", {"class": "a-icon-star"})
            if tag:
                m = re.search(r"([\d.]+)", tag.get_text())
                if m:
                    rating = float(m.group(1))
    except Exception:
        pass

    # ── Rating count ──────────────────────────────────────────────────────────
    rating_count = None
    try:
        # Method 1: #acrCustomerReviewText  e.g. "154 ratings"
        tag = soup.find("span", {"id": "acrCustomerReviewText"})
        if tag:
            m = re.search(r"([\d,]+)", tag.get_text())
            if m:
                rating_count = int(m.group(1).replace(",", ""))

        # Method 2: link text next to star widget  e.g. "(27)"  or  "27 ratings"
        if not rating_count:
            tag = soup.find("a", {"id": "acrCustomerReviewLink"})
            if tag:
                m = re.search(r"([\d,]+)", tag.get_text())
                if m:
                    rating_count = int(m.group(1).replace(",", ""))

        # Method 3: data-hook attribute used by newer Amazon pages
        if not rating_count:
            tag = soup.find(attrs={"data-hook": "total-review-count"})
            if tag:
                m = re.search(r"([\d,]+)", tag.get_text())
                if m:
                    rating_count = int(m.group(1).replace(",", ""))

        # Method 4: generic scan — find span near star widget containing "rating"
        if not rating_count:
            for span in soup.find_all("span"):
                txt = span.get_text(strip=True)
                if re.search(r"[\d,]+\s+ratings?", txt, re.I):
                    m = re.search(r"([\d,]+)", txt)
                    if m:
                        rating_count = int(m.group(1).replace(",", ""))
                        break

    except Exception:
        pass

    return {
        "asin":         asin,
        "url":          f"https://www.{domain}/dp/{asin}",
        "title":        title,
        "price":        price,
        "currency":     "INR" if domain == "amazon.in" else "USD",
        "rating":       rating,
        "rating_count": rating_count,
        "availability": availability,
        "status":       200,
    }


def scrape(asin, domain):
    try:
        url = f"https://www.{domain}/dp/{asin}?th=1&psc=1"
        html, err = fetch_with_retry(url, max_attempts=6)
        if err == "DEAD_PAGE":
            return {
                "error": "Product does not exist on Amazon. The ASIN may be invalid or delisted.",
                "status": 404,
                "dead": True
            }
        if err:
            return {"error": err, "status": 422}

        data = parse(html, asin, domain)

        if not data["title"] and not data["price"] and data["availability"] == AVAIL_UNKNOWN:
            return {"error": "Could not extract data. Try again.", "status": 422}

        return data

    except Exception as e:
        return {"error": str(e), "status": 500}


def to_json(data):
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except Exception:
        return b'{"error":"JSON encode failed","status":500}'


class handler(BaseHTTPRequestHandler):

    def send_json(self, data):
        body = to_json(data)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            asin   = params.get("asin",   [""])[0].strip().upper()
            domain = params.get("domain", ["amazon.in"])[0].strip()
            allowed = ["amazon.in", "amazon.com", "amazon.co.uk", "amazon.de", "amazon.co.jp"]

            if not asin or not re.match(r"^[A-Z0-9]{10}$", asin):
                self.send_json({"error": "Invalid ASIN.", "status": 400})
                return
            if domain not in allowed:
                self.send_json({"error": "Unsupported domain.", "status": 400})
                return

            self.send_json(scrape(asin, domain))

        except Exception as e:
            self.send_json({"error": str(e), "status": 500})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass
