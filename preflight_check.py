#!/usr/bin/env python3
"""
Preflight check for the cravebox-world-sites schedule.

Lead times
----------
- 1 day : tomorrow's featured recipe URL
- 2 days: incoming featured sites when next rotation is ≤ 48 h away

Exit 0 = all clear.  Exit 1 = failures found (GitHub Actions marks the run red
and the caller workflow opens a GitHub Issue).
"""
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCHEDULE_FILE = Path(__file__).parent / "data" / "schedule.json"
TIMEOUT = 12          # seconds per request
USER_AGENT = "CraveBox-Preflight/1.0"


# ── HTTP check ───────────────────────────────────────────────────────────────

def check_url(url: str) -> tuple[bool, str]:
    """Return (ok, human-readable status)."""
    def _head(u):
        req = urllib.request.Request(u, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status

    def _get_range(u):
        req = urllib.request.Request(
            u, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status

    try:
        status = _head(url)
        return status < 500, f"HTTP {status}"
    except urllib.error.HTTPError as e:
        if e.code < 500:
            return True, f"HTTP {e.code}"
        # Some servers reject HEAD — retry with a byte-range GET
        try:
            status = _get_range(url)
            return status < 500, f"HTTP {status} (GET)"
        except Exception as e2:
            return False, f"HTTP {e.code} + GET failed: {e2}"
    except Exception as e:
        return False, str(e)


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    # Ensure timezone-aware so comparisons with now (UTC) always work
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def weekday_key(dt: datetime) -> str:
    """'mon' … 'sun'"""
    return dt.strftime("%a").lower()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    now      = datetime.now(timezone.utc)
    tomorrow = now + timedelta(days=1)

    with open(SCHEDULE_FILE, encoding="utf-8") as f:
        schedule = json.load(f)

    # Attach parsed datetime and sort chronologically
    for entry in schedule:
        entry["_dt"] = parse_dt(entry["starts"])
    schedule.sort(key=lambda e: e["_dt"])

    # Active entry: latest one whose starts ≤ now
    active = None
    for entry in schedule:
        if entry["_dt"] <= now:
            active = entry

    failures: list[str] = []

    # ── 1-day lead: tomorrow's recipe ────────────────────────────────────────
    print("═" * 60)
    print(f"Preflight check  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("═" * 60)
    print("\n[1-DAY LEAD] Tomorrow's featured recipe")

    if active:
        day_key = weekday_key(tomorrow)
        recipe  = active.get("recipes", {}).get(day_key)
        if recipe:
            url  = recipe.get("recipe_url") or recipe.get("url", "")
            name = recipe.get("title", url)[:70]
            if url:
                ok, msg = check_url(url)
                mark = "✅" if ok else "❌"
                print(f"  {mark}  [{day_key.upper()}] {name}")
                print(f"        {url}")
                print(f"        → {msg}")
                if not ok:
                    failures.append(
                        f"RECIPE ({day_key.upper()}): {name}\n"
                        f"  URL : {url}\n"
                        f"  Result: {msg}"
                    )
            else:
                print(f"  ⚠️  No URL in schedule for {day_key}")
        else:
            print(f"  ⚠️  No recipe entry for '{day_key}' in week {active.get('week')}")
    else:
        print("  ⚠️  No active schedule entry found.")

    # ── 2-day lead: incoming sites ────────────────────────────────────────────
    print("\n[2-DAY LEAD] Incoming featured sites")

    next_entry = next((e for e in schedule if e["_dt"] > now), None)

    if next_entry:
        hours_until = (next_entry["_dt"] - now).total_seconds() / 3600
        if hours_until <= 48:
            print(f"  ⏰  Next rotation in {hours_until:.1f} h  (week {next_entry.get('week')}) — checking:")
            for site in next_entry.get("sites", []):
                url  = site.get("url", "")
                name = site.get("name", url)
                if not url:
                    continue
                ok, msg = check_url(url)
                mark = "✅" if ok else "❌"
                print(f"  {mark}  {name}")
                print(f"        {url}  → {msg}")
                if not ok:
                    failures.append(
                        f"SITE (week {next_entry.get('week')}): {name}\n"
                        f"  URL : {url}\n"
                        f"  Result: {msg}"
                    )
        else:
            print(f"  ✅  Next rotation in {hours_until:.1f} h — nothing to check yet.")
    else:
        print("  ⚠️  No upcoming schedule entry found.")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    if failures:
        print(f"❌  {len(failures)} failure(s) found:\n")
        for item in failures:
            print(f"  • {item}\n")
        # Write failures to file so the workflow can read them for the issue body
        out = Path(__file__).parent / "preflight_failures.txt"
        out.write_text("\n\n".join(failures), encoding="utf-8")
        sys.exit(1)
    else:
        print("✅  All preflight checks passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
