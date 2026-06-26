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
        "we couldn\'t find that page",
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
            # Fast first try, then add small delays
            if attempt > 0:
                time.sleep(random.uniform(0.5, 1.5) * attempt)

            session.headers.update(get_headers())
            resp = session.get(url, timeout=12, allow_redirects=True)

            if resp.status_code == 404:
                return None, "Product not found. Check the ASIN."
            if resp.status_code in (503, 429, 403):
                continue  # retry immediately with new UA
            if resp.status_code != 200:
                continue

            html = resp.text

            # Dead product — stop immediately, no point retrying
            if is_dead_page(html):
                return None, "DEAD_PAGE"

            if is_blocked(html):
                continue  # retry with new UA

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

    # ── Availability — resolved FIRST so we never leak carousel prices ──
    # Amazon shows "similar product" prices on OOS pages in the same
    # price selectors; we must gate price extraction on availability.
    availability = "Unknown"
    is_available = False
    try:
        avail_div = soup.find("div", {"id": "availability"})
        if avail_div:
            txt = avail_div.get_text(strip=True).lower()
            if "in stock" in txt:
                availability = "In Stock"
                is_available = True
            elif "only" in txt and "left" in txt:
                availability = "Low Stock"
                is_available = True
            elif any(x in txt for x in [
                "out of stock", "unavailable", "not available",
                "currently unavailable",
            ]):
                availability = "Out of Stock"
                is_available = False
            else:
                # Truncated raw text — treat as unknown, still try price
                availability = avail_div.get_text(strip=True)[:50]
                is_available = None   # uncertain — attempt price anyway
        else:
            # No availability div — infer later from buybox
            is_available = None
    except Exception:
        pass

    # ── Price (buybox only, skip MRP/strikethrough) ────────────
    # We ONLY look inside the buybox / core-price containers so we never
    # accidentally pick up "sponsored" or "similar items" carousel prices.
    price = None
    if is_available is not False:   # skip entirely if confirmed OOS
        try:
            # Scope: prefer the buybox region; fall back progressively.
            buybox = (
                soup.find("div", {"id": "buyBoxAccordion"}) or
                soup.find("div", {"id": "buybox"}) or
                soup.find("div", {"id": "rightCol"}) or
                soup.find("div", {"id": "dp-container"})
            )
            scope = buybox if buybox else soup

            # Method 1: corePriceDisplay block inside the scoped region
            core = (
                scope.find("div", {"id": "corePriceDisplay_desktop_feature_div"}) or
                scope.find("div", {"id": "corePrice_desktop"}) or
                scope.find("div", {"id": "apex_desktop"})
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

            # Method 2: any non-MRP a-price span inside the scoped region
            if not price:
                for block in scope.find_all("span", {"class": "a-price"}):
                    cls = " ".join(block.get("class", []))
                    if any(x in cls for x in ["a-text-strike", "basisPrice", "a-text-price"]):
                        continue
                    off = block.find("span", {"class": "a-offscreen"})
                    if off:
                        p = clean_price(off.get_text())
                        if p:
                            price = p
                            break

            # Method 3: whole + fraction (scoped)
            if not price:
                whole = scope.find("span", {"class": "a-price-whole"})
                frac  = scope.find("span", {"class": "a-price-fraction"})
                if whole:
                    w = re.sub(r"[^\d]", "", whole.get_text())
                    f = re.sub(r"[^\d]", "", frac.get_text() if frac else "00")
                    try:
                        price = float(f"{w}.{f}") if w else None
                    except Exception:
                        pass

            # Method 4: legacy price-block IDs (always page-wide, but rare now)
            if not price:
                for pid in ["priceblock_ourprice", "priceblock_dealprice", "priceblock_saleprice"]:
                    tag = soup.find("span", {"id": pid})
                    if tag:
                        p = clean_price(tag.get_text())
                        if p:
                            price = p
                            break

            if price and price > 500000:
                price = None

        except Exception:
            pass

    # ── Reconcile availability with price evidence ──────────────
    # If availability was unknown and we found no price, mark OOS.
    if availability == "Unknown":
        if price:
            availability = "In Stock"
        else:
            availability = "Out of Stock"

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

        if not data["title"] and not data["price"]:
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
