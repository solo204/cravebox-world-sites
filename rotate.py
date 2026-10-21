"""
rotate.py — CraveBox content rotator

Reads data/schedule.json and writes data/featured.json.

Slot logic (sequential):
  slot_idx = floor( elapsed_hours_since_first_entry / SLOT_HOURS )
  Clamped to last entry if schedule is exhausted — no crash, content keeps showing.

Fallback logic (runs on every cron tick):
  - Each of the 4 featured sites is health-checked (HEAD request, 5s timeout)
  - Today's recipe image is checked (HEAD request)
  - Failed sites → replaced from site_reserves first, then any browse_cuisines site that is up
  - Failed recipe → replaced from recipe_reserves first, then candidates

Testing:  SLOT_HOURS = 2   (cycles through schedule entries every 2 hours)
Production: SLOT_HOURS = 168  (weekly)

Run: python rotate.py  (via GitHub Actions cron)
"""

import json
import requests
from datetime import datetime, timezone
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────

SLOT_HOURS = 2     # testing: 2  |  production: 168
DATA_DIR   = Path(__file__).parent / "data"
HEADERS    = {"User-Agent": "CraveBox-Rotator/1.0"}
HEALTH_TIMEOUT = 5   # seconds per health check

# ── Loaders ────────────────────────────────────────────────────────────────────

