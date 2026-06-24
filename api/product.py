from http.server import BaseHTTPRequestHandler
import requests
from bs4 import BeautifulSoup
import json
import re
import time
import random
from urllib.parse import urlparse, parse_qs


HEADERS_LIST = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    },
    {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    },
]


def get_headers():
    return random.choice(HEADERS_LIST)


def clean_price(price_str):
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d.]", "", price_str.replace(",", ""))
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except:
        return None


def scrape_amazon(asin, domain="amazon.in"):
    url = f"https://www.{domain}/dp/{asin}"
    session = requests.Session()

    try:
        time.sleep(random.uniform(0.3, 1.0))
        resp = session.get(url, headers=get_headers(), timeout=20)

        if resp.status_code == 503:
            return {"error": "Amazon blocked the request (503). Try again in a moment.", "status": 503}
        if resp.status_code == 404:
            return {"error": "Product not found. Check the ASIN.", "status": 404}
        if resp.status_code != 200:
            return {"error": f"Unexpected response: {resp.status_code}", "status": resp.status_code}

        soup = BeautifulSoup(resp.content, "html.parser")

        # Title
        title = None
        title_tag = soup.find("span", {"id": "productTitle"})
        if title_tag:
            title = title_tag.get_text(strip=True)

        # Price
        price = None
        price_selectors = [
            {"class": "a-price-whole"},
            {"id": "priceblock_ourprice"},
            {"id": "priceblock_dealprice"},
            {"id": "priceblock_saleprice"},
            {"class": "a-offscreen"},
        ]
        for sel in price_selectors:
            tag = soup.find("span", sel)
            if tag:
                p = clean_price(tag.get_text(strip=True))
                if p:
                    price = p
                    break

        if not price:
            price_block = soup.find("span", {"class": "a-price"})
            if price_block:
                offscreen = price_block.find("span", {"class": "a-offscreen"})
                if offscreen:
                    price = clean_price(offscreen.get_text())

        # MRP
        mrp = None
        mrp_tag = soup.find("span", {"class": "a-price a-text-price"})
        if mrp_tag:
            mrp_off = mrp_tag.find("span", {"class": "a-offscreen"})
            if mrp_off:
                mrp = clean_price(mrp_off.get_text())

        # Discount
        discount = None
        if price and mrp and mrp > price:
            discount = round(((mrp - price) / mrp) * 100, 1)

        # Rating
        rating = None
        rating_tag = soup.find("span", {"class": "a-icon-alt"})
        if rating_tag:
            m = re.search(r"(\d+\.?\d*)", rating_tag.get_text())
            if m:
                rating = float(m.group(1))

        # Reviews
        reviews = None
        review_tag = soup.find("span", {"id": "acrCustomerReviewText"})
        if review_tag:
            reviews = review_tag.get_text(strip=True)

        # Availability
        availability = "Unknown"
        avail_tag = soup.find("div", {"id": "availability"})
        if avail_tag:
            avail_text = avail_tag.get_text(strip=True).lower()
            if "in stock" in avail_text:
                availability = "In Stock"
            elif "out of stock" in avail_text or "currently unavailable" in avail_text:
                availability = "Out of Stock"
            elif "only" in avail_text and "left" in avail_text:
                availability = "Low Stock"
            else:
                availability = avail_tag.get_text(strip=True)[:40]

        # Brand
        brand = None
        brand_tag = soup.find("a", {"id": "bylineInfo"})
        if brand_tag:
            brand = brand_tag.get_text(strip=True).replace("Brand: ", "").replace("Visit the ", "").replace(" Store", "")

        # Image
        image_url = None
        img_tag = soup.find("img", {"id": "landingImage"})
        if img_tag:
            image_url = img_tag.get("src") or img_tag.get("data-old-hires")

        # Category
        category = None
        breadcrumb = soup.find("div", {"id": "wayfinding-breadcrumbs_feature_div"})
        if breadcrumb:
            crumbs = [a.get_text(strip=True) for a in breadcrumb.find_all("a")]
            if crumbs:
                category = " > ".join(crumbs[-2:]) if len(crumbs) >= 2 else crumbs[-1]

        # Seller
        seller = None
        seller_tag = soup.find("div", {"id": "merchant-info"})
        if seller_tag:
            seller = seller_tag.get_text(strip=True)[:80]

        if not title and not price:
            return {
                "error": "Could not extract product data. Amazon may have blocked the request or the ASIN is invalid.",
                "status": 422
            }

        return {
            "asin": asin,
            "url": url,
            "title": title,
            "price": price,
            "mrp": mrp,
            "discount_percent": discount,
            "currency": "INR",
            "availability": availability,
            "rating": rating,
            "reviews": reviews,
            "brand": brand,
            "category": category,
            "image_url": image_url,
            "seller": seller,
            "status": 200
        }

    except requests.exceptions.Timeout:
        return {"error": "Request timed out. Try again.", "status": 408}
    except requests.exceptions.ConnectionError:
        return {"error": "Connection failed.", "status": 503}
    except Exception as e:
        return {"error": f"Scraper error: {str(e)}", "status": 500}


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # CORS headers
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

        asin = params.get("asin", [""])[0].strip().upper()
        domain = params.get("domain", ["amazon.in"])[0].strip()

        allowed_domains = ["amazon.in", "amazon.com", "amazon.co.uk", "amazon.de", "amazon.co.jp"]

        if not asin or not re.match(r"^[A-Z0-9]{10}$", asin):
            self.wfile.write(json.dumps({"error": "Invalid ASIN. Must be 10 alphanumeric characters."}).encode())
            return

        if domain not in allowed_domains:
            self.wfile.write(json.dumps({"error": "Unsupported domain."}).encode())
            return

        data = scrape_amazon(asin, domain)
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
