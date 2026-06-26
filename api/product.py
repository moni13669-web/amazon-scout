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
    """Detect Amazon 404 / dead product pages — no point retrying these."""
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
    """
    Retry up to max_attempts times with increasing delay.
    Switches UA every attempt.
    """
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


def detect_availability(soup, price):
    """
    Robustly detect product availability.
    Returns one of: "In Stock", "Currently Unavailable", "Low Stock", "Out of Stock", "Unknown"
    """
    # ── Check #1: merchant-info / buybox unavailability signals ──────────────
    # These are the most reliable indicators for "Currently unavailable"
    unavail_ids = [
        "outOfStock",
        "availability",
        "exports_desktop_qualifiedBuyBox_tlc_feature_div",
        "buybox-see-all-buying-choices",
    ]

    # Check the dedicated availability div first
    avail_div = soup.find("div", {"id": "availability"})
    if avail_div:
        txt = avail_div.get_text(separator=" ", strip=True).lower()
        if any(x in txt for x in [
            "currently unavailable",
            "we don't know when or if this item will be back in stock",
            "unavailable",
        ]):
            return "Currently Unavailable"
        if "in stock" in txt:
            return "In Stock"
        if "only" in txt and "left" in txt:
            return "Low Stock"
        if any(x in txt for x in ["out of stock", "not available"]):
            return "Out of Stock"

    # ── Check #2: Add to Cart / Buy Now buttons present? ─────────────────────
    # If these exist, the product is purchasable → In Stock
    add_to_cart = soup.find("input", {"id": "add-to-cart-button"})
    buy_now     = soup.find("input", {"id": "buy-now-button"})
    if add_to_cart or buy_now:
        return "In Stock"

    # ── Check #3: merchant-info block containing "Currently unavailable" ──────
    merchant_info = soup.find("div", {"id": "merchant-info"})
    if merchant_info:
        txt = merchant_info.get_text(separator=" ", strip=True).lower()
        if "currently unavailable" in txt or "unavailable" in txt:
            return "Currently Unavailable"

    # ── Check #4: olp_feature_div — "Currently unavailable" often appears here ─
    olp = soup.find("div", {"id": "olp_feature_div"})
    if olp:
        txt = olp.get_text(separator=" ", strip=True).lower()
        if "currently unavailable" in txt:
            return "Currently Unavailable"

    # ── Check #5: Scan full page text for the canonical Amazon phrase ─────────
    page_text = soup.get_text(separator=" ", strip=True).lower()
    if "currently unavailable" in page_text:
        return "Currently Unavailable"
    if "we don't know when or if this item will be back in stock" in page_text:
        return "Currently Unavailable"

    # ── Fallback: infer from price ────────────────────────────────────────────
    if price:
        return "In Stock"

    return "Unknown"


def parse(html, asin, domain):
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # ── Title ──────────────────────────────────────────────────
    title = None
    try:
        tag = soup.find("span", {"id": "productTitle"})
        if tag:
            title = tag.get_text(strip=True)
    except Exception:
        pass

    # ── Price (sale price only, skip MRP/strikethrough) ────────
    price = None
    try:
        # Method 1: corePriceDisplay block — most reliable
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

        # Method 2: any non-MRP price span on page
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

        # Method 3: whole + fraction
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

        # Method 4: legacy IDs
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

    # ── Availability ───────────────────────────────────────────
    # Always detect availability independently — do NOT infer from price alone.
    availability = detect_availability(soup, price)

    # ── If currently unavailable, clear price (no buyable price shown) ────────
    # Amazon sometimes leaks MRP into the DOM even for unavailable products,
    # so we nullify it to avoid misleading the caller.
    if availability == "Currently Unavailable":
        price = None

    # ── Rating ─────────────────────────────────────────────────
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

    return {
        "asin":         asin,
        "url":          f"https://www.{domain}/dp/{asin}",
        "title":        title,
        "price":        price,
        "currency":     "INR" if domain == "amazon.in" else "USD",
        "rating":       rating,
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

        # A title with no price and "Unknown" availability is suspicious — could be a parse fail
        if not data["title"] and not data["price"] and data["availability"] == "Unknown":
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