def load_schedule():
    path = DATA_DIR / "schedule.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_master():
    path = DATA_DIR / "browse_cuisines.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_history():
    path = DATA_DIR / "used_history.json"
    if not path.exists():
        return {"recipe_urls": [], "site_names": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_reserves():
    path = DATA_DIR / "reserves.json"
    if not path.exists():
        return {"site_reserves": [], "recipe_reserves": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def parse_dt(s):
    if not s:
        return None
    if len(s) == 10:
        s += "T00:00:00+00:00"
    elif s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)

# ── Health checks ──────────────────────────────────────────────────────────────

def site_is_up(site):
    """Return True if the site's home URL responds with < 500."""
    try:
        r = requests.head(site["url"], timeout=HEALTH_TIMEOUT,
                          headers=HEADERS, allow_redirects=True)
        return r.status_code < 500
    except Exception:
        return False

def image_is_up(url):
    """Return True if an image URL responds with 200."""
    if not url:
        return False
    try:
        r = requests.head(url, timeout=HEALTH_TIMEOUT,
                          headers=HEADERS, allow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False

# ── Fallback helpers ───────────────────────────────────────────────────────────

def find_fallback_site(failed_site, current_sites, global_site_reserves, master, history):
    """
    Find a replacement for a downed site:
      1. Try global site reserves (reserves.json)
      2. Fall back to browse_cuisines pool
    """
    current_names = {s["name"] for s in current_sites}
    current_names.add(failed_site["name"])

    # 1. Try global site reserves first
    for site in global_site_reserves:
        if site["name"] in current_names:
            continue
        if site_is_up(site):
            print(f"  [fallback] {failed_site['name']} → {site['name']} (global reserve)")
            return site

    # 2. Fall back to browse_cuisines pool
    history_names = set(history.get("site_names", []))
    pool = sorted(master, key=lambda s: (s["name"] not in history_names))
    for site in pool:
        if site["name"] in current_names:
            continue
        if site_is_up(site):
            print(f"  [fallback] {failed_site['name']} → {site['name']} (master pool)")
            return site

    print(f"  [fallback] No replacement found for {failed_site['name']} — keeping original")
    return failed_site

def find_fallback_recipe(candidates, global_recipe_reserves, used_urls):
    """
    Pick a fallback recipe:
      1. Try week's candidates first
      2. Try global recipe reserves (reserves.json)
    """
    pool = list(candidates) + list(global_recipe_reserves)
    for c in pool:
        url = c.get("url") or c.get("recipe_url") or ""
        if url in used_urls:
            continue
        img = c.get("image_url", "")
        if image_is_up(img):
            return c
    return None

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    print(f"[rotate] UTC: {now.isoformat()}")

    sched = load_schedule()
    if not sched:
        print("[rotate] schedule.json missing or empty — nothing to do")
        return

    # ── Find current slot ──────────────────────────────────────────────────────
    # Use starts timestamps — find the last entry whose start is <= now.
    # This is immune to SLOT_HOURS wrapping bugs.
    slot_idx = 0
    for i, e in enumerate(sched):
        entry_dt = parse_dt(e.get("starts", ""))
        if entry_dt and entry_dt <= now:
            slot_idx = i
        else:
            break  # schedule is ordered; no need to continue
    print(f"[rotate] Slot {slot_idx + 1}/{len(sched)}")

    entry = sched[slot_idx]
    print(f"[rotate] Week {entry.get('week','?')} · starts {entry.get('starts','?')}")

    # ── Today's recipe ─────────────────────────────────────────────────────────
    recipes   = entry.get("recipes") or {}
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    if SLOT_HOURS < 168:
        slot_start_dt = parse_dt(entry.get("starts", "")) or now
        elapsed_in_slot = max(0.0, (now - slot_start_dt).total_seconds() / 3600)
        recipe_idx  = int(elapsed_in_slot / SLOT_HOURS * 7)
        recipe_idx  = min(recipe_idx, 6)
        today_day   = day_names[recipe_idx]
        print(f"[rotate] Testing sub-slot: recipe {recipe_idx + 1}/7 ({today_day})")
    else:
        today_day = day_names[now.weekday()]

    today_recipe = recipes.get(today_day)
    if not today_recipe:
        today_recipe = next((r for r in recipes.values() if r), None)

    # ── Fallback checks ────────────────────────────────────────────────────────
    master                = load_master()
    history               = load_history()
    reserves              = load_reserves()
    used_urls             = set(history.get("recipe_urls", []))
    global_site_reserves  = reserves.get("site_reserves", [])
    global_recipe_reserves= reserves.get("recipe_reserves", [])
    candidates            = entry.get("candidates", [])
    fallbacks             = []

    # Check sites
    sites = list(entry.get("sites", []))
    for i, site in enumerate(sites):
        print(f"[rotate] Checking site: {site['name']} …", end=" ")
        if site_is_up(site):
            print("OK")
        else:
            print("DOWN")
            replacement = find_fallback_site(site, sites, global_site_reserves, master, history)
            if replacement["name"] != site["name"]:
                sites[i] = replacement
                fallbacks.append(f"site:{site['name']}→{replacement['name']}")

    # Check today's recipe image
    recipe_fallback = False
    if today_recipe:
        img = today_recipe.get("image_url", "")
        print(f"[rotate] Checking recipe image …", end=" ")
        if image_is_up(img):
            print("OK")
        else:
            print("DOWN — searching candidates then global reserves")
            replacement = find_fallback_recipe(candidates, global_recipe_reserves, used_urls)
            if replacement:
                print(f"  [fallback] recipe → {replacement.get('title','?')[:60]}")
                today_recipe = replacement
                recipe_fallback = True
                fallbacks.append(f"recipe→{replacement.get('title','?')[:40]}")
            else:
                print("  [fallback] No valid candidate or reserve found — keeping original")

    # ── Write featured.json ────────────────────────────────────────────────────
    output = {
        "updated_at":       now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slot_hours":       SLOT_HOURS,
        "slot_idx":         slot_idx,
        "week":             entry.get("week"),
        "featured_sites":   sites,
        "featured_recipes": recipes,
        "featured_recipe":  today_recipe,
    }
    if fallbacks:
        output["fallbacks"] = fallbacks
        print(f"[rotate] Fallbacks applied: {fallbacks}")

    out_path = DATA_DIR / "featured.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[rotate] Written → {out_path}")
    if today_recipe:
        print(f"[rotate] Today ({today_day}): {today_recipe.get('title','?')[:70]}")
    else:
        print(f"[rotate] No recipe for {today_day}")


if __name__ == "__main__":
    main()
