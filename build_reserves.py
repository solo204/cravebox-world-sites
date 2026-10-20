"""
build_reserves.py — Build a global reserve pool for CraveBox rotation.

Generates data/reserves.json with:
  - site_reserves:   up to 20 validated sites from browse_cuisines
  - recipe_reserves: up to 50 validated recipes not already in schedule.json

Run this once after finishing schedule.json, and re-run periodically to refresh.

Usage:
  python build_reserves.py
  python build_reserves.py --sites 20 --recipes 50
  python build_reserves.py --dry-run

Requires: pip install requests
"""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_DIR    = Path(__file__).parent / "data"
SCHED_PATH  = DATA_DIR / "schedule.json"
MASTER_PATH = DATA_DIR / "browse_cuisines.json"
OUT_PATH    = DATA_DIR / "reserves.json"
HEADERS     = {"User-Agent": "CraveBox-Planner/1.0"}
TIMEOUT     = 8
RSS_ITEMS   = 25


def image_loads(url, referer=""):
    if not url or not url.startswith("http"):
        return False
    hdrs = {**HEADERS}
    if referer:
        hdrs["Referer"] = referer
    try:
        r = requests.head(url, timeout=TIMEOUT, headers=hdrs, allow_redirects=True)
        if r.status_code == 405:
            r = requests.get(url, timeout=TIMEOUT, headers=hdrs, stream=True)
            r.close()
        return r.status_code == 200 and "image" in r.headers.get("content-type", "")
    except Exception:
        return False


def fetch_rss_items(site):
    rss_url = site.get("rss_url", site["url"].rstrip("/") + "/feed/")
    results = []
    try:
        r = requests.get(rss_url, timeout=TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        root  = ET.fromstring(r.content)
        ns    = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for item in items[:RSS_ITEMS]:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or item.findtext("guid") or "").strip()
            if not title or not link:
                continue
            image_url = ""
            mc = item.find("media:content", {"media": "http://search.yahoo.com/mrss/"})
            if mc is not None:
                image_url = mc.get("url", "")
            if not image_url:
                enc = item.find("enclosure")
                if enc is not None and "image" in (enc.get("type") or ""):
                    image_url = enc.get("url", "")
            if not image_url:
                desc = (item.findtext("description") or
                        item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc)
                if m:
                    image_url = m.group(1)
            results.append({
                "title":     title,
                "url":       link,
                "image_url": image_url,
                "site_name": site.get("name", ""),
                "site_url":  site.get("url", ""),
            })
    except Exception as e:
        print(f"  RSS error [{site.get('name')}]: {e}")
    return results


def collect_used_urls(schedule):
    used = set()
    for entry in schedule:
        for recipe in (entry.get("recipes") or {}).values():
            if recipe:
                used.add(recipe.get("url") or recipe.get("recipe_url") or "")
        for r in entry.get("candidates", []):
            used.add(r.get("url") or r.get("recipe_url") or "")
        for r in entry.get("recipe_reserves", []):
            used.add(r.get("url") or r.get("recipe_url") or "")
    used.discard("")
    return used


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sites",   type=int, default=20)
    parser.add_argument("--recipes", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(SCHED_PATH, encoding="utf-8") as f:
        schedule = json.load(f)
    with open(MASTER_PATH, encoding="utf-8") as f:
        master = json.load(f)

    used_urls = collect_used_urls(schedule)
    print(f"Used URLs in schedule: {len(used_urls)}")

    # ── Site reserves ──────────────────────────────────────────────────────────
    print(f"\nBuilding site reserves (target: {args.sites})…")
    site_reserves = []
    for site in master:
        img = site.get("image_url", "")
        if img and image_loads(img, referer=site.get("url", "")):
            site_reserves.append(site)
            print(f"  ✓ {site['name']}")
        else:
            print(f"  ✗ {site['name']} — image not loading")
        if len(site_reserves) >= args.sites:
            break
    print(f"Site reserves: {len(site_reserves)}/{args.sites}")

    # ── Recipe reserves ────────────────────────────────────────────────────────
    print(f"\nBuilding recipe reserves (target: {args.recipes})…")
    recipe_reserves = []

    for site in master:
        if len(recipe_reserves) >= args.recipes:
            break
        print(f"  Fetching RSS: {site['name']}…")
        items = fetch_rss_items(site)
        for item in items:
            url = item.get("url", "")
            if not url or url in used_urls:
                continue
            if image_loads(item.get("image_url", ""), referer=item.get("site_url", "")):
                recipe_reserves.append(item)
                used_urls.add(url)
                print(f"    ✓ {item['title'][:60]}")
            if len(recipe_reserves) >= args.recipes:
                break

    print(f"Recipe reserves: {len(recipe_reserves)}/{args.recipes}")

    # ── Write ──────────────────────────────────────────────────────────────────
    output = {
        "generated_at":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site_reserves":   site_reserves,
        "recipe_reserves": recipe_reserves,
    }

    if args.dry_run:
        print(f"\nDRY RUN — would write {len(site_reserves)} sites + {len(recipe_reserves)} recipes to {OUT_PATH}")
    else:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\nWritten → {OUT_PATH}")


if __name__ == "__main__":
    main()
