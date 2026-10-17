"""
Manually patch image_url for sites that fill_images.py couldn't reach.
Add entries to PATCHES below, then run this script.
"""
import json

BROWSE_PATH = r"C:\Users\John\documents\projects\tiktok recipe collection\cravebox-world-sites\data\browse_cuisines.json"

# Paste any image URL you want — grab one from the site's homepage or a recipe page
PATCHES = {
    "https://www.cookingwithria.com": "",   # paste image URL between the quotes
    "https://trinigourmet.com":        "",   # paste image URL between the quotes
}

with open(BROWSE_PATH, encoding="utf-8") as f:
    sites = json.load(f)

for site in sites:
    img = PATCHES.get(site["url"].rstrip("/")) or PATCHES.get(site["url"])
    if img:
        site["image_url"] = img
        print(f"  ✓ {site['name']}: {img[:70]}")

with open(BROWSE_PATH, "w", encoding="utf-8") as f:
    json.dump(sites, f, indent=2, ensure_ascii=False)

print("Done.")
