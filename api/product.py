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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
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
    """
    Robustly parse any INR/USD price string.
    Handles commas (1,299 / 12,999 / 1,00,000 / 10,00,000),
    rupee symbol, currency codes, trailing dots, etc.
    No upper-bound cap — handles prices up to any amount.
    """
    if not text:
        return None
    try:
        # Strip currency symbols and whitespace
        s = str(text).strip()
        s = re.sub(r"[₹$£€¥\s]", "", s)
        s = re.sub(r"[^\d.,]", "", s)

        # Remove all commas (Indian or international formatting)
        s = s.replace(",", "")

        # Handle multiple dots — keep only integer part
        parts = s.split(".")
        if len(parts) > 2:
            s = parts[0]
        elif len(parts) == 2:
            # e.g. "1299.00" → keep as float
            pass

        if not s or s == ".":
            return None

        val = float(s)
        # Must be at least ₹1 — no upper bound
        return val if val >= 1 else None
    except Exception:
        return None


def fetch_with_retry(url, max_attempts=6):
    session = requests.Session()
    for attempt in range(max_attempts):
        try:
            if attempt > 0:
                time.sleep(random.uniform(0.8, 2.0) * attempt)

            session.headers.update(get_headers())
            resp = session.get(url, timeout=15, allow_redirects=True)

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

_PHRASE_MAP = [
    ("temporarily out of stock",                        AVAIL_TEMP_OOS),
    ("we are working hard to be back in stock",         AVAIL_TEMP_OOS),
    ("working hard to be back in stock",                AVAIL_TEMP_OOS),
    ("currently unavailable",                           AVAIL_CURRENTLY_UNAVAIL),
    ("we don't know when or if this item will be back", AVAIL_CURRENTLY_UNAVAIL),
    ("we do not know when or if this item will be back",AVAIL_CURRENTLY_UNAVAIL),
    ("in stock",                                        AVAIL_IN_STOCK),
    ("out of stock",                                    AVAIL_OUT_OF_STOCK),
    ("not available",                                   AVAIL_OUT_OF_STOCK),
    ("only",                                            None),  # checked separately
]

def _phrase_to_avail(txt):
    for phrase, status in _PHRASE_MAP:
        if phrase in txt:
            if phrase == "only":
                if "left" in txt:
                    return AVAIL_LOW_STOCK
                continue
            return status
    return None

def detect_availability(soup, price):
    avail_div = soup.find("div", {"id": "availability"})
    if avail_div:
        txt = avail_div.get_text(separator=" ", strip=True).lower()
        result = _phrase_to_avail(txt)
        if result:
            return result

    if soup.find("input", {"id": "add-to-cart-button"}) or \
       soup.find("input", {"id": "buy-now-button"}):
        return AVAIL_IN_STOCK

    merchant = soup.find("div", {"id": "merchant-info"})
    if merchant:
        txt = merchant.get_text(separator=" ", strip=True).lower()
        result = _phrase_to_avail(txt)
        if result:
            return result

    oos_div = soup.find("div", {"id": "outOfStock"})
    if oos_div:
        txt = oos_div.get_text(separator=" ", strip=True).lower()
        result = _phrase_to_avail(txt)
        if result:
            return result
        return AVAIL_OUT_OF_STOCK

    page_text = soup.get_text(separator=" ", strip=True).lower()
    result = _phrase_to_avail(page_text)
    if result:
        return result

    if price:
        return AVAIL_IN_STOCK

    return AVAIL_UNKNOWN


def _extract_price_from_block(block):
    """
    Given a BeautifulSoup tag that is an a-price block,
    extract the numeric price, skipping if it's a strikethrough/MRP block.
    """
    cls = " ".join(block.get("class", []))
    # Skip MRP / strikethrough price blocks
    if any(x in cls for x in ["a-text-strike", "basisPrice", "a-text-price"]):
        return None

    # Primary: a-offscreen span (hidden accessible text, most reliable)
    off = block.find("span", {"class": "a-offscreen"})
    if off:
        return clean_price(off.get_text())

    # Fallback: whole + fraction
    whole = block.find("span", {"class": "a-price-whole"})
    if whole:
        frac = block.find("span", {"class": "a-price-fraction"})
        w = re.sub(r"[^\d]", "", whole.get_text())
        f = re.sub(r"[^\d]", "", frac.get_text() if frac else "00")
        if w:
            try:
                return clean_price(f"{w}.{f}")
            except Exception:
                pass
    return None


