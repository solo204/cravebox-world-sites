"""
plan.py — CraveBox 4-week content planner

Generates a rolling 4-week schedule from master_sites.json:
  - Assigns 4 validated sites per week (image confirmed, skips broken)
  - Auto-picks 7 validated recipes (Mon–Sun), each with a loadable image
  - Fetches 20 more validated candidates as a replacement bench
  - Writes data/schedule.json

Usage:
  python plan.py                  # build / refresh all 4 weeks
  python plan.py --weeks 6        # plan 6 weeks ahead
  python plan.py --week 2         # regenerate only week 2 in-place
  python plan.py --dry-run        # print plan without writing

Requires: pip install requests
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
try:
    from rss_scraper import scrape_recipe_listing as _scrape
except ImportError:
    _scrape = None

# ── Config ─────────────────────────────────────────────────────────────────────

DATA_DIR             = Path(__file__).parent / "data"
MASTER_PATH          = DATA_DIR / "master_sites.json"
SCHED_PATH           = DATA_DIR / "schedule.json"
HEADERS              = {"User-Agent": "CraveBox-Planner/1.0"}
TIMEOUT              = 8
RECIPES_PER_WEEK     = 7
CANDIDATES_PER_WEEK  = 20
TOTAL_NEEDED         = RECIPES_PER_WEEK + CANDIDATES_PER_WEEK   # 27
DAYS                 = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
RSS_ITEMS_PER_SITE   = 12   # how many RSS items to pull per site (need ~27 total)

# ── I/O ────────────────────────────────────────────────────────────────────────

def load_master():
    with open(MASTER_PATH, encoding="utf-8") as f:
        return json.load(f)

def load_schedule():
    if not SCHED_PATH.exists():
        return []
    with open(SCHED_PATH, encoding="utf-8") as f:
        return json.load(f)

def load_history():
    """Load used_history.json (local only, never pushed to GitHub)."""
    path = DATA_DIR / "used_history.json"
    if not path.exists():
        return {"recipe_urls": [], "site_names": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def save_schedule(sched):
    DATA_DIR.mkdir(exist_ok=True)
    with open(SCHED_PATH, "w", encoding="utf-8") as f:
        json.dump(sched, f, indent=2, ensure_ascii=False)

# ── Date helpers ───────────────────────────────────────────────────────────────

def week_mondays(n=4):
    """Return the next n Monday dates (UTC), starting from this week's Monday."""
    today  = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(weeks=i) for i in range(n)]

def pool_for_week(week_start, num_pools):
    """Deterministic pool index for a given week (by ISO week number)."""
    iso_week = week_start.isocalendar()[1]
    return iso_week % num_pools

# ── Image validation ───────────────────────────────────────────────────────────

def image_loads(url, referer=""):
    """HEAD-check an image URL. Falls back to streaming GET on 405."""
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

def fetch_og_image(page_url):
    """Scrape og:image (or first large img with dimension hint) from a page."""
    try:
        r = requests.get(page_url, timeout=TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text) or
             re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text))
        if m:
            return m.group(1).strip()
        SKIP = ('icon', 'logo', 'avatar', 'gravatar', 'badge',
                'favicon', 'star', 'rating', 'data:')
        for m2 in re.finditer(r'(?:data-src|data-lazy-src|src)=["\']([^"\']+)["\']', r.text):
            img = m2.group(1)
            if not img.startswith('http'):
                continue
            if any(s in img.lower() for s in SKIP):
                continue
            if re.search(r'-\d{3,4}x\d{3,4}', img):
                return img
    except Exception:
        pass
    return None

# ── Site validation ────────────────────────────────────────────────────────────

def validate_site(site):
    """
    Confirm a site has a working image.
    If image_url is missing or broken, tries to fetch one from RSS.
    Returns updated site dict on success, None if no image can be found.
    """
    img = site.get("image_url", "")
    if img and image_loads(img, referer=site["url"]):
        print(f"    ✓ {site['name']}")
        return site

    print(f"    ⚠ {site['name']} — image missing/broken, searching RSS…")
    rss_url = site.get("rss_url", site["url"].rstrip("/") + "/feed/")
    try:
        r = requests.get(rss_url, timeout=TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        root  = ET.fromstring(r.content)
        items = root.findall(".//item")
        for item in items[:5]:
            # media:content
            mc = item.find("media:content", {"media": "http://search.yahoo.com/mrss/"})
            if mc is not None and mc.get("url"):
                candidate = mc.get("url")
                if image_loads(candidate, referer=site["url"]):
                    print(f"      → image found via media:content")
                    return {**site, "image_url": candidate}
            # <img> in description
            desc = item.findtext("description") or ""
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc)
            if m and image_loads(m.group(1), referer=site["url"]):
                print(f"      → image found via description img")
                return {**site, "image_url": m.group(1)}
            # og:image from article page
            link = item.findtext("link") or ""
            if link.startswith("http"):
                og = fetch_og_image(link)
                if og and image_loads(og, referer=site["url"]):
                    print(f"      → image found via og:image scrape")
                    return {**site, "image_url": og}
    except Exception as e:
        print(f"      RSS error: {e}")

    print(f"    ✗ {site['name']} — no working image found, skipping")
    return None

