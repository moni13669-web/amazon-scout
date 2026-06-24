from http.server import BaseHTTPRequestHandler
import json
import re
import time
import random
import traceback
from urllib.parse import urlparse, parse_qs

import cloudscraper
from bs4 import BeautifulSoup


# ── Browser profiles ────────────────────────────────────────────────────────
PROFILES = [
    {
        "browser": "chrome", "platform": "windows", "desktop": True,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "lang": "en-IN,en;q=0.9",
        "sec_ch": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_mobile": "?0", "sec_ch_platform": '"Windows"',
    },
    {
        "browser": "chrome", "platform": "macos", "desktop": True,
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


def safe_text(tag, strip=True):
    """Safely extract text from a BS4 tag, returns None if tag is None."""
    try:
        if tag is None:
            return None
        text = tag.get_text(separator=" ", strip=strip)
        return text if text else None
    except Exception:
        return None


def clean_price(text):
    """Extract float price from any string like ₹1,299 or 1299.00"""
    if not text:
        return None
    try:
        cleaned = re.sub(r"[^\d.]", "", str(text).replace(",", ""))
        # Remove multiple dots
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = parts[0] + "." + parts[1]
        val = float(cleaned)
        # Sanity check — prices between ₹1 and ₹10,00,000
        return val if 1 <= val <= 1000000 else None
    except Exception:
        return None


def fetch_page(url, timeout=15):
    """
    Try each profile once with short timeout.
    Vercel has 10s limit — keep total time under 9s.
    """
    profiles = random.sample(PROFILES, len(PROFILES))

    for i, profile in enumerate(profiles):
        try:
            # Only sleep on retries, not first attempt
            if i > 0:
                time.sleep(random.uniform(1, 2))

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
            if resp.status_code in (503, 429):
                continue  # try next profile
            if resp.status_code != 200:
                continue

            html = resp.text
            if not html or len(html) < 500:
                continue  # empty response

            if is_blocked(html):
                continue  # try next profile

            return html, None

        except Exception as e:
            if i == len(profiles) - 1:
                return None, f"Connection error: {str(e)}"
            continue

    return None, "Amazon blocked all requests. Try again in a few minutes."


def parse_product(html, asin, domain):
    """Parse product data from Amazon HTML with multiple fallback selectors."""
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

    # ── Title ──────────────────────────────────────────────────────────────
    try:
        for sel in [{"id": "productTitle"}, {"id": "title"}, {"class": "product-title"}]:
            tag = soup.find("span", sel) or soup.find("h1", sel)
            if tag:
                t = safe_text(tag)
                if t and len(t) > 3:
                    result["title"] = t
                    break
    except Exception:
        pass

    # ── Price ───────────────────────────────────────────────────────────────
    try:
        # Method 1: apex price block (most reliable on amazon.in)
        apex = soup.find("div", {"id": "apex_desktop"}) or \
               soup.find("div", {"id": "corePriceDisplay_desktop_feature_div"}) or \
               soup.find("div", {"id": "corePrice_desktop"}) or \
               soup.find("div", {"id": "price"})

        if apex:
            for block in apex.find_all("span", {"class": "a-price"}):
                if "a-text-strike" in " ".join(block.get("class", [])):
                    continue
                off = block.find("span", {"class": "a-offscreen"})
                if off:
                    p = clean_price(safe_text(off))
                    if p:
                        result["price"] = p
                        break

        # Method 2: global price spans
        if not result["price"]:
            for block in soup.find_all("span", {"class": "a-price"}):
                classes = " ".join(block.get("class", []))
                if "a-text-strike" in classes or "basisPrice" in classes:
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
                "priceblock_ourprice", "priceblock_dealprice",
                "priceblock_saleprice", "tp_price_block_total_price_ww",
                "kindle-price", "buyNewSection",
            ]:
                tag = soup.find(attrs={"id": pid})
                if tag:
                    p = clean_price(safe_text(tag))
                    if p:
                        result["price"] = p
                        break

        # Method 4: price-whole + price-fraction
        if not result["price"]:
            whole = soup.find("span", {"class": "a-price-whole"})
            frac  = soup.find("span", {"class": "a-price-fraction"})
            if whole:
                w = re.sub(r"[^\d]", "", safe_text(whole) or "")
                f = re.sub(r"[^\d]", "", safe_text(frac) or "00")
                try:
                    result["price"] = float(f"{w}.{f}") if w else None
                except Exception:
                    pass

        # Method 5: regex scan entire page for price pattern
        if not result["price"]:
            matches = re.findall(r'["\']price["\']\s*:\s*["\']?([\d,]+\.?\d*)["\']?', html)
            for m in matches:
                p = clean_price(m)
                if p and p > 10:
                    result["price"] = p
                    break

    except Exception:
        pass

    # ── MRP ─────────────────────────────────────────────────────────────────
    try:
        for block in soup.find_all("span", {"class": "a-price"}):
            classes = " ".join(block.get("class", []))
            if "a-text-price" in classes or "basisPrice" in classes:
                off = block.find("span", {"class": "a-offscreen"})
                if off:
                    m = clean_price(safe_text(off))
                    if m and (not result["price"] or m > result["price"]):
                        result["mrp"] = m
                        break

        # Fallback: find strikethrough price
        if not result["mrp"]:
            strike = soup.find("span", {"class": "a-text-strike"})
            if strike:
                m = clean_price(safe_text(strike))
                if m:
                    result["mrp"] = m
    except Exception:
        pass

    # ── Discount ─────────────────────────────────────────────────────────────
    try:
        if result["price"] and result["mrp"] and result["mrp"] > result["price"]:
            result["discount_percent"] = round(
                ((result["mrp"] - result["price"]) / result["mrp"]) * 100, 1
            )
        else:
            for cls in ["savingsPercentage", "a-color-price"]:
                tag = soup.find("span", {"class": cls})
                if tag:
                    m = re.search(r"(\d+)%", safe_text(tag) or "")
                    if m:
                        result["discount_percent"] = float(m.group(1))
                        break
    except Exception:
        pass

    # ── Availability ─────────────────────────────────────────────────────────
    try:
        avail_div = soup.find("div", {"id": "availability"}) or \
                    soup.find("div", {"id": "outOfStock"})
        if avail_div:
            txt = (safe_text(avail_div) or "").lower()
            if "in stock" in txt:
                result["availability"] = "In Stock"
            elif "out of stock" in txt or "currently unavailable" in txt or "unavailable" in txt:
                result["availability"] = "Out of Stock"
            elif "only" in txt and "left" in txt:
                result["availability"] = "Low Stock"
            elif "usually" in txt or "days" in txt:
                result["availability"] = "Ships Soon"
            else:
                raw = (safe_text(avail_div) or "Unknown")[:50].strip()
                result["availability"] = raw if raw else "Unknown"
        elif result["price"]:
            # If we have a price, item is likely in stock
            result["availability"] = "In Stock"
    except Exception:
        pass

    # ── Rating ───────────────────────────────────────────────────────────────
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

    # ── Reviews ──────────────────────────────────────────────────────────────
    try:
        rev_tag = soup.find("span", {"id": "acrCustomerReviewText"})
        if rev_tag:
            result["reviews"] = safe_text(rev_tag)
        if not result["reviews"]:
            m = re.search(r'"totalReviewCount"\s*:\s*(\d+)', html)
            if m:
                result["reviews"] = f"{m.group(1)} ratings"
    except Exception:
        pass

    # ── Brand ────────────────────────────────────────────────────────────────
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
            # Try product details table
            rows = soup.find_all("tr")
            for row in rows:
                th = row.find("th") or row.find("td")
                td = row.find_all("td")
                if th and td and "brand" in (safe_text(th) or "").lower():
                    b = safe_text(td[-1])
                    if b:
                        result["brand"] = b[:60]
                        break
    except Exception:
        pass

    # ── Image ────────────────────────────────────────────────────────────────
    try:
        for img_id in ["landingImage", "imgBlkFront", "main-image"]:
            img = soup.find("img", {"id": img_id})
            if img:
                url = img.get("data-old-hires") or img.get("data-src") or img.get("src")
                if url and url.startswith("http"):
                    result["image_url"] = url
                    break

        if not result["image_url"]:
            # Try JSON embedded image data
            m = re.search(r'"hiRes"\s*:\s*"(https://[^"]+)"', html)
            if m:
                result["image_url"] = m.group(1)
    except Exception:
        pass

    # ── Category ─────────────────────────────────────────────────────────────
    try:
        bc = soup.find("div", {"id": "wayfinding-breadcrumbs_feature_div"}) or \
             soup.find("ul", {"class": "a-breadcrumb"})
        if bc:
            crumbs = [a.get_text(strip=True) for a in bc.find_all("a") if a.get_text(strip=True)]
            if crumbs:
                result["category"] = " > ".join(crumbs[-2:])
    except Exception:
        pass

    # ── Seller ───────────────────────────────────────────────────────────────
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
        html, error = fetch_page(url, timeout=12)

        if error:
            return {"error": error, "status": 422}

        data = parse_product(html, asin, domain)

        if not data.get("title") and not data.get("price"):
            return {
                "error": "Could not extract product data. Amazon may have changed its page layout. Try again.",
                "status": 422,
            }

        return data

    except Exception as e:
        return {
            "error": f"Scraper crashed: {str(e)}",
            "debug": traceback.format_exc()[-300:],
            "status": 500,
        }


def json_response(data):
    """Always returns valid JSON bytes, never raises."""
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except Exception as e:
        return json.dumps({"error": f"JSON encoding failed: {str(e)}", "status": 500}).encode("utf-8")


# ── Vercel serverless handler ────────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        body = json_response(data)
        self.send_response(status)
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
                self.send_json({"error": "Invalid ASIN. Must be 10 alphanumeric characters.", "status": 400}, 400)
                return

            if domain not in allowed:
                self.send_json({"error": "Unsupported domain.", "status": 400}, 400)
                return

            result = scrape_amazon(asin, domain)
            http_status = result.get("status", 200)
            # Only use 200 or 422 for HTTP status — avoid Vercel treating 5xx as infra error
            self.send_json(result, 200)

        except Exception as e:
            self.send_json({
                "error": f"Handler error: {str(e)}",
                "status": 500
            }, 200)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default request logs
