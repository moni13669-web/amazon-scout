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


# Phrases that definitively mean unavailable — only checked inside the #availability div
_UNAVAIL_PHRASES = [
    ("temporarily out of stock",                         AVAIL_TEMP_OOS),
    ("we are working hard to be back in stock",          AVAIL_TEMP_OOS),
    ("working hard to be back in stock",                 AVAIL_TEMP_OOS),
    ("currently unavailable",                            AVAIL_CURRENTLY_UNAVAIL),
    ("we don't know when or if this item will be back",  AVAIL_CURRENTLY_UNAVAIL),
    ("we do not know when or if this item will be back", AVAIL_CURRENTLY_UNAVAIL),
    ("out of stock",                                     AVAIL_OUT_OF_STOCK),
]
# Phrases that mean IN stock — safe to match broadly
_INSTOCK_PHRASES = [
    ("in stock",    AVAIL_IN_STOCK),
    ("only",        None),   # checked with "left" separately → Low Stock
]

def _phrase_to_avail(txt):
    for phrase, status in _UNAVAIL_PHRASES:
        if phrase in txt:
            return status
    if "in stock" in txt:
        return AVAIL_IN_STOCK
    if "only" in txt and "left" in txt:
        return AVAIL_LOW_STOCK
    return None

def detect_availability(soup, price):
    """
    Determine availability using a strict-scoping strategy.

    RULE: Never run a page-wide scan for OOS phrases.
    The page contains delivery text like "Not available at your location"
    which is NOT about product availability — a page-wide scan picks this
    up and incorrectly marks in-stock products as OOS.

    Priority:
    1. #availability div — most reliable, Amazon puts canonical status here
    2. Add-to-Cart / Buy-Now button presence → definitively In Stock
    3. #outOfStock div — explicit OOS signal
    4. #merchant-info — secondary availability text
    5. Page-wide scan ONLY for IN-STOCK phrases (safe — no false positives)
       NEVER page-wide scan for OOS/unavailable phrases
    6. Price fallback → In Stock
    """
    # ── 1. #availability div (most reliable) ─────────────────────────────────
    avail_div = soup.find("div", {"id": "availability"})
    if avail_div:
        txt = avail_div.get_text(separator=" ", strip=True).lower()
        # Check unavailable phrases first (they're more specific)
        for phrase, status in _UNAVAIL_PHRASES:
            if phrase in txt:
                return status
        # Then in-stock
        if "in stock" in txt:
            return AVAIL_IN_STOCK
        if "only" in txt and "left" in txt:
            return AVAIL_LOW_STOCK

    # ── 2. Add-to-Cart / Buy-Now buttons → definitively In Stock ─────────────
    # These only exist when the item is purchasable. Check both input and button.
    atc = (
        soup.find("input", {"id": "add-to-cart-button"}) or
        soup.find("button", {"id": "add-to-cart-button"}) or
        soup.find("input", {"name": "submit.add-to-cart"})
    )
    buy = (
        soup.find("input", {"id": "buy-now-button"}) or
        soup.find("button", {"id": "buy-now-button"})
    )
    if atc or buy:
        return AVAIL_IN_STOCK

    # ── 3. #outOfStock div → explicit OOS signal ──────────────────────────────
    oos_div = soup.find("div", {"id": "outOfStock"})
    if oos_div:
        return AVAIL_OUT_OF_STOCK

    # ── 4. #merchant-info scoped check ───────────────────────────────────────
    merchant = soup.find("div", {"id": "merchant-info"})
    if merchant:
        txt = merchant.get_text(separator=" ", strip=True).lower()
        for phrase, status in _UNAVAIL_PHRASES:
            if phrase in txt:
                return status
        if "in stock" in txt:
            return AVAIL_IN_STOCK

    # ── 5. buyBox / rightCol scoped scan for availability text ───────────────
    for bid in ["buyBoxInner", "rightCol", "buyBox"]:
        box = soup.find(id=bid)
        if not box:
            continue
        txt = box.get_text(separator=" ", strip=True).lower()
        for phrase, status in _UNAVAIL_PHRASES:
            if phrase in txt:
                return status
        if "in stock" in txt:
            return AVAIL_IN_STOCK
        if "only" in txt and "left" in txt:
            return AVAIL_LOW_STOCK

    # ── 6. Page-wide scan ONLY for in-stock phrases (safe — no false positives)
    # !! NEVER scan page-wide for "not available", "out of stock" etc.
    # !! Those phrases appear in delivery info, recommendations, and other
    # !! non-buybox areas and cause false OOS detection.
    page_text = soup.get_text(separator=" ", strip=True).lower()
    if "currently unavailable" in page_text:
        return AVAIL_CURRENTLY_UNAVAIL
    if "temporarily out of stock" in page_text:
        return AVAIL_TEMP_OOS
    if "we don't know when or if this item will be back" in page_text:
        return AVAIL_CURRENTLY_UNAVAIL

    # ── 7. Price fallback ─────────────────────────────────────────────────────
    if price:
        return AVAIL_IN_STOCK

    return AVAIL_UNKNOWN


