"""
rss_scraper.py — Recipe page scraper for sites without RSS feeds

For sites with a `scrape_url` field in master_sites.json, scrapes their
recipe listing page to extract recipe links + images.

Returns items in the same dict format as plan.py's fetch_rss_items():
  { title, url, recipe_url, image_url, site_name, cuisine }

Usage:
  python rss_scraper.py https://www.seriouseats.com/recipes "Serious Eats" "American"
"""

import re
import sys
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; CraveBox/1.0)"}
TIMEOUT  = 10

# Paths that are never recipes
SKIP_PATHS = [
    "/tag/", "/category/", "/author/", "/page/", "/search/", "/topics/",
    "/about", "/contact", "/privacy", "/terms", "/shop", "/subscribe",
    "/newsletter", "/feed", "/sitemap", "/wp-", "/cdn-cgi/",
    ".jpg", ".png", ".pdf", ".gif", ".webp",
]

# Image fragments that indicate icons/logos rather than recipe photos
SKIP_IMG = ["logo", "icon", "avatar", "pixel", "1x1", "badge", "arrow",
            "spinner", "placeholder", "blank", "spacer", "ad-"]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _enrich(item):
    """Fetch og:title and og:image from an article page."""
    try:
        r = requests.get(item["url"], timeout=TIMEOUT, headers=HEADERS)
        html = r.text
        if not item["title"]:
            m = (re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html) or
                 re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html) or
                 re.search(r'<title[^>]*>([^<|]+)', html))
            if m:
                item["title"] = re.sub(r'\s*[|\-–]\s*.*$', '', m.group(1)).strip()
        if not item["image_url"]:
            m2 = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html) or
                  re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html))
            if m2:
                item["image_url"] = m2.group(1)
    except Exception:
        pass
    return item


def _is_good_img(url):
    url_lower = url.lower()
    if any(s in url_lower for s in SKIP_IMG):
        return False
    # Skip tiny images referenced by dimensions in the URL
    m = re.search(r'[-_x](\d+)x(\d+)', url_lower)
    if m and (int(m.group(1)) < 150 or int(m.group(2)) < 150):
        return False
    return True


# ── Main scraper ───────────────────────────────────────────────────────────────

def scrape_recipe_listing(site, max_items=20):
    """
    Scrape a recipe listing page and return recipe dicts.

    Strategy:
    1. Fetch the listing page HTML
    2. Find all <a href> on-domain links that look like recipe pages
    3. Pair each link with the nearest <img> in the HTML
    4. Parallel-fetch og:title + og:image for items still missing either
    5. Filter out items without a title
    """
    scrape_url = site.get("scrape_url", "")
    if not scrape_url:
        return []

    site_url     = site.get("url", "").rstrip("/")
    parsed_base  = urlparse(site_url)
    base_domain  = parsed_base.netloc.lstrip("www.")
    site_name    = site.get("name", "")
    cuisine      = site.get("cuisine", "")

    print(f"[scraper] Fetching listing page: {scrape_url}")
    try:
        r    = requests.get(scrape_url, timeout=TIMEOUT, headers=HEADERS)
        html = r.text
    except Exception as e:
        print(f"[scraper] Failed: {e}")
        return []

    # ── Collect all images with position ──────────────────────────────────────
    img_re = re.compile(
        r'<img[^>]+(?:src|data-src|data-lazy-src|data-original)=["\']([^"\']+)["\']',
        re.IGNORECASE
    )
    imgs = [(m.start(), m.group(1)) for m in img_re.finditer(html)]

    # ── Collect recipe-like links ─────────────────────────────────────────────
    link_re = re.compile(r'<a\s[^>]*href=["\']([^"\'#?][^"\']*)["\']', re.IGNORECASE)
    seen    = set()
    items   = []

    for m in link_re.finditer(html):
        if len(items) >= max_items:
            break
        href = m.group(1).strip()

        # Resolve to absolute
        if href.startswith("http"):
            abs_url = href
        elif href.startswith("/"):
            abs_url = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
        else:
            continue

        # Must be same domain
        if urlparse(abs_url).netloc.lstrip("www.") != base_domain:
            continue

        path = urlparse(abs_url).path
        if any(s in path.lower() for s in SKIP_PATHS):
            continue
        if len([p for p in path.strip("/").split("/") if p]) < 1:
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)

        # Find the nearest non-icon image within 3000 chars
        best_img  = ""
        best_dist = float("inf")
        for img_pos, img_url in imgs:
            dist = abs(img_pos - m.start())
            if dist < best_dist and dist < 3000 and _is_good_img(img_url):
                best_img  = img_url
                best_dist = dist

        items.append({
            "title":      "",
            "url":        abs_url,
            "recipe_url": abs_url,
            "image_url":  best_img,
            "site_name":  site_name,
            "cuisine":    cuisine,
        })

    if not items:
        print(f"[scraper] No recipe links found on {scrape_url}")
        return []

    needs = [it for it in items if not it["title"] or not it["image_url"]]
    print(f"[scraper] {site_name}: {len(items)} links found, enriching {len(needs)} via og:tags…")

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_enrich, needs))   # in-place mutation

    result = [it for it in items if it["title"]]
    print(f"[scraper] {site_name}: {len(result)} recipes ready")
    return result


# ── CLI test ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python rss_scraper.py <scrape_url> [site_name] [cuisine]")
        sys.exit(1)

    test_site = {
        "name":       sys.argv[2] if len(sys.argv) > 2 else "Test Site",
        "url":        "/".join(sys.argv[1].split("/")[:3]),
        "cuisine":    sys.argv[3] if len(sys.argv) > 3 else "General",
        "scrape_url": sys.argv[1],
    }
    items = scrape_recipe_listing(test_site, max_items=10)
    print(json.dumps(items, indent=2, ensure_ascii=False))
