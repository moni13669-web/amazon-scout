from http.server import BaseHTTPRequestHandler
import json
import re
import time
import random
import traceback
from urllib.parse import urlparse, parse_qs

import cloudscraper
from bs4 import BeautifulSoup


# ── Valid cloudscraper platforms: linux, windows, darwin, android, ios ──────
PROFILES = [
    {
        "browser": "chrome", "platform": "windows", "desktop": True,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "lang": "en-IN,en;q=0.9",
        "sec_ch": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_mobile": "?0", "sec_ch_platform": '"Windows"',
    },
    {
        "browser": "chrome", "platform": "darwin", "desktop": True,
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "lang": "en-GB,en;q=0.9",
        "sec_ch": '"Chromium";v="123", "Google Chrome";v="123", "Not-A.Brand";v="99"',
        "sec_ch_mobile": "?0", "sec_ch_platform": '"macOS"',
    },
    {
        "browser": "firefox", "platform": "windows", "desktop": True,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "lang": "en-US,en;q=0.5",
        "sec_ch": None, "sec_ch_mobile": None, "sec_ch_platform": None,
    },
    {
        "browser": "chrome", "platform": "linux", "desktop": True,
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "lang": "en-US,en;q=0.8",
        "sec_ch": '"Chromium";v="122", "Google Chrome";v="122", "Not-A.Brand";v="99"',
        "sec_ch_mobile": "?0", "sec_ch_platform": '"Linux"',
    },
]