def _is_mrp_block(block):
    """
    Return True if this a-price span is the MRP/strikethrough, NOT the selling price.
    Checks multiple signals Amazon uses.
    """
    cls = " ".join(block.get("class", []))
    # Class-based signals
    if any(x in cls for x in ["a-text-strike", "basisPrice", "a-text-price"]):
        return True
    # Parent element is a strikethrough container
    parent = block.parent
    if parent:
        pcls = " ".join(parent.get("class", []))
        if any(x in pcls for x in ["a-text-strike", "basisPrice"]):
            return True
    # data-a-strike attribute
    if block.get("data-a-strike") == "true":
        return True
    # aria-label containing "M.R.P." or "was"
    aria = block.get("aria-label", "").lower()
    if any(x in aria for x in ["m.r.p", "mrp", "was ", "list price", "original"]):
        return True
    # Check if a sibling/ancestor label says M.R.P.
    try:
        for sib in block.parent.children:
            if hasattr(sib, "get_text"):
                t = sib.get_text().lower()
                if "m.r.p" in t or "was " in t:
                    return True
    except Exception:
        pass
    return False


def _price_from_block(block):
    """
    Extract numeric price from an a-price span that has already passed MRP check.
    Returns float or None.
    """
    # Most reliable: a-offscreen (accessibility text)
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
            return clean_price(f"{w}.{f}")
    return None


def _first_sale_price_in(container):
    """
    Scan a-price blocks inside `container`, skip MRP/strikethrough, return first valid price.
    """
    if container is None:
        return None
    for block in container.find_all("span", {"class": "a-price"}):
        if _is_mrp_block(block):
            continue
        p = _price_from_block(block)
        if p:
            return p
    return None


def _center_col_price(soup):
    """
    Extract price from the central product column — the '-5% ₹1,27,990 [Price history]'
    area shown in the main product description. This is always the correct selling price.
    Amazon uses `corePriceDisplay_desktop_feature_div` (or `corePrice_desktop`) for this.
    The priceToPay span inside it is the most reliable single signal.
    """
    for cid in [
        "corePriceDisplay_desktop_feature_div",
        "corePrice_desktop",
        "corePrice_feature_div",
        "corePrice_mobile_feature_div",
    ]:
        core = soup.find("div", {"id": cid})
        if not core:
            continue

        # priceToPay inside corePriceDisplay is 100% the selling price
        ttp = core.find("span", {"class": "priceToPay"})
        if ttp:
            off = ttp.find("span", {"class": "a-offscreen"})
            if off:
                p = clean_price(off.get_text())
                if p:
                    return p

        # Non-strikethrough a-price blocks in DOM order
        # (sale price is ALWAYS the first a-price; MRP comes after)
        for block in core.find_all("span", {"class": "a-price"}):
            if _is_mrp_block(block):
                continue
            p = _price_from_block(block)
            if p:
                return p

    return None


def _accordion_base_price(soup):
    """
    Handle Amazon's accordion buybox (exchange-offer pages: MacBook, phones, etc.)
    The accordion has:
      - "With Exchange"    → lower subsidized price  (NOT what we want)
      - "Without Exchange" → base selling price       (THIS is what we want)

    DOM structure inside each accordion row:
      <div class="newAccordionRow">
        <span>Without Exchange</span>
        <span class="a-price">  ← current price
          <span class="a-offscreen">₹1,27,990</span>
        </span>
        <span class="a-price a-text-strike">  ← old/MRP price (strikethrough)
          <span class="a-offscreen">₹1,34,900</span>
        </span>
      </div>
    """
    accordion = soup.find("div", {"id": "buyBoxAccordion"})
    if not accordion:
        return None

    # Find all accordion rows
    rows = accordion.find_all("div", class_=lambda c: c and "newAccordionRow" in c)
    if not rows:
        # Fallback: any direct child divs
        rows = accordion.find_all("div", recursive=False)

    without_exchange_price = None
    with_exchange_price = None

    for row in rows:
        row_text = row.get_text(separator=" ", strip=True).lower()
        is_without = any(x in row_text for x in ["without exchange", "no exchange", "buy new", "new ("])
        is_with    = "with exchange" in row_text

        # Get the FIRST non-MRP price in this row
        row_price = None
        for block in row.find_all("span", {"class": "a-price"}):
            if _is_mrp_block(block):
                continue
            p = _price_from_block(block)
            if p:
                row_price = p
                break

        if is_without and row_price:
            without_exchange_price = row_price
        elif is_with and row_price:
            with_exchange_price = row_price

    # Return "Without Exchange" price if found; never return exchange-discounted price
    if without_exchange_price:
        return without_exchange_price

    # No labeled rows found — accordion might have only one row (just "New")
    # Collect all non-MRP prices and return the highest (base price >= exchange price)
    all_prices = []
    for block in accordion.find_all("span", {"class": "a-price"}):
        if _is_mrp_block(block):
            continue
        p = _price_from_block(block)
        if p:
            all_prices.append(p)

    if all_prices:
        return max(all_prices)

    return None


