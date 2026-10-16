"""
fix_images.py — Auto-fill blank image_url fields in master_sites.json

Strategy (in order):
  1. RSS feed — media:content, media:thumbnail, enclosure, <img> in description
  2. Homepage og:image meta tag (fallback for feeds with no images)

Run locally:
  python fix_images.py
  python fix_images.py --dry-run   # preview without writing
"""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

DATA_FILE = Path(__file__).parent / "data" / "master_sites.json"
TIMEOUT   = 15
HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; CraveBox/1.0)"}
MEDIA_NS  = "http://search.yahoo.com/mrss/"


def find_image_in_feed(rss_url: str, site_url: str) -> str | None:
    """Fetch RSS and return the first usable image URL found."""
    try:
        r = requests.get(rss_url, timeout=TIMEOUT, headers={**HEADERS, "Referer": site_url})
        r.raise_for_status()
    except Exception as e:
        print(f"  RSS fetch failed: {e}")
        return None

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        print(f"  XML parse error: {e}")
        return None

    for el in root.iter(f"{{{MEDIA_NS}}}content"):
        url = el.get("url", "")
        if url and _looks_like_image(url):
            return url

    for el in root.iter(f"{{{MEDIA_NS}}}thumbnail"):
        url = el.get("url", "")
        if url and _looks_like_image(url):
            return url

    for el in root.iter("enclosure"):
        url = el.get("url", "")
        t   = el.get("type", "")
        if url and ("image" in t or _looks_like_image(url)):
            return url

    for item in root.iter("item"):
        for tag in ("description", "{http://purl.org/rss/1.0/modules/content/}encoded"):
            text = item.findtext(tag, "")
            if text:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text, re.IGNORECASE)
                if m and _looks_like_image(m.group(1)):
                    return m.group(1)
        break

    return None


def find_og_image(site_url: str) -> str | None:
    """Fetch homepage and extract og:image meta tag."""
    try:
        r = requests.get(site_url, timeout=TIMEOUT, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"  Homepage fetch failed: {e}")
        return None

    m = re.search(
        r'<meta[^>]+(?:property=["\']og:image["\']|name=["\']og:image["\'])[^>]+content=["\']([^"\']+)["\']',
        r.text, re.IGNORECASE
    )
    if not m:
        # also try content= before property=
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property=["\']og:image["\'])',
            r.text, re.IGNORECASE
        )
    return m.group(1) if m else None


def _looks_like_image(url: str) -> bool:
    return bool(re.search(r'\.(jpg|jpeg|png|webp|gif)(\?|$)', url, re.IGNORECASE))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    args = parser.parse_args()

    with open(DATA_FILE) as f:
        sites = json.load(f)

    blank = [s for s in sites if not s.get("image_url", "").strip()]
    print(f"Found {len(blank)} sites with blank image_url\n")

    changed = 0
    for site in blank:
        name     = site["name"]
        rss_url  = site.get("rss_url", "")
        site_url = site.get("url", "")
        img      = None

        # 1. Try RSS
        if rss_url:
            print(f"[{name}] RSS: {rss_url}")
            img = find_image_in_feed(rss_url, site_url)
            if img:
                print(f"  ✓ RSS → {img[:90]}")

        # 2. Fall back to og:image
        if not img and site_url:
            print(f"[{name}] og:image: {site_url}")
            img = find_og_image(site_url)
            if img:
                print(f"  ✓ og:image → {img[:90]}")

        if img:
            site["image_url"] = img
            changed += 1
        else:
            print(f"  ✗ no image found — needs manual fix")

    print(f"\n{changed} of {len(blank)} filled.")

    if changed and not args.dry_run:
        with open(DATA_FILE, "w") as f:
            json.dump(sites, f, indent=2, ensure_ascii=False)
        print(f"Saved → {DATA_FILE}")
    elif args.dry_run:
        print("Dry run — nothing written.")


if __name__ == "__main__":
    main()