def build_headers(profile):
    h = {
        "User-Agent": profile["ua"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": profile["lang"],
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }
    if profile.get("sec_ch"):
        h["Sec-Ch-Ua"]          = profile["sec_ch"]
        h["Sec-Ch-Ua-Mobile"]   = profile["sec_ch_mobile"]
        h["Sec-Ch-Ua-Platform"] = profile["sec_ch_platform"]
    return h


def is_blocked(html):
    lower = html.lower()
    return any(s in lower for s in [
        "robot check", "captcha", "validatecaptcha",
        "enter the characters", "not a robot",
        "api-services-support@amazon.com",
    ])


def safe_text(tag):
    try:
        if tag is None:
            return None
        text = tag.get_text(separator=" ", strip=True)
        return text if text else None
    except Exception:
        return None


def clean_price(text):
    """
    Extract the ACTUAL selling price — NOT MRP.
    Strips ₹, commas, spaces. Validates range 1–500000.
    """
    if not text:
        return None
    try:
        # Remove currency symbols and commas
        cleaned = re.sub(r"[₹$£€,\s]", "", str(text))
        # Keep only digits and one dot
        cleaned = re.sub(r"[^\d.]", "", cleaned)
        if not cleaned:
            return None
        # Handle multiple dots
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = parts[0]
        val = float(cleaned)
        return val if 1 <= val <= 500000 else None
    except Exception:
        return None


def fetch_page(url, timeout=14):
    """Try profiles in random order. Return first successful HTML."""
    profiles = random.sample(PROFILES, len(PROFILES))

    for i, profile in enumerate(profiles):
        try:
            if i > 0:
                time.sleep(random.uniform(1.5, 3))

            scraper = cloudscraper.create_scraper(
                browser={
                    "browser": profile["browser"],
                    "platform": profile["platform"],
                    "desktop": profile["desktop"],
                },
            )
            scraper.headers.update(build_headers(profile))

            resp = scraper.get(url, timeout=timeout, allow_redirects=True)

            if resp.status_code == 404:
                return None, "Product not found. Check the ASIN."
            if resp.status_code in (503, 429, 403):
                continue
            if resp.status_code != 200:
                continue

            html = resp.text
            if not html or len(html) < 1000:
                continue
            if is_blocked(html):
                continue

            return html, None

        except Exception as e:
            err = str(e)
            if i == len(profiles) - 1:
                return None, f"Connection error: {err}"
            continue

    return None, "Amazon blocked all requests. Try again in a few minutes."


def extract_price_from_json(html):
    """
    Extract price from Amazon's embedded JSON data (most reliable).
    Amazon embeds price in JS variables on every page.
    """
    patterns = [
        # priceAmount in data JSON
        r'"priceAmount"\s*:\s*([\d.]+)',
        r'"price"\s*:\s*"?([\d.]+)"?',
        r'"buyingPrice"\s*:\s*([\d.]+)',
        r'"landingPrice"\s*:\s*([\d.]+)',
        r'"displayPrice"\s*:"[₹$]?([\d,]+\.?\d*)"',
        r'data-price="([\d.]+)"',
        r'"offerPrice"\s*:\s*"[^"]*?([\d,]+\.?\d*)"',
    ]
    for pat in patterns:
        matches = re.findall(pat, html)
        for m in matches:
            p = clean_price(m)
            if p and p > 10:
                return p
    return None


def parse_product(html, asin, domain):
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    result = {
        "asin":             asin,
        "url":              f"https://www.{domain}/dp/{asin}",
        "title":            None,
        "price":            None,
        "mrp":              None,
        "discount_percent": None,
        "currency":         "INR" if domain == "amazon.in" else "USD",
        "availability":     "Unknown",
        "rating":           None,
        "reviews":          None,
        "brand":            None,
        "category":         None,
        "image_url":        None,
        "seller":           None,
        "status":           200,
    }

    # ── Title ─────────────────────────────────────────────────────────────
    try:
        for sel in [{"id": "productTitle"}, {"id": "title"}]:
            tag = soup.find("span", sel) or soup.find("h1", sel)
            if tag:
                t = safe_text(tag)
                if t and len(t) > 3:
                    result["title"] = t
                    break
    except Exception:
        pass

    # ── PRICE (Critical — must get the SALE price not MRP) ────────────────
    #
    # Amazon price hierarchy on amazon.in:
    #   1. corePriceDisplay block  → .a-price (non-strikethrough) → a-offscreen
    #   2. apex_desktop block
    #   3. Legacy price IDs
    #   4. whole + fraction spans
    #   5. JSON embedded data
    #
    # MRP is always in .a-text-price or .a-text-strike — NEVER use those for price.

    try:
        # Method 1: corePriceDisplay — most reliable on amazon.in
        core = (
            soup.find("div", {"id": "corePriceDisplay_desktop_feature_div"}) or
            soup.find("div", {"id": "corePrice_desktop"}) or
            soup.find("div", {"id": "apex_desktop"}) or
            soup.find("div", {"id": "buyBoxInner"}) or
            soup.find("div", {"id": "price"})
        )
        if core:
            # Find ALL price spans, skip strikethrough ones
            for block in core.find_all("span", {"class": "a-price"}):
                classes = " ".join(block.get("class", []))
                # Skip MRP/strikethrough price
                if any(x in classes for x in ["a-text-strike", "basisPrice", "a-text-price"]):
                    continue
                off = block.find("span", {"class": "a-offscreen"})
                if off:
                    p = clean_price(safe_text(off))
                    if p:
                        result["price"] = p
                        break

        # Method 2: global search — skip strikethrough
        if not result["price"]:
            for block in soup.find_all("span", {"class": "a-price"}):
                classes = " ".join(block.get("class", []))
                if any(x in classes for x in ["a-text-strike", "basisPrice", "a-text-price"]):
                    continue
                off = block.find("span", {"class": "a-offscreen"})
                if off:
                    p = clean_price(safe_text(off))
                    if p:
                        result["price"] = p
                        break

        # Method 3: legacy price IDs
        if not result["price"]:
            for pid in [
                "priceblock_ourprice",
                "priceblock_dealprice",
                "priceblock_saleprice",
                "tp_price_block_total_price_ww",
            ]:
                tag = soup.find("span", {"id": pid})
                if tag:
                    p = clean_price(safe_text(tag))
                    if p:
                        result["price"] = p
                        break

        # Method 4: whole + fraction
        if not result["price"]:
            whole = soup.find("span", {"class": "a-price-whole"})
            frac  = soup.find("span", {"class": "a-price-fraction"})
            if whole:
                w = re.sub(r"[^\d]", "", safe_text(whole) or "")
                f = re.sub(r"[^\d]", "", safe_text(frac) or "00")
                try:
                    candidate = float(f"{w}.{f}") if w else None
                    if candidate:
                        result["price"] = candidate
                except Exception:
                    pass

        # Method 5: JSON embedded
        if not result["price"]:
            result["price"] = extract_price_from_json(html)

    except Exception:
        pass

    # ── MRP ───────────────────────────────────────────────────────────────
    try:
        # MRP is in a-text-price or a-text-strike spans
        for block in soup.find_all("span", {"class": "a-price"}):
            classes = " ".join(block.get("class", []))
            if "a-text-price" in classes or "basisPrice" in classes:
                off = block.find("span", {"class": "a-offscreen"})
                if off:
                    m = clean_price(safe_text(off))
                    # MRP must be higher than sale price
                    if m and (not result["price"] or m > result["price"]):
                        result["mrp"] = m
                        break

        # Fallback: strikethrough text
        if not result["mrp"]:
            strike = soup.find("span", {"class": "a-text-strike"})
            if not strike:
                strike = soup.find("span", {"class": re.compile("a-text-price")})
            if strike:
                m = clean_price(safe_text(strike))
                if m and (not result["price"] or m > result["price"]):
                    result["mrp"] = m

        # If price and mrp got swapped (mrp < price), swap them back
        if result["price"] and result["mrp"]:
            if result["mrp"] < result["price"]:
                result["price"], result["mrp"] = result["mrp"], result["price"]

    except Exception:
        pass

    # ── Discount ──────────────────────────────────────────────────────────
    try:
        if result["price"] and result["mrp"] and result["mrp"] > result["price"]:
            result["discount_percent"] = round(
                ((result["mrp"] - result["price"]) / result["mrp"]) * 100, 1
            )
        else:
            # Try reading discount badge from page
            for cls in ["savingsPercentage", "reinventPriceSavingsPercentageMargin"]:
                tag = soup.find("span", {"class": cls})
                if tag:
                    m = re.search(r"(\d+)", safe_text(tag) or "")
                    if m:
                        result["discount_percent"] = float(m.group(1))
                        break
    except Exception:
        pass

    # ── Availability ──────────────────────────────────────────────────────
    try:
        avail_div = (
            soup.find("div", {"id": "availability"}) or
            soup.find("div", {"id": "outOfStock"}) or
            soup.find("span", {"class": "availabilityMessage"})
        )
        if avail_div:
            txt = (safe_text(avail_div) or "").lower()
            if "in stock" in txt or "only" in txt and "left" in txt:
                result["availability"] = "In Stock"
                if "only" in txt and "left" in txt:
                    result["availability"] = "Low Stock"
            elif (
                "out of stock" in txt or
                "currently unavailable" in txt or
                "unavailable" in txt or
                "not available" in txt
            ):
                result["availability"] = "Out of Stock"
            elif "usually" in txt or "days" in txt or "weeks" in txt:
                result["availability"] = "Ships Soon"
            else:
                raw = (safe_text(avail_div) or "").strip()
                result["availability"] = raw[:60] if raw else "Unknown"
        elif result["price"]:
            result["availability"] = "In Stock"
        else:
            # No price + no availability = likely out of stock
            result["availability"] = "Out of Stock"
    except Exception:
        pass

    # ── Rating ────────────────────────────────────────────────────────────
    try:
        for sel in [{"class": "a-icon-alt"}, {"id": "acrPopover"}]:
            tag = soup.find("span", sel) or soup.find("i", sel)
            if tag:
                txt = safe_text(tag) or ""
                m = re.search(r"([\d.]+)\s*out\s*of\s*5", txt)
                if m:
                    result["rating"] = float(m.group(1))
                    break
        if not result["rating"]:
            m = re.search(r'"ratingScore"\s*:\s*"([\d.]+)"', html)
            if m:
                result["rating"] = float(m.group(1))
    except Exception:
        pass

    # ── Reviews ───────────────────────────────────────────────────────────
    try:
        rev_tag = soup.find("span", {"id": "acrCustomerReviewText"})
        if rev_tag:
            result["reviews"] = safe_text(rev_tag)
        if not result["reviews"]:
            m = re.search(r'"totalReviewCount"\s*:\s*(\d+)', html)
            if m:
                result["reviews"] = f"{int(m.group(1)):,} ratings"
    except Exception:
        pass

    # ── Brand ─────────────────────────────────────────────────────────────
    try:
        for bid in ["bylineInfo", "brand"]:
            tag = soup.find(attrs={"id": bid})
            if tag:
                b = safe_text(tag) or ""
                b = re.sub(r"(Brand:|Visit the|Store|\n)", "", b).strip()
                if b:
                    result["brand"] = b[:60]
                    break
        if not result["brand"]:
            rows = soup.find_all("tr")
            for row in rows:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    label = (safe_text(cells[0]) or "").lower()
                    if "brand" in label:
                        b = safe_text(cells[1])
                        if b:
                            result["brand"] = b[:60]
                            break
    except Exception:
        pass

    # ── Image ─────────────────────────────────────────────────────────────
    try:
        for img_id in ["landingImage", "imgBlkFront", "main-image"]:
            img = soup.find("img", {"id": img_id})
            if img:
                url = img.get("data-old-hires") or img.get("data-src") or img.get("src")
                if url and url.startswith("http"):
                    result["image_url"] = url
                    break
        if not result["image_url"]:
            m = re.search(r'"hiRes"\s*:\s*"(https://[^"]+)"', html)
            if m:
                result["image_url"] = m.group(1)
    except Exception:
        pass

    # ── Category ──────────────────────────────────────────────────────────
    try:
        bc = (
            soup.find("div", {"id": "wayfinding-breadcrumbs_feature_div"}) or
            soup.find("ul", {"class": "a-breadcrumb"})
        )
        if bc:
            crumbs = [a.get_text(strip=True) for a in bc.find_all("a") if a.get_text(strip=True)]
            if crumbs:
                result["category"] = " > ".join(crumbs[-2:])
    except Exception:
        pass

    # ── Seller ────────────────────────────────────────────────────────────
    try:
        for sid in ["merchant-info", "sellerProfileTriggerId", "tabular-buybox-truncate-0"]:
            tag = soup.find(attrs={"id": sid})
            if tag:
                s = safe_text(tag)
                if s and len(s) > 1:
                    result["seller"] = s[:80]
                    break
    except Exception:
        pass

    return result


def scrape_amazon(asin, domain="amazon.in"):
    try:
        url = f"https://www.{domain}/dp/{asin}?th=1&psc=1"
        html, error = fetch_page(url, timeout=13)

        if error:
            return {"error": error, "status": 422}

        data = parse_product(html, asin, domain)

        if not data.get("title") and not data.get("price"):
            return {
                "error": "Could not extract product data. Amazon may have changed its layout. Try again.",
                "status": 422,
            }

        return data

    except Exception as e:
        return {
            "error": f"Scraper error: {str(e)}",
            "status": 500,
        }


def json_response(data):
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except Exception as e:
        return json.dumps({"error": f"JSON error: {str(e)}", "status": 500}).encode("utf-8")


# ── Vercel serverless handler ─────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def send_json(self, data):
        body = json_response(data)
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
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            asin   = params.get("asin",   [""])[0].strip().upper()
            domain = params.get("domain", ["amazon.in"])[0].strip()
            allowed = ["amazon.in", "amazon.com", "amazon.co.uk", "amazon.de", "amazon.co.jp"]

            if not asin or not re.match(r"^[A-Z0-9]{10}$", asin):
                self.send_json({"error": "Invalid ASIN. Must be 10 alphanumeric characters.", "status": 400})
                return

            if domain not in allowed:
                self.send_json({"error": "Unsupported domain.", "status": 400})
                return

            result = scrape_amazon(asin, domain)
            self.send_json(result)

        except Exception as e:
            self.send_json({"error": f"Handler error: {str(e)}", "status": 500})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass
