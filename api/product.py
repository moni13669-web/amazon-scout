from http.server import BaseHTTPRequestHandler
import json
import re
import time
import random
from urllib.parse import urlparse, parse_qs

import cloudscraper
from bs4 import BeautifulSoup


# ── Rotating real browser fingerprints ─────────────────────────────────────
PROFILES = [
    {
        "browser": "chrome",
        "platform": "windows",
        "desktop": True,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "lang": "en-IN,en;q=0.9,hi;q=0.8",
        "sec_ch": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_mobile": "?0",
        "sec_ch_platform": '"Windows"',
    },
    {
        "browser": "chrome",
        "platform": "macos",
        "desktop": True,
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "lang": "en-GB,en;q=0.9",
        "sec_ch": '"Chromium";v="123", "Google Chrome";v="123", "Not-A.Brand";v="99"',
        "sec_ch_mobile": "?0",
        "sec_ch_platform": '"macOS"',
    },
    {
        "browser": "firefox",
        "platform": "windows",
        "desktop": True,
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "lang": "en-US,en;q=0.5",
        "sec_ch": None,
        "sec_ch_mobile": None,
        "sec_ch_platform": None,
    },
    {
        "browser": "chrome",
        "platform": "linux",
        "desktop": True,
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "lang": "en-US,en;q=0.8",
        "sec_ch": '"Chromium";v="122", "Google Chrome";v="122", "Not-A.Brand";v="99"',
        "sec_ch_mobile": "?0",
        "sec_ch_platform": '"Linux"',
    },
]


def build_headers(profile):
    headers = {
        "User-Agent": profile["ua"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": profile["lang"],
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "DNT": "1",
    }
    if profile.get("sec_ch"):
        headers["Sec-Ch-Ua"]          = profile["sec_ch"]
        headers["Sec-Ch-Ua-Mobile"]   = profile["sec_ch_mobile"]
        headers["Sec-Ch-Ua-Platform"] = profile["sec_ch_platform"]
    return headers


def is_blocked(html):
    lower = html.lower()
    signals = [
        "robot check",
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "type the characters you see in this image",
        "captcha",
        "api-services-support@amazon.com",
        "validatecaptcha",
    ]
    return any(s in lower for s in signals)


def make_scraper(profile):
    scraper = cloudscraper.create_scraper(
        browser={
            "browser": profile["browser"],
            "platform": profile["platform"],
            "desktop": profile["desktop"],
        },
        delay=random.uniform(2, 5),
    )
    scraper.headers.update(build_headers(profile))
    return scraper


def fetch_page(url, retries=3):
    profiles = random.sample(PROFILES, len(PROFILES))

    for attempt in range(retries):
        profile = profiles[attempt % len(profiles)]
        scraper = make_scraper(profile)
        delay = random.uniform(2, 4) + (attempt * random.uniform(1, 3))
        time.sleep(delay)

        try:
            resp = scraper.get(url, timeout=20, allow_redirects=True)

            if resp.status_code == 404:
                return None, "Product not found (404). Check the ASIN."
            if resp.status_code == 503:
                if attempt < retries - 1:
                    time.sleep(random.uniform(3, 6))
                    continue
                return None, "Amazon returned 503. Try again in a moment."
            if resp.status_code != 200:
                if attempt < retries - 1:
                    continue
                return None, f"Unexpected status {resp.status_code}."

            html = resp.text
            if is_blocked(html):
                if attempt < retries - 1:
                    time.sleep(random.uniform(4, 8))
                    continue
                return None, "Amazon is showing a CAPTCHA. Try again later."

            return html, None

        except Exception as e:
            if attempt < retries - 1:
                time.sleep(random.uniform(2, 4))
                continue
            return None, f"Request failed: {str(e)}"

    return None, "All retry attempts failed."


def clean_price(text):
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except:
        return None


def parse_product(html, asin, domain):
    soup = BeautifulSoup(html, "lxml")

    # Title
    title = None
    for sel in [{"id": "productTitle"}, {"id": "title"}]:
        tag = soup.find("span", sel)
        if tag:
            title = tag.get_text(strip=True)
            break

    # Price - Method 1: structured price block
    price = None
    for block in soup.find_all("span", {"class": "a-price"}):
        if "a-text-strike" in block.get("class", []):
            continue
        off = block.find("span", {"class": "a-offscreen"})
        if off:
            p = clean_price(off.get_text())
            if p:
                price = p
                break

    # Price - Method 2: legacy IDs
    if not price:
        for pid in ["priceblock_ourprice", "priceblock_dealprice",
                    "priceblock_saleprice", "tp_price_block_total_price_ww"]:
            tag = soup.find("span", {"id": pid})
            if tag:
                p = clean_price(tag.get_text())
                if p:
                    price = p
                    break

    # Price - Method 3: whole + fraction
    if not price:
        whole = soup.find("span", {"class": "a-price-whole"})
        fraction = soup.find("span", {"class": "a-price-fraction"})
        if whole:
            raw = whole.get_text(strip=True).replace(",", "")
            frac = fraction.get_text(strip=True) if fraction else "00"
            try:
                price = float(f"{raw}{frac}") if frac else float(raw)
            except:
                pass

    # MRP
    mrp = None
    for block in soup.find_all("span", {"class": "a-price"}):
        classes = block.get("class", [])
        if "a-text-price" in classes or "basisPrice" in str(block):
            off = block.find("span", {"class": "a-offscreen"})
            if off:
                m = clean_price(off.get_text())
                if m and (not price or m > price):
                    mrp = m
                    break

    # Discount
    discount = None
    if price and mrp and mrp > price:
        discount = round(((mrp - price) / mrp) * 100, 1)
    else:
        badge = soup.find("span", {"class": "savingsPercentage"})
        if badge:
            m = re.search(r"(\d+)", badge.get_text())
            if m:
                discount = float(m.group(1))

    # Availability
    availability = "Unknown"
    avail_div = soup.find("div", {"id": "availability"})
    if avail_div:
        txt = avail_div.get_text(strip=True).lower()
        if "in stock" in txt:
            availability = "In Stock"
        elif "out of stock" in txt or "currently unavailable" in txt:
            availability = "Out of Stock"
        elif "only" in txt and "left" in txt:
            availability = "Low Stock"
        else:
            availability = avail_div.get_text(strip=True)[:50]

    # Rating
    rating = None
    rating_tag = soup.find("span", {"class": "a-icon-alt"})
    if rating_tag:
        m = re.search(r"([\d.]+)\s+out of", rating_tag.get_text())
        if m:
            rating = float(m.group(1))

    # Reviews
    reviews = None
    rev_tag = soup.find("span", {"id": "acrCustomerReviewText"})
    if rev_tag:
        reviews = rev_tag.get_text(strip=True)

    # Brand
    brand = None
    for bid in ["bylineInfo", "brand"]:
        tag = soup.find(attrs={"id": bid})
        if tag:
            brand = tag.get_text(strip=True)
            brand = re.sub(r"(Brand:|Visit the|Store)", "", brand).strip()
            break

    # Image
    image_url = None
    img = soup.find("img", {"id": "landingImage"})
    if img:
        image_url = img.get("data-old-hires") or img.get("src")
    if not image_url:
        img = soup.find("img", {"id": "imgBlkFront"})
        if img:
            image_url = img.get("src")

    # Category
    category = None
    bc = soup.find("div", {"id": "wayfinding-breadcrumbs_feature_div"})
    if bc:
        crumbs = [a.get_text(strip=True) for a in bc.find_all("a")]
        if crumbs:
            category = " > ".join(crumbs[-2:])

    # Seller
    seller = None
    for sid in ["merchant-info", "sellerProfileTriggerId"]:
        tag = soup.find(attrs={"id": sid})
        if tag:
            seller = tag.get_text(strip=True)[:80]
            break

    return {
        "asin":             asin,
        "url":              f"https://www.{domain}/dp/{asin}",
        "title":            title,
        "price":            price,
        "mrp":              mrp,
        "discount_percent": discount,
        "currency":         "INR" if domain == "amazon.in" else "USD",
        "availability":     availability,
        "rating":           rating,
        "reviews":          reviews,
        "brand":            brand,
        "category":         category,
        "image_url":        image_url,
        "seller":           seller,
        "status":           200,
    }


def scrape_amazon(asin, domain="amazon.in"):
    url = f"https://www.{domain}/dp/{asin}?th=1&psc=1"
    html, error = fetch_page(url)
    if error:
        return {"error": error, "status": 422}
    data = parse_product(html, asin, domain)
    if not data["title"] and not data["price"]:
        return {"error": "Could not extract product data. Amazon may have updated its page structure.", "status": 422}
    return data


# ── Vercel serverless handler ───────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        asin   = params.get("asin",   [""])[0].strip().upper()
        domain = params.get("domain", ["amazon.in"])[0].strip()
        allowed = ["amazon.in", "amazon.com", "amazon.co.uk", "amazon.de", "amazon.co.jp"]

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

        if not asin or not re.match(r"^[A-Z0-9]{10}$", asin):
            self.wfile.write(json.dumps({"error": "Invalid ASIN."}).encode())
            return

        if domain not in allowed:
            self.wfile.write(json.dumps({"error": "Unsupported domain."}).encode())
            return

        result = scrape_amazon(asin, domain)
        self.wfile.write(json.dumps(result).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
