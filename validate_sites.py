"""
validate_sites.py — CraveBox World Sites validator

Run this BEFORE adding a site to master_sites.json.
Checks: RSS reachability, item count, recipe link format, image availability.

Usage:
  python validate_sites.py                          # validate all sites in master_sites.json
  python validate_sites.py --site "Maangchi"        # validate one site by name
  python validate_sites.py --url https://example.com  # validate a new candidate URL
  python validate_sites.py --failing                # only print failures
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

DATA_DIR       = Path(__file__).parent / "data"
REQUEST_TIMEOUT = 12

HEADERS = {
    "User-Agent": "CraveBox-Rotator/1.0 (https://github.com/solo204/cravebox-world-sites)"
}

# ── Result helpers ─────────────────────────────────────────────────────────────

OK   = "✓"
FAIL = "✗"
WARN = "⚠"

def result(icon, msg):
    return f"{icon} {msg}"


# ── Image accessibility check ──────────────────────────────────────────────────

def check_image_loads(image_url: str, referer: str = "") -> str:
    """HEAD the image URL; fall back to streaming GET if HEAD is refused."""
    hdrs = {**HEADERS}
    if referer:
        hdrs["Referer"] = referer
    try:
        r = requests.head(image_url, timeout=REQUEST_TIMEOUT, headers=hdrs, allow_redirects=True)
        if r.status_code == 405:
            # Server doesn't allow HEAD — stream GET instead
            r = requests.get(image_url, timeout=REQUEST_TIMEOUT, headers=hdrs, stream=True)
            r.close()
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and "image" in ct:
            return result(OK, f"HTTP {r.status_code} ({ct.split(';')[0].strip()})")
        return result(FAIL, f"HTTP {r.status_code} content-type={ct!r}")
    except Exception as e:
        return result(FAIL, str(e))


# ── og:image scrape ────────────────────────────────────────────────────────────

def fetch_og_image(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text) or
             re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text))
        if m:
            return m.group(1)
        SKIP = ('icon', 'logo', 'avatar', 'gravatar', 'badge', 'favicon',
                'star', 'rating', 'w480', 'w200', 'w100', '32x32', '64x64', 'data:')
        for m2 in re.finditer(r'(?:data-src|data-lazy-src|src)=["\']([^"\']+)["\']', r.text):
            img = m2.group(1)
            if not img.startswith('http'):
                continue
            if any(s in img.lower() for s in SKIP):
                continue
            if re.search(r'-\d{3,4}x\d{3,4}', img):
                return img
        return None
    except Exception:
        return None


# ── Core validator ─────────────────────────────────────────────────────────────

def validate_site(site: dict) -> dict[str, str]:
    """
    Run all checks for one site. Returns an ordered dict of check_name → result_string.
    A result starting with FAIL means the site should not be added.
    """
    checks: dict[str, str] = {}
    name    = site.get("name", site.get("url", "?"))
    url     = site.get("url", "").rstrip("/")
    rss_url = site.get("rss_url", url + "/feed/")

    # ── 1. RSS reachable ──────────────────────────────────────────────────────
    try:
        r = requests.get(rss_url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        checks["rss_fetch"] = result(OK, f"HTTP {r.status_code}")
    except Exception as e:
        checks["rss_fetch"] = result(FAIL, str(e))
        return checks  # nothing else makes sense without the feed

    # ── 2. RSS parses + has items ─────────────────────────────────────────────
    try:
        # Try raw bytes first; fall back to re-encoded text (handles BOM / charset mismatches)
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            root = ET.fromstring(r.text.encode("utf-8"))
        ns    = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        if not items:
            checks["rss_items"] = result(FAIL, "no <item> elements found")
            return checks
        checks["rss_items"] = result(OK, f"{len(items)} items")
    except ET.ParseError as e:
        checks["rss_items"] = result(FAIL, f"XML parse error: {e}")
        return checks

    item = items[0]

    # ── 3. Recipe link is a post URL (not the site homepage) ─────────────────
    # Atom entries use <link href="..."/> (attribute), RSS uses <link> text content
    link_el = item.find("link")
    if link_el is not None and not (link_el.text or "").strip() and link_el.get("href"):
        link = link_el.get("href", "").strip()
    else:
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
    if not link:
        checks["recipe_link"] = result(FAIL, "no <link> or <guid> in first item")
    elif link.rstrip("/") == url:
        checks["recipe_link"] = result(FAIL, f"points to homepage: {link}")
    elif not link.startswith("http"):
        checks["recipe_link"] = result(FAIL, f"relative URL: {link}")
    else:
        checks["recipe_link"] = result(OK, link[:90])

    # ── 4. Image from RSS ────────────────────────────────────────────────────
    image_url: str | None = None

    # Patterns that look like images in media:content but are actually video embeds
    VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com")

    mc = item.find("media:content", {"media": "http://search.yahoo.com/mrss/"})
    if mc is not None and mc.get("url"):
        candidate = mc.get("url")
        # Fix double-domain bug: https://foo.com/foo.com/path  OR  https://foo.com/https://foo.com/path
        candidate = re.sub(r'^(https?://([^/]+))/(https?://\2/|\2/)', r'\1/', candidate)
        if not any(v in candidate for v in VIDEO_HOSTS):
            image_url = candidate
            checks["image_source"] = result(OK, f"media:content → {image_url[:80]}")

    if not image_url:
        enc = item.find("enclosure")
        if enc is not None and "image" in (enc.get("type") or ""):
            image_url = enc.get("url")
            checks["image_source"] = result(OK, f"enclosure → {image_url[:80]}")

    if not image_url:
        desc = (item.findtext("description") or
                item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc)
        if m:
            image_url = m.group(1)
            checks["image_source"] = result(OK, f"<img> in description → {image_url[:80]}")

    if not image_url:
        checks["image_source"] = result(WARN, "no image in RSS — falling back to og:image scrape")
        if link and link.startswith("http"):
            image_url = fetch_og_image(link)
            if image_url:
                checks["og_image"] = result(OK, image_url[:90])
            else:
                checks["og_image"] = result(WARN, "og:image scrape returned nothing")

    # Final fallback: use hardcoded image_url from master_sites.json entry
    if not image_url and site.get("image_url"):
        image_url = site["image_url"]
        checks["image_source"] = result(WARN, f"using hardcoded image_url → {image_url[:80]}")

    # ── 5. Image actually loads (hotlink check) ───────────────────────────────
    if image_url:
        checks["image_loads"] = check_image_loads(image_url, referer=url)
    else:
        checks["image_loads"] = result(FAIL, "no image URL to test")

    return checks


# ── Formatting ─────────────────────────────────────────────────────────────────

def passed(checks: dict[str, str]) -> bool:
    return all(not v.startswith(FAIL) for v in checks.values())


def print_report(name: str, checks: dict[str, str], show_passing: bool = True) -> bool:
    ok = passed(checks)
    if not show_passing and ok:
        return ok
    status = f"PASS {OK}" if ok else f"FAIL {FAIL}"
    print(f"\n{'─'*60}")
    print(f"  {name}  →  {status}")
    print(f"{'─'*60}")
    for key, val in checks.items():
        print(f"  {key:<18} {val}")
    return ok


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Validate recipe sites before adding to master_sites.json")
    group  = parser.add_mutually_exclusive_group()
    group.add_argument("--site", metavar="NAME",
                       help="Validate one site by name from master_sites.json")
    group.add_argument("--url",  metavar="URL",
                       help="Validate a new candidate URL (not yet in master_sites.json)")
    parser.add_argument("--failing", action="store_true",
                        help="Only print failures")
    args = parser.parse_args()

    show_passing = not args.failing
    all_passed   = True

    if args.url:
        # Ad-hoc candidate
        site = {"name": args.url, "url": args.url}
        checks = validate_site(site)
        ok = print_report(args.url, checks, show_passing)
        all_passed = all_passed and ok

    else:
        # Load master list
        master_path = DATA_DIR / "master_sites.json"
        if not master_path.exists():
            print(f"ERROR: {master_path} not found", file=sys.stderr)
            sys.exit(1)
        with open(master_path, encoding="utf-8") as f:
            master = json.load(f)

        if args.site:
            targets = [s for s in master if s["name"].lower() == args.site.lower()]
            if not targets:
                print(f"ERROR: site '{args.site}' not found in master_sites.json", file=sys.stderr)
                sys.exit(1)
        else:
            targets = [s for s in master if s.get("active", True)]

        skipped = len(master) - len(targets)
        suffix  = f" ({skipped} inactive skipped)" if skipped else ""
        print(f"Validating {len(targets)} site(s)…{suffix}")
        for site in targets:
            checks = validate_site(site)
            ok = print_report(site["name"], checks, show_passing)
            all_passed = all_passed and ok

    print()
    if all_passed:
        print(f"{OK} All sites passed.")
    else:
        print(f"{FAIL} One or more sites failed — fix before pushing to master_sites.json.")
        sys.exit(1)


if __name__ == "__main__":
    main()