def parse_price(soup):
    """
    Extract the correct buybox/sale price from Amazon product page.

    Priority order (most → least reliable):
    1. corePriceDisplay buybox block  — the actual purchase price shown in the buy box
    2. priceToPay block               — newer Amazon layout
    3. apex_desktop block             — another buybox container
    4. twister/variation price        — variant selection price
    5. Page-wide a-price scan         — skip strikethrough ones
    6. Legacy priceblock IDs          — older Amazon pages
    7. Whole+fraction directly        — last resort
    """
    price = None

    # ── Method 1: corePriceDisplay — most reliable buybox price ──────────────
    # This div specifically holds the price you PAY, not the MRP
    core_ids = [
        "corePriceDisplay_desktop_feature_div",
        "corePrice_desktop",
        "corePrice_feature_div",
    ]
    for cid in core_ids:
        core = soup.find("div", {"id": cid})
        if not core:
            continue
        # Within core, find the priceToPay span first (most accurate)
        ttp = core.find("span", {"class": "priceToPay"})
        if ttp:
            off = ttp.find("span", {"class": "a-offscreen"})
            if off:
                p = clean_price(off.get_text())
                if p:
                    return p

        # Then try non-strikethrough a-price blocks
        for block in core.find_all("span", {"class": "a-price"}):
            p = _extract_price_from_block(block)
            if p:
                price = p
                break
        if price:
            break

    # ── Method 2: priceToPay anywhere on page ────────────────────────────────
    if not price:
        ttp = soup.find("span", {"class": "priceToPay"})
        if ttp:
            off = ttp.find("span", {"class": "a-offscreen"})
            if off:
                price = clean_price(off.get_text())

    # ── Method 3: apex_desktop / apex_offerDisplay ────────────────────────────
    if not price:
        for aid in ["apex_desktop", "apex_offerDisplay_desktop_feature_div"]:
            apex = soup.find("div", {"id": aid})
            if not apex:
                continue
            for block in apex.find_all("span", {"class": "a-price"}):
                p = _extract_price_from_block(block)
                if p:
                    price = p
                    break
            if price:
                break

    # ── Method 4: buybox price span (id-based) ───────────────────────────────
    if not price:
        buybox = soup.find("div", {"id": "buyBoxInner"}) or \
                 soup.find("div", {"id": "rightCol"}) or \
                 soup.find("div", {"id": "ppd"})
        if buybox:
            for block in buybox.find_all("span", {"class": "a-price"}):
                p = _extract_price_from_block(block)
                if p:
                    price = p
                    break

    # ── Method 5: all a-price blocks on page, skip strikethrough ─────────────
    if not price:
        for block in soup.find_all("span", {"class": "a-price"}):
            p = _extract_price_from_block(block)
            if p:
                price = p
                break

    # ── Method 6: legacy priceblock IDs ──────────────────────────────────────
    if not price:
        for pid in [
            "priceblock_ourprice",
            "priceblock_dealprice",
            "priceblock_saleprice",
            "priceblock_snsprice_Based",
        ]:
            tag = soup.find("span", {"id": pid})
            if tag:
                p = clean_price(tag.get_text())
                if p:
                    price = p
                    break

    # ── Method 7: raw whole+fraction anywhere ─────────────────────────────────
    if not price:
        whole = soup.find("span", {"class": "a-price-whole"})
        frac  = soup.find("span", {"class": "a-price-fraction"})
        if whole:
            w = re.sub(r"[^\d]", "", whole.get_text())
            f = re.sub(r"[^\d]", "", frac.get_text() if frac else "00")
            if w:
                try:
                    price = clean_price(f"{w}.{f}")
                except Exception:
                    pass

    return price


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

    # ── Price ─────────────────────────────────────────────────────────────────
    price = None
    try:
        price = parse_price(soup)
    except Exception:
        pass

    # ── Availability ──────────────────────────────────────────────────────────
    availability = detect_availability(soup, price)

    # Nullify price if product is not purchasable
    if availability in (AVAIL_CURRENTLY_UNAVAIL, AVAIL_TEMP_OOS, AVAIL_OUT_OF_STOCK):
        price = None

    # ── Rating score ──────────────────────────────────────────────────────────
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
        # Method 1: standard id
        tag = soup.find("span", {"id": "acrCustomerReviewText"})
        if tag:
            m = re.search(r"([\d,]+)", tag.get_text())
            if m:
                rating_count = int(m.group(1).replace(",", ""))

        # Method 2: review link
        if not rating_count:
            tag = soup.find("a", {"id": "acrCustomerReviewLink"})
            if tag:
                m = re.search(r"([\d,]+)", tag.get_text())
                if m:
                    rating_count = int(m.group(1).replace(",", ""))

        # Method 3: data-hook
        if not rating_count:
            tag = soup.find(attrs={"data-hook": "total-review-count"})
            if tag:
                m = re.search(r"([\d,]+)", tag.get_text())
                if m:
                    rating_count = int(m.group(1).replace(",", ""))

        # Method 4: generic scan
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
