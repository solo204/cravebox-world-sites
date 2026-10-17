"""
Fill empty image_url fields in browse_cuisines.json
using RSS feed images (with og:image fallback).
Run from anywhere; paths are absolute.
"""
import json, re, requests, xml.etree.ElementTree as ET

BROWSE_PATH = r"C:\Users\John\documents\projects\tiktok recipe collection\cravebox-world-sites\data\browse_cuisines.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CraveBox-ImageFetcher/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def fetch_og_image(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text) or
             re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text))
        return m.group(1) if m else None
    except Exception:
        return None

def fetch_rss_image(site):
    rss_url = site.get("rss_url", "")
    if not rss_url:
        return None
    try:
        r = requests.get(rss_url, headers=HEADERS, timeout=10)
        root = ET.fromstring(r.content)
        ns = {"media": "http://search.yahoo.com/mrss/",
              "content": "http://purl.org/rss/1.0/modules/content/"}
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for item in items[:3]:
            # media:content
            mc = item.find("media:content", ns)
            if mc is not None:
                ctype = mc.get("type", "")
                url   = mc.get("url", "")
                if url and ("image" in ctype or re.search(r'\.(jpg|jpeg|png|webp)', url, re.I)):
                    return url
            # enclosure
            enc = item.find("enclosure")
            if enc is not None and "image" in (enc.get("type") or ""):
                return enc.get("url")
            # <img> in description or content:encoded
            text = (item.findtext("description") or
                    item.findtext("content:encoded", namespaces=ns) or "")
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

def get_image(site):
    img = fetch_rss_image(site)
    if img:
        return img
    return fetch_og_image(site["url"])

with open(BROWSE_PATH, encoding="utf-8") as f:
    sites = json.load(f)

empty = [s for s in sites if not s.get("image_url")]
print(f"{len(empty)} sites with empty image_url\n")

updated = 0
for i, site in enumerate(sites):
    if site.get("image_url"):
        continue
    print(f"  [{i+1}] {site['name'][:45]:<45}", end=" ", flush=True)
    img = get_image(site)
    if img:
        site["image_url"] = img
        updated += 1
        print(f"✓ {img[:70]}")
    else:
        print("✗ no image found")

with open(BROWSE_PATH, "w", encoding="utf-8") as f:
    json.dump(sites, f, indent=2, ensure_ascii=False)

print(f"\nDone. Updated {updated}/{len(empty)} sites.")
