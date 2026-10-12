"""
audit_new_sites.py — Fetch images for sites with empty image_url in master_sites.json.

Strategy (in order):
  1. media:content, enclosure, or <img> in RSS description
  2. data-src / srcset / data-lazy-src attributes in RSS description
  3. og:image scraped from the first RSS item's link
  4. og:image scraped from the site homepage

Also fixes known bad RSS URLs before fetching.

Usage:
    python audit_new_sites.py
"""

import json, re, sys, xml.etree.ElementTree as ET
from pathlib import Path
import requests

HEADERS  = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
TIMEOUT  = 12
PATH     = Path(__file__).parent / "data" / "master_sites.json"

SKIP_IMG = ('icon','logo','avatar','gravatar','badge','favicon','star','rating',
            'w480','w200','w100','32x32','64x64','data:','placeholder','spinner',
            'loading','blank','transparent','youtube.com','youtu.be','vimeo.com',
            '.svg','soundcloud')

# ── Known RSS URL fixes ────────────────────────────────────────────────────────
RSS_OVERRIDES = {
    "A Spanish Bite":       "https://www.aspanishbite.com/feed/",
    "Not Quite Nigella":    "https://www.notquitenigella.com/feed/atom/",
    "Serious Eats":         "https://www.seriouseats.com/feeds/all/rss.xml",
    "Simply Recipes":       "https://feeds.feedburner.com/SimplyRecipes",
    "AmazingRibs":          "https://feeds.feedburner.com/AmazingRibs",
    "My Persian Kitchen":   "https://mypersiankitchen.com/feed/",
    "Persian Mama":         "https://www.persianmama.com/feed/rss2/",
    "Pilar's Chilean Food": "https://www.chileanfoodandgarden.com/feed/",
    "What's Gaby Cooking":  "https://www.whatsgabycooking.com/feed/rss2/",
}

def is_bad(url):
    return not url or any(s in url.lower() for s in SKIP_IMG)

def extract_images_from_text(html):
    """Find all candidate image URLs in an HTML/XML blob."""
    found = []
    # src, data-src, data-lazy-src, srcset (first URL)
    for attr in ('data-lazy-src', 'data-src', 'src', 'srcset'):
        for m in re.finditer(rf'{attr}=["\']([^"\']+)["\']', html):
            val = m.group(1).split(',')[0].split(' ')[0].strip()
            if val.startswith('http') and not is_bad(val):
                found.append(val)
    return found

def fetch_og_image(url):
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=HEADERS,
                         verify=False, allow_redirects=True)
        # og:image
        for pattern in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]:
            m = re.search(pattern, r.text)
            if m and not is_bad(m.group(1)):
                return m.group(1)
        # Fallback: any large image in page
        imgs = extract_images_from_text(r.text)
        for img in imgs:
            if re.search(r'-\d{3,4}x\d{3,4}', img) or re.search(r'/\d{4}/', img):
                return img
    except Exception:
        pass
    return None

def get_image_from_rss(rss_url, site_url):
    try:
        r = requests.get(rss_url, timeout=TIMEOUT, headers=HEADERS,
                         verify=False, allow_redirects=True)
        r.raise_for_status()

        # Strip BOM / XML declaration issues
        content = r.content
        if content.startswith(b'\xef\xbb\xbf'):
            content = content[3:]
        text = content.decode('utf-8', errors='replace')
        # Remove duplicate XML declarations
        text = re.sub(r'<\?xml[^?]+\?>\s*<\?xml[^?]+\?>', '<?xml version="1.0" encoding="UTF-8"?>', text)

        try:
            root = ET.fromstring(text.encode('utf-8'))
        except ET.ParseError:
            # Last resort: strip all XML declarations and retry
            text2 = re.sub(r'<\?xml[^?]+\?>', '', text).strip()
            root = ET.fromstring(text2.encode('utf-8'))

        ns = {"media": "http://search.yahoo.com/mrss/",
              "content": "http://purl.org/rss/1.0/modules/content/"}
        items = (root.findall(".//item") or
                 root.findall(".//{http://www.w3.org/2005/Atom}entry"))
        if not items:
            return None, None, "no items in feed"

        first_link = None
        for item in items[:5]:
            # Grab first link for og:image fallback
            link = (item.findtext("link") or item.findtext("guid") or "").strip()
            if link and link.startswith("http") and not first_link:
                first_link = link

            # 1. media:content
            for mc in item.findall("media:content", ns):
                u = mc.get("url", "")
                if u and not is_bad(u):
                    return u, first_link, "media:content"
            for mc in item.findall("media:thumbnail", ns):
                u = mc.get("url", "")
                if u and not is_bad(u):
                    return u, first_link, "media:thumbnail"

            # 2. enclosure
            enc = item.find("enclosure")
            if enc is not None and "image" in (enc.get("type") or ""):
                u = enc.get("url", "")
                if u and not is_bad(u):
                    return u, first_link, "enclosure"

            # 3. description + content:encoded — full scan
            desc = (item.findtext("description") or "") + \
                   (item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
            if desc:
                imgs = extract_images_from_text(desc)
                if imgs:
                    return imgs[0], first_link, "img in description"

        return None, first_link, "no image in feed"
    except Exception as e:
        return None, None, f"ERROR: {e}"


def main():
    requests.packages.urllib3.disable_warnings()  # suppress SSL warnings

    with open(PATH, encoding="utf-8") as f:
        sites = json.load(f)

    BAD_PREFIXES = ("https://www.youtube.com", "https://youtu.be",
                    "https://vimeo.com", "https://player.")
    # Clear bad image_url values (video embeds, etc.)
    for s in sites:
        img = s.get("image_url", "")
        if any(img.startswith(p) for p in BAD_PREFIXES):
            print(f"  Clearing bad image_url for {s['name']}: {img[:60]}")
            s["image_url"] = ""

    targets = [s for s in sites if not s.get("image_url")]
    if not targets:
        print("All sites already have images — nothing to do.")
        return

    print(f"Auditing {len(targets)} sites with no image...\n")
    fixed, failed = [], []

    for site in targets:
        name     = site["name"]
        site_url = site["url"].rstrip("/")
        rss_url  = RSS_OVERRIDES.get(name) or site.get("rss_url") or site_url + "/feed/"

        # Apply RSS override to master_sites.json too
        if name in RSS_OVERRIDES and site.get("rss_url") != RSS_OVERRIDES[name]:
            site["rss_url"] = RSS_OVERRIDES[name]

        img, first_link, source = get_image_from_rss(rss_url, site_url)

        # Fallback 1: og:image from first RSS item
        if not img and first_link:
            img = fetch_og_image(first_link)
            if img:
                source = f"og:image from {first_link[:60]}"

        # Fallback 2: og:image from site homepage
        if not img:
            img = fetch_og_image(site_url)
            if img:
                source = "og:image from homepage"

        if img:
            site["image_url"] = img
            fixed.append(name)
            print(f"  ✓  {name}")
            print(f"       [{source}]")
            print(f"       {img[:100]}")
        else:
            failed.append((name, source))
            print(f"  ✗  {name}  — {source}")

    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(sites, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"  Fixed: {len(fixed)}  |  Still missing: {len(failed)}")
    if failed:
        print(f"\nThese need a manual image URL in master_sites.json:")
        for name, reason in failed:
            print(f"  • {name}: {reason}")
    else:
        print("All images populated!")


if __name__ == "__main__":
    main()
