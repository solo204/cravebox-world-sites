"""
rotate.py — CraveBox World Sites content rotator

Runs every 2 hours via GitHub Actions.
- Picks 4 featured sites for the current time slot (deterministic — all users see same sites)
- Fetches the latest recipe from each featured site's RSS feed
- Picks the freshest one as the featured recipe of the day
- Writes data/featured.json and exits

Slot logic:  slot = (UTC hour // 2) % num_pools
             num_pools = len(master_sites) // 4

To switch to weekly rotation: change SLOT_HOURS = 2 → 168
"""

import json
import os
import sys
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

SLOT_HOURS   = 2          # hours per slot (2 for testing, 168 for weekly)
SITES_PER_SLOT = 4        # how many sites to feature per slot
REQUEST_TIMEOUT = 8       # seconds per HTTP request
DATA_DIR     = Path(__file__).parent / "data"

HEADERS = {
    "User-Agent": "CraveBox-Rotator/1.0 (https://github.com/solo204/cravebox-world-sites)"
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_master() -> list[dict]:
    with open(DATA_DIR / "master_sites.json", encoding="utf-8") as f:
        return json.load(f)


def current_slot(num_pools: int) -> int:
    now = datetime.now(timezone.utc)
    total_hours = now.day * 24 + now.hour
    return (total_hours // SLOT_HOURS) % num_pools


def pick_sites(master: list[dict]) -> list[dict]:
    num_pools = len(master) // SITES_PER_SLOT
    if num_pools == 0:
        return master[:SITES_PER_SLOT]
    slot = current_slot(num_pools)
    start = slot * SITES_PER_SLOT
    return master[start : start + SITES_PER_SLOT]


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def extract_image_from_content(content: str) -> str | None:
    """Pull the first <img src=...> from RSS item content."""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content or "")
    return m.group(1) if m else None


def fetch_latest_recipe(site: dict) -> dict | None:
    """
    Try to get the most recent recipe from a site's RSS feed.
    Returns a dict or None on failure.
    """
    rss_url = site.get("rss_url", site["url"].rstrip("/") + "/feed/")
    try:
        r = requests.get(rss_url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"  RSS fetch failed for {site['name']}: {e}")
        return None

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        print(f"  RSS parse error for {site['name']}: {e}")
        return None

    # Handle both RSS <item> and Atom <entry>
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") or root.findall(".//atom:entry", ns)
    if not items:
        print(f"  No RSS items found for {site['name']}")
        return None

    item = items[0]  # most recent

    def txt(tag: str) -> str:
        el = item.find(tag)
        return (el.text or "").strip() if el is not None else ""

    title    = strip_html(txt("title"))
    link     = txt("link") or txt("guid")
    pub_date = txt("pubDate") or txt("published") or txt("updated")

    # Image: try media:content, enclosure, then scrape from description/content
    image_url = None
    media_content = item.find("media:content", {"media": "http://search.yahoo.com/mrss/"})
    if media_content is not None:
        image_url = media_content.get("url")
    if not image_url:
        enclosure = item.find("enclosure")
        if enclosure is not None and "image" in (enclosure.get("type") or ""):
            image_url = enclosure.get("url")
    if not image_url:
        description = txt("description") or txt("{http://purl.org/rss/1.0/modules/content/}encoded")
        image_url = extract_image_from_content(description)
    if not image_url:
        # Fall back to og:image from the article page
        image_url = fetch_og_image(link) or site.get("image_url", "")

    if not title or not link:
        return None

    print(f"  ✓ {site['name']}: {title[:60]}")
    return {
        "title":        title,
        "site_name":    site["name"],
        "site_url":     site["url"],
        "cuisine":      site["cuisine"],
        "recipe_url":   link,
        "image_url":    image_url or "",
        "published_at": pub_date,
    }


def fetch_og_image(url: str) -> str | None:
    """
    Fetch a page and return the best image URL for use as a site card.
    Strategy:
      1. og:image meta tag (reliable, explicit share image)
      2. First <img src> in the page body with dimension hints (e.g. -1200x800)
         skipping avatars, icons, and tiny thumbnails
    Returns None on any failure.
    """
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        # 1. og:image meta tag
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text) or
             re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text))
        if m:
            return m.group(1)
        # 2. First large image from page body.
        #    Some sites (e.g. JOC with SPAI) lazy-load images: the <img src>
        #    is a data:svg placeholder and the real URL is in data-src.
        #    We check both attributes, prefer URLs with large-dimension hints,
        #    and skip avatars/icons/thumbnails.
        SKIP = ('icon', 'logo', 'avatar', 'gravatar', 'badge', 'favicon',
                'star', 'rating', 'w480', 'w200', 'w100', '32x32', '64x64',
                '250x250', '277x416', 'data:')
        candidates = []
        for m in re.finditer(r'(?:data-src|data-lazy-src|src)=["\']([^"\']+)["\']', r.text):
            img_url = m.group(1)
            if not img_url.startswith('http'):
                continue
            lower = img_url.lower()
            if any(s in lower for s in SKIP):
                continue
            if re.search(r'-\d{3,4}x\d{3,4}', img_url):
                return img_url   # found a large-dimension image immediately
            candidates.append(img_url)
        # no dimension-hinted image — return first non-skipped candidate
        return candidates[0] if candidates else None
    except Exception:
        return None