# ── RSS fetching ───────────────────────────────────────────────────────────────

def fetch_rss_items(site, max_items=RSS_ITEMS_PER_SITE):
    """Fetch raw RSS items from a site. Falls back to scraper for sites without RSS."""
    if not site.get("rss_url") and site.get("scrape_url") and _scrape:
        print(f"  [scraper] {site['name']} has no RSS — using page scraper")
        return _scrape(site, max_items=max_items)
    rss_url = site.get("rss_url", site["url"].rstrip("/") + "/feed/")
    results = []
    try:
        r = requests.get(rss_url, timeout=TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        root  = ET.fromstring(r.content)
        ns    = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for item in items[:max_items]:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or item.findtext("guid") or "").strip()
            pub   = (item.findtext("pubDate") or
                     item.findtext("published") or
                     item.findtext("updated") or "").strip()
            if not title or not link:
                continue
            # Image: media:content → enclosure → description img
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
                "title":        title,
                "url":          link,
                "recipe_url":   link,
                "image_url":    image_url,
                "site_name":    site["name"],
                "site_url":     site["url"],
                "cuisine":      site.get("cuisine", ""),
                "published_at": pub,
            })
    except Exception as e:
        print(f"    RSS error ({site['name']}): {e}")
    return results

# ── Recipe validation ──────────────────────────────────────────────────────────

def is_recipe_page(url, site_url):
    """True if url looks like a recipe post (has a path beyond /)."""
    try:
        path = urlparse(url).path.rstrip("/")
        return len(path) > 1
    except Exception:
        return False

def validate_recipe(recipe):
    """
    Validate a recipe candidate in-place:
    - Non-empty title + URL
    - URL is a recipe page (not homepage)
    - Image loads (tries og:image scrape as fallback)
    Returns True if valid. Modifies recipe["image_url"] if scrape finds a better one.
    """
    if not recipe.get("title") or not recipe.get("url"):
        return False
    if not is_recipe_page(recipe["url"], recipe.get("site_url", "")):
        return False
    img = recipe.get("image_url", "")
    if img and image_loads(img, referer=recipe.get("site_url", "")):
        return True
    # Try og:image scrape as fallback
    og = fetch_og_image(recipe["url"])
    if og and image_loads(og, referer=recipe.get("site_url", "")):
        recipe["image_url"] = og
        return True
    return False

# ── Week builder ───────────────────────────────────────────────────────────────

