"""
resequence.py — Fix schedule.json dates after inserting or deleting a week.

Anchors to week 1's starts date and sets every subsequent week
exactly 7 days apart. Run after any manual schedule edit.

Usage:
    python resequence.py
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

SCHED_PATH = Path(__file__).parent / "data" / "schedule.json"

def parse_dt(s):
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if len(s) == 10:
        s += "T00:00:00+00:00"
    return datetime.fromisoformat(s)

def main():
    with open(SCHED_PATH, encoding="utf-8") as f:
        sched = json.load(f)

    anchor = parse_dt(sched[0].get("starts", ""))
    if anchor is None:
        print("ERROR: week 1 has no starts date.")
        return

    changed = 0
    for i, week in enumerate(sched):
        new_starts = (anchor + timedelta(weeks=i)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        if week.get("starts") != new_starts:
            print(f"  Week {week.get('week','?')}: {week.get('starts','')} → {new_starts}")
            week["starts"] = new_starts
            changed += 1

    with open(SCHED_PATH, "w", encoding="utf-8") as f:
        json.dump(sched, f, indent=2, ensure_ascii=False)

    if changed:
        print(f"\nFixed {changed} date(s) across {len(sched)} weeks.")
    else:
        print(f"All {len(sched)} weeks already in sequence — nothing changed.")

if __name__ == "__main__":
    main()
