"""
rotate.py — CraveBox content rotator

Reads data/schedule.json and writes data/featured.json.

Slot logic (sequential):
  slot_idx = floor( elapsed_hours_since_first_entry / SLOT_HOURS )
  Clamped to last entry if schedule is exhausted — no crash, content keeps showing.

Testing:  SLOT_HOURS = 2   (cycles through schedule entries every 2 hours)
Production: SLOT_HOURS = 168  (weekly)

Run: python rotate.py  (via GitHub Actions cron)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────

SLOT_HOURS = 2     # testing: 2  |  production: 168
DATA_DIR   = Path(__file__).parent / "data"

# ── Helpers ────────────────────────────────────────────────────────────────────

def load_schedule():
    path = DATA_DIR / "schedule.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def parse_dt(s):
    """Parse an ISO date string (YYYY-MM-DD or full ISO) to UTC datetime."""
    if not s:
        return None
    if len(s) == 10:            # date only → midnight UTC
        s += "T00:00:00+00:00"
    elif s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    print(f"[rotate] UTC: {now.isoformat()}")

    sched = load_schedule()
    if not sched:
        print("[rotate] schedule.json missing or empty — nothing to do")
        return

    # ── Find current slot ──────────────────────────────────────────────────────
    first_dt = parse_dt(sched[0].get("starts", ""))
    if first_dt is None:
        first_dt = now

    elapsed_hours = max(0.0, (now - first_dt).total_seconds() / 3600)
    slot_idx      = int(elapsed_hours // SLOT_HOURS)

    if slot_idx >= len(sched):
        slot_idx = len(sched) - 1
        print(f"[rotate] WARNING: schedule exhausted — repeating last entry (slot {slot_idx + 1})")
    else:
        print(f"[rotate] Slot {slot_idx + 1}/{len(sched)}")

    entry = sched[slot_idx]
    print(f"[rotate] Week {entry.get('week','?')} · starts {entry.get('starts','?')}")

    # ── Build featured.json ────────────────────────────────────────────────────
    recipes    = entry.get("recipes") or {}
    day_names  = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    if SLOT_HOURS < 168:
        # TESTING: simulate 7 days within the slot (~17 min each at SLOT_HOURS=2)
        elapsed_in_slot = (now - first_dt).total_seconds() / 3600 - (slot_idx * SLOT_HOURS)
        recipe_idx  = int(elapsed_in_slot / SLOT_HOURS * 7)
        recipe_idx  = min(recipe_idx, 6)
        today_day   = day_names[recipe_idx]
        print(f"[rotate] Testing sub-slot: recipe {recipe_idx + 1}/7 ({today_day})")
    else:
        # PRODUCTION: one recipe per calendar day
        today_day = day_names[now.weekday()]

    # Today's recipe: prefer matching day, fall back to first non-null
    today_recipe = recipes.get(today_day)
    if not today_recipe:
        today_recipe = next((r for r in recipes.values() if r), None)

    output = {
        "updated_at":       now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slot_hours":       SLOT_HOURS,
        "slot_idx":         slot_idx,
        "week":             entry.get("week"),
        "featured_sites":   entry.get("sites", []),
        "featured_recipes": recipes,       # dict Mon–Sun  (new format)
        "featured_recipe":  today_recipe,  # single entry  (backward compat)
    }

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