def build_week(master, week_start, pool_idx, week_num, used_urls=None, used_sites=None):
    """
    Build one week's schedule:
    - Select 4 sites with validated images (starting from pool, wrapping if needed)
    - Fetch RSS from those sites, validate recipes
    - Auto-pick 7 (Mon–Sun) + 20 candidates
    """
    used_urls  = set(used_urls  or [])
    used_sites = set(used_sites or [])
    num_pools  = len(master) // 4
    print(f"\n{'='*60}")
    print(f"  Week {week_num}  |  {week_start}  |  Pool {pool_idx + 1}/{num_pools}")
    if used_sites:
        print(f"  Skipping {len(used_sites)} previously aired sites")
    if used_urls:
        print(f"  Skipping {len(used_urls)} previously aired recipes")
    print(f"{'='*60}")

    # ── 1. Select 4 sites with valid images ───────────────────────────────
    print("\n[Sites]")
    pool_start   = pool_idx * 4
    site_pool    = [master[(pool_start + i) % len(master)] for i in range(len(master))]
    valid_sites  = []
    for site in site_pool:
        if len(valid_sites) >= 4:
            break
        if site.get("name") in used_sites:
            print(f"    ↷ {site['name']} — skipped (aired before)")
            continue
        result = validate_site(site)
        if result:
            valid_sites.append(result)

    if len(valid_sites) < 4:
        print(f"  WARNING: only {len(valid_sites)} valid sites found for this week")

    # ── 2. Fetch RSS from all valid sites ─────────────────────────────────
    print(f"\n[Recipes] fetching RSS from {len(valid_sites)} sites…")
    site_buckets = []
    for site in valid_sites:
        items = fetch_rss_items(site)
        site_buckets.append(items)
        print(f"  {site['name']}: {len(items)} items")

    # Interleave (round-robin across sites) for variety
    interleaved = []
    max_len = max((len(b) for b in site_buckets), default=0)
    for i in range(max_len):
        for bucket in site_buckets:
            if i < len(bucket):
                interleaved.append(bucket[i])

    # ── 3. Validate recipes ───────────────────────────────────────────────
    print(f"\n[Validation] checking {len(interleaved)} candidates…")
    validated  = []
    seen_urls  = set()
    skipped    = 0
    for recipe in interleaved:
        if len(validated) >= TOTAL_NEEDED:
            break
        url = recipe.get("url", "")
        if url in seen_urls or url in used_urls:
            continue
        seen_urls.add(url)
        if validate_recipe(recipe):
            validated.append(recipe)
            print(f"  ✓ [{recipe['site_name']}] {recipe['title'][:55]}")
        else:
            skipped += 1

    print(f"\n  Result: {len(validated)} valid  ·  {skipped} skipped")

    # ── 4. Split into assigned (7) + candidates (20) ─────────────────────
    assigned   = validated[:RECIPES_PER_WEEK]
    candidates = validated[RECIPES_PER_WEEK:RECIPES_PER_WEEK + CANDIDATES_PER_WEEK]

    recipes = {day: (assigned[i] if i < len(assigned) else None)
               for i, day in enumerate(DAYS)}

    if len(assigned) < RECIPES_PER_WEEK:
        print(f"  WARNING: only {len(assigned)}/{RECIPES_PER_WEEK} recipes assigned "
              f"(not enough valid RSS items)")

    return {
        "week":         week_num,
        "starts":       week_start.isoformat(),
        "pool":         pool_idx + 1,
        "sites":        valid_sites,
        "recipes":      recipes,
        "candidates":   candidates,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate CraveBox weekly content schedule"
    )
    parser.add_argument("--weeks",   type=int, default=4,
                        help="Weeks to plan ahead (default 4)")
    parser.add_argument("--week",    type=int, default=None,
                        help="Regenerate only week N in place (1-based)")
    parser.add_argument("--idx",     type=int, default=None,
                        help="Regenerate schedule entry at 0-based index")
    parser.add_argument("--starts",  type=str, default=None,
                        help="Start date for --idx regen (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without writing schedule.json")
    args = parser.parse_args()

    master    = load_master()
    history   = load_history()
    used_urls  = set(history.get("recipe_urls", []))
    used_sites = set(history.get("site_names",  []))

    # Also collect URLs/sites already queued in existing schedule weeks
    existing_sched = load_schedule()
    for week in existing_sched:
        if not week:
            continue
        for site in week.get("sites", []):
            used_sites.add(site.get("name", ""))
        for recipe in week.get("recipes", {}).values():
            if recipe:
                used_urls.add(recipe.get("url", ""))
        for c in week.get("candidates", []):
            used_urls.add(c.get("url", ""))

    num_pools = len(master) // 4
    if num_pools == 0:
        print("ERROR: Need at least 4 sites in master_sites.json")
        sys.exit(1)

    print(f"Master list: {len(master)} sites - {num_pools} pools")
    mondays = week_mondays(args.weeks)

    if args.idx is not None:
        # ── Regenerate by schedule index + starts date (from manager) ────────
        idx        = args.idx
        try:
            week_start = datetime.strptime(args.starts, "%Y-%m-%d").date() if args.starts else datetime.now(timezone.utc).date()
        except Exception:
            week_start = datetime.now(timezone.utc).date()
        week_num   = idx + 1
        pool_idx   = pool_for_week(week_start, num_pools)
        entry      = build_week(master, week_start, pool_idx, week_num, used_urls, used_sites)
        if not args.dry_run:
            sched = load_schedule()
            while len(sched) <= idx:
                sched.append(None)
            sched[idx] = entry
            save_schedule(sched)
            print(f"\nOK Week {week_num} regenerated")
        else:
            print(f"\n[dry-run] Week {week_num} not written.")

    elif args.week is not None:
        # ── Regenerate a single week in the existing schedule ─────────────
        idx = args.week - 1
        if idx < 0:
            print(f"ERROR: --week must be >= 1")
            sys.exit(1)
        # Extend mondays list if regenerating beyond default range
        while len(mondays) <= idx:
            mondays.append(mondays[-1] + timedelta(weeks=1))
        week_start = mondays[idx]
        pool_idx   = pool_for_week(week_start, num_pools)
        entry      = build_week(master, week_start, pool_idx, args.week, used_urls, used_sites)
        if not args.dry_run:
            sched = load_schedule()
            while len(sched) <= idx:
                sched.append(None)
            sched[idx] = entry
            save_schedule(sched)
            print(f"\n✓ Week {args.week} regenerated → {SCHED_PATH}")
        else:
            print(f"\n[dry-run] Week {args.week} not written.")
    else:
        # ── Build all weeks ───────────────────────────────────────────────
        sched = []
        for i, week_start in enumerate(mondays):
            pool_idx = pool_for_week(week_start, num_pools)
            entry    = build_week(master, week_start, pool_idx, i + 1, used_urls, used_sites)
            sched.append(entry)

        if not args.dry_run:
            save_schedule(sched)
            print(f"\n✓ {len(sched)}-week schedule written → {SCHED_PATH}")
        else:
            print(f"\n[dry-run] {len(sched)} weeks planned, not written.")

    print(f"Done  ·  {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")


if __name__ == "__main__":
    main()