def proxy_image(url: str) -> str:
    """Wrap an image URL through wsrv.nl to bypass hotlink protection.

    Skip CDN URLs — their content is already publicly accessible and CDNs like
    SPAI block wsrv.nl, so proxying them makes things worse.
    """
    if not url:
        return url
    # Don't proxy CDN/image-optimizer URLs — they're public and may block wsrv.nl
    skip_proxy_hints = ('cdn.', '/spai/', 'cloudfront', 'fastly', 'imgix', 'cloudinary', 'wsrv.nl')
    if any(h in url for h in skip_proxy_hints):
        return url
    return f"https://wsrv.nl/?url={urllib.parse.quote(url, safe='')}&w=800&h=600&fit=cover&output=jpg"


def fetch_site_image(site: dict) -> str:
    """
    Fetch the freshest image for a site from its RSS feed.
    Strategy (in order):
      1. media:content URL from RSS item
      2. enclosure URL from RSS item
      3. <img> in description or content:encoded
      4. og:image (or first large <img>) scraped from the article page
    Falls back to site['image_url'] if everything fails.
    """
    rss_url = site.get("rss_url", site["url"].rstrip("/") + "/feed/")
    try:
        r = requests.get(rss_url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for item in items[:5]:
            mc = item.find("media:content", {"media": "http://search.yahoo.com/mrss/"})
            if mc is not None and mc.get("url"):
                return mc.get("url")
            enc = item.find("enclosure")
            if enc is not None and "image" in (enc.get("type") or ""):
                return enc.get("url")
            desc = (item.findtext("description") or
                    item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc)
            if m:
                return m.group(1)
            # Fallback: scrape og:image (or first article img) from the page
            link = item.findtext("link") or item.findtext("guid")
            if link and link.startswith("http"):
                og = fetch_og_image(link)
                if og:
                    print(f"  page-scrape image ({site['name']}): {og[:80]}")
                    return og
    except Exception as e:
        print(f"  site image fetch failed for {site['name']}: {e}")
    return site.get("image_url", "")


def pick_featured_recipe(sites: list[dict]) -> dict | None:
    """Try each featured site in order; return the first successful RSS fetch."""
    for site in sites:
        recipe = fetch_latest_recipe(site)
        if recipe:
            return recipe
    print("  WARNING: all RSS fetches failed — no featured recipe this slot")
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[rotate] UTC: {datetime.now(timezone.utc).isoformat()}")

    master = load_master()
    print(f"[rotate] Master list: {len(master)} sites")

    featured_sites = pick_sites(master)
    print(f"[rotate] Slot sites: {[s['name'] for s in featured_sites]}")

    print("[rotate] Fetching latest recipe + site images from featured sites…")
    featured_recipe = pick_featured_recipe(featured_sites)

    # Fetch fresh site card images from RSS (falls back to master image_url)
    site_images = {s["name"]: fetch_site_image(s) for s in featured_sites}

    # Proxy the featured recipe image too
    if featured_recipe and featured_recipe.get("image_url"):
        featured_recipe["image_url"] = proxy_image(featured_recipe["image_url"])

    output = {
        "updated_at":       datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slot_hours":       SLOT_HOURS,
        "featured_sites":   [
            {
                "name":      s["name"],
                "url":       s["url"],
                "cuisine":   s["cuisine"],
                "tagline":   s["tagline"],
                "image_url": site_images.get(s["name"]) or s["image_url"],
            }
            for s in featured_sites
        ],
        "featured_recipe":  featured_recipe,
    }

    out_path = DATA_DIR / "featured.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[rotate] Written → {out_path}")


if __name__ == "__main__":
    main()