def parse_price(soup):
    """
    Extract the correct selling price from an Amazon product page.

    The single most reliable source is the CENTER COLUMN price block
    (`corePriceDisplay_desktop_feature_div`) — this always shows the actual
    selling price exactly as displayed next to "Price history" link.

    For exchange-offer pages (MacBook, phones): the center column may show
    the exchange-discounted price. In that case the accordion "Without Exchange"
    row has the base price. We detect this case and use the higher price.

    Priority:
      T1  Center column priceToPay / corePriceDisplay  — most reliable
      T2  Accordion "Without Exchange" row             — MacBook/phone pages
      T3  apex_desktop                                 — alternate wrapper
      T4  rightCol / buyBoxInner scoped scan           — right buybox
      T5  #ppd scoped (noise stripped)                 — broad fallback
      T6  Legacy priceblock IDs                        — old pages
    """

    # ══ TIER 1: Center column — corePriceDisplay / priceToPay ════════════════
    center_price = _center_col_price(soup)

    # ══ TIER 2: Accordion "Without Exchange" base price ═══════════════════════
    accordion_price = _accordion_base_price(soup)

    # If both exist, take the HIGHER one
    # (exchange pages show lower price in center; accordion has the real price)
    if center_price and accordion_price:
        return max(center_price, accordion_price)
    if center_price:
        return center_price
    if accordion_price:
        return accordion_price

    # ══ TIER 3: priceToPay anywhere (non-noise contexts) ══════════════════════
    bad_ancestors = {
        "similarities-widget", "sp-atf", "sponsoredProductsCarousel",
        "all-offers-display", "olp_feature_div", "olp-padding-small",
        "tmmSwatches",
    }
    for ttp in soup.find_all("span", {"class": "priceToPay"}):
        ancestor_ids = " ".join(
            a.get("id", "") for a in ttp.parents if hasattr(a, "get")
        )
        if any(b in ancestor_ids for b in bad_ancestors):
            continue
        off = ttp.find("span", {"class": "a-offscreen"})
        if off:
            p = clean_price(off.get_text())
            if p:
                return p

    # ══ TIER 4: apex_desktop — alternative buybox wrapper ═════════════════════
    for aid in [
        "apex_desktop",
        "apex_offerDisplay_desktop_feature_div",
        "apex_desktop_newAccordionRow",
    ]:
        apex = soup.find("div", {"id": aid})
        if not apex:
            continue
        p = _first_sale_price_in(apex)
        if p:
            return p

    # ══ TIER 5: rightCol / buyBoxInner — right-side buybox ════════════════════
    for bid in ["buyBoxInner", "rightCol"]:
        box = soup.find("div", {"id": bid})
        if not box:
            continue
        p = _first_sale_price_in(box)
        if p:
            return p

    # ══ TIER 6: #ppd scoped — broad scan, noise stripped ══════════════════════
    ppd = soup.find("div", {"id": "ppd"})
    if ppd:
        noise_ids = [
            "similarities-widget", "sp-atf", "sponsoredProductsCarousel",
            "olp_feature_div", "tmmSwatches",
        ]
        ppd_copy = BeautifulSoup(str(ppd), "lxml")
        for nid in noise_ids:
            for tag in ppd_copy.find_all(id=nid):
                tag.decompose()
        p = _first_sale_price_in(ppd_copy)
        if p:
            return p

    # ══ TIER 7: legacy IDs ════════════════════════════════════════════════════
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
                return p

    return None


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

    # ── Cross-validate: extract MRP and compare ───────────────────────────────
    # If the price we found matches the MRP exactly, we grabbed the wrong span.
    # In that case, discard and try to find a lower (sale) price.
    try:
        mrp = None
        # MRP is always inside a basisPrice or a-text-strike container
        for block in soup.find_all("span", {"class": "a-price"}):
            cls = " ".join(block.get("class", []))
            if any(x in cls for x in ["basisPrice", "a-text-strike", "a-text-price"]):
                off = block.find("span", {"class": "a-offscreen"})
                if off:
                    mrp = clean_price(off.get_text())
                    if mrp:
                        break
        # If price == mrp exactly, we got the MRP — throw it away
        if price and mrp and abs(price - mrp) < 0.01:
            # Try to find any price in buybox that differs from MRP
            for cid in ["corePriceDisplay_desktop_feature_div", "buyBoxInner", "rightCol"]:
                container = soup.find(id=cid)
                if not container:
                    continue
                for block in container.find_all("span", {"class": "a-price"}):
                    cls = " ".join(block.get("class", []))
                    if any(x in cls for x in ["basisPrice", "a-text-strike", "a-text-price"]):
                        continue
                    off = block.find("span", {"class": "a-offscreen"})
                    if off:
                        candidate = clean_price(off.get_text())
                        if candidate and abs(candidate - mrp) > 0.01:
                            price = candidate
                            break
                if price and abs(price - mrp) > 0.01:
                    break
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
