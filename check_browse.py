"""
check_browse.py — CraveBox Browse Cuisines health checker

Tier 1 (fast, ~2 min):   checks homepage reachability + RSS feed validity
Tier 2 (deep, ~70 min):  extracts a real recipe via the CraveBox backend

Usage:
  python check_browse.py                        # fast check all sites
  python check_browse.py --deep                 # fast + deep all sites
  python check_browse.py --deep --only-failed   # deep check only fast failures
  python check_browse.py --site https://x.com   # check one site (fast + deep)
  python check_browse.py --report               # print summary from site_health.json
"""

import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_DIR    = Path(__file__).parent / "data"
BROWSE_PATH = DATA_DIR / "browse_cuisines.json"
HEALTH_PATH = DATA_DIR / "site_health.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CraveBox-Checker/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BACKEND_URL = os.environ.get("CRAVEBOX_BACKEND", "https://cravebox-backend.onrender.com")
API_KEY     = os.environ.get("CRAVEBOX_API_KEY", "85bf5ec2371d9cc653837f288c4b1a365babbe551972063266e46e71c6c9dcc3")

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ── Helpers ────────────────────────────────────────────────────────────────────

def load_browse():
    with open(BROWSE_PATH, encoding="utf-8") as f:
        return json.load(f)

def load_health():
    if not HEALTH_PATH.exists():
        return {"checked_at": NOW, "results": {}}
    with open(HEALTH_PATH, encoding="utf-8") as f:
        return json.load(f)

def save_health(health):
    health["checked_at"] = NOW
    with open(HEALTH_PATH, "w", encoding="utf-8") as f:
        json.dump(health, f, indent=2, ensure_ascii=False)

def _status_label(code):
    if code == 200:   return "ok"
    if code in (403, 503, 429): return "dead"
    if code == 404:   return "dead"
    return "warn"

# ── Tier 1: Fast check ─────────────────────────────────────────────────────────

def fast_check_site(site):
    url     = site["url"]
    rss_url = site.get("rss_url", "")
    result  = {"name": site["name"], "checked_at": NOW}

    # 1. Homepage
    try:
        t0   = time.time()
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        ms   = int((time.time() - t0) * 1000)
        code = resp.status_code
    except requests.exceptions.Timeout:
        result["fast"] = {"status": "dead", "http_code": 0, "response_ms": 10000,
                          "rss_ok": False, "rss_items": 0, "error": "timeout", "checked_at": NOW}
        return url, result
    except Exception as e:
        result["fast"] = {"status": "dead", "http_code": 0, "response_ms": 0,
                          "rss_ok": False, "rss_items": 0, "error": str(e)[:80], "checked_at": NOW}
        return url, result

    http_status = _status_label(code)

    # 2. RSS feed
    rss_ok    = False
    rss_items = 0
    rss_error = ""
    if rss_url:
        try:
            rr = requests.get(rss_url, headers=HEADERS, timeout=10)
            if rr.status_code == 200:
                root  = ET.fromstring(rr.content)
                items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
                rss_items = len(items)
                rss_ok    = rss_items > 0
            else:
                rss_error = f"HTTP {rr.status_code}"
        except Exception as e:
            rss_error = str(e)[:60]
    else:
        rss_error = "no rss_url"

    # Overall fast status
    if http_status == "dead":
        status = "dead"
    elif not rss_ok:
        status = "warn"
    else:
        status = "ok"

    fast = {
        "status":      status,
        "http_code":   code,
        "response_ms": ms,
        "rss_ok":      rss_ok,
        "rss_items":   rss_items,
        "checked_at":  NOW,
    }
    if rss_error:
        fast["rss_error"] = rss_error

    result["fast"] = fast
    return url, result


def run_fast(sites, workers=20):
    print(f"\n── Tier 1: Fast check ({len(sites)} sites, {workers} workers) ──")
    health  = load_health()
    results = health.get("results", {})
    done    = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fast_check_site, s): s for s in sites}
        for future in as_completed(futures):
            site_url, result = future.result()
            if site_url not in results:
                results[site_url] = {}
            results[site_url].update(result)
            done += 1
            st = result.get("fast", {}).get("status", "?")
            icon = "✓" if st == "ok" else ("⚠" if st == "warn" else "✗")
            print(f"  [{done:3}/{len(sites)}] {icon} {result['name'][:40]:<40}  {st}")

    health["results"] = results
    save_health(health)
    _print_fast_summary(results, sites)
    return results


def _print_fast_summary(results, sites):
    ok = warn = dead = 0
    for s in sites:
        st = results.get(s["url"], {}).get("fast", {}).get("status", "?")
        if st == "ok":   ok   += 1
        elif st == "warn": warn += 1
        else:              dead += 1
    print(f"\n  Fast summary: {ok} ok  {warn} warn  {dead} dead\n")


# ── Tier 2: Deep check ─────────────────────────────────────────────────────────

def _get_rss_urls(site, max_items=5):
    """Return up to max_items article URLs from the RSS feed."""
    rss_url = site.get("rss_url", "")
    if not rss_url:
        return []
    try:
        rr   = requests.get(rss_url, headers=HEADERS, timeout=10)
        root = ET.fromstring(rr.content)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        urls = []
        for item in items[:max_items]:
            link = (item.findtext("link") or
                    (item.find("{http://www.w3.org/2005/Atom}link") or {}).get("href", ""))
            if link.strip():
                urls.append(link.strip())
        return urls
    except Exception:
        return []


def _extract_one(recipe_url):
    """Submit one URL to the backend and return (n_ing, n_step, conf, error)."""
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    try:
        resp = requests.post(
            f"{BACKEND_URL}/extract",
            json={"url": recipe_url},
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            return 0, 0, "", f"extract HTTP {resp.status_code}"
        job_id = resp.json().get("job_id")
    except Exception as e:
        return 0, 0, "", str(e)[:80]

    poll_headers = {"X-API-Key": API_KEY} if API_KEY else {}
    for _ in range(20):
        time.sleep(3)
        try:
            sr   = requests.get(f"{BACKEND_URL}/status/{job_id}", headers=poll_headers, timeout=10)
            data = sr.json()
            if data.get("status") == "done":
                break
            if data.get("status") == "error":
                return 0, 0, "", data.get("error", "backend error")[:80]
        except Exception:
            pass
    else:
        return 0, 0, "", "poll timeout"

    try:
        rr     = requests.get(f"{BACKEND_URL}/result/{job_id}", headers=poll_headers, timeout=10)
        recipe = rr.json().get("recipe", {})
        n_ing  = len(recipe.get("ingredients", []))
        n_step = len(recipe.get("steps", []))
        conf   = recipe.get("confidence", "")
        return n_ing, n_step, conf, ""
    except Exception as e:
        return 0, 0, "", str(e)[:80]


def deep_check_site(site):
    url       = site["url"]
    result    = {}
    rss_urls  = _get_rss_urls(site, max_items=5)

    if not rss_urls:
        result["deep"] = {
            "status": "fail", "error": "no RSS item URLs",
            "ingredients": 0, "steps": 0, "checked_at": NOW
        }
        return url, result

    # Try each RSS item until one extracts a real recipe
    last_error  = ""
    tested_url  = rss_urls[0]
    for recipe_url in rss_urls:
        n_ing, n_step, conf, err = _extract_one(recipe_url)
        if n_ing >= 3 and n_step >= 2:
            result["deep"] = {
                "status":      "ok",
                "tested_url":  recipe_url,
                "ingredients": n_ing,
                "steps":       n_step,
                "confidence":  conf,
                "checked_at":  NOW,
            }
            return url, result
        if n_ing > 0 or n_step > 0:
            # partial — keep trying but remember this
            tested_url = recipe_url
            last_error = ""
            result["deep"] = {
                "status":      "warn",
                "tested_url":  recipe_url,
                "ingredients": n_ing,
                "steps":       n_step,
                "confidence":  conf,
                "checked_at":  NOW,
            }
        else:
            last_error = err or "0 ingredients / 0 steps"

    # Nothing worked — return warn if we had partial, else fail
    if "deep" in result:
        return url, result

    result["deep"] = {
        "status":      "fail",
        "error":       last_error,
        "tested_url":  tested_url,
        "ingredients": 0,
        "steps":       0,
        "checked_at":  NOW,
    }
    return url, result


def run_deep(sites, workers=4):
    print(f"\n── Tier 2: Deep check ({len(sites)} sites, {workers} workers) ──")
    if not API_KEY:
        print("  ⚠  CRAVEBOX_API_KEY not set — requests will be unauthenticated")
    health  = load_health()
    results = health.get("results", {})
    done    = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(deep_check_site, s): s for s in sites}
        for future in as_completed(futures):
            site_url, result = future.result()
            if site_url not in results:
                results[site_url] = {}
            results[site_url].update(result)
            done += 1
            deep = result.get("deep", {})
            st   = deep.get("status", "?")
            icon = "✓" if st == "ok" else ("⚠" if st == "warn" else "✗")
            name = results[site_url].get("name", site_url)
            ing  = deep.get("ingredients", 0)
            step = deep.get("steps", 0)
            print(f"  [{done:3}/{len(sites)}] {icon} {name[:38]:<38}  {st}  "
                  f"({ing} ingredients, {step} steps)")

    health["results"] = results
    save_health(health)
    _print_deep_summary(results, sites)
    return results


def _print_deep_summary(results, sites):
    ok = warn = fail = 0
    failures = []
    for s in sites:
        deep = results.get(s["url"], {}).get("deep", {})
        st   = deep.get("status", "?")
        if st == "ok":   ok   += 1
        elif st == "warn": warn += 1
        else:
            fail += 1
            failures.append(s["name"])
    print(f"\n  Deep summary: {ok} ok  {warn} warn  {fail} fail")
    if failures:
        print(f"\n  Failed sites:")
        for name in failures:
            print(f"    ✗ {name}")
    print()


# ── Report ─────────────────────────────────────────────────────────────────────

def print_report():
    if not HEALTH_PATH.exists():
        print("No site_health.json found. Run a check first.")
        return
    health  = load_health()
    results = health.get("results", {})
    sites   = load_browse()

    print(f"\n── Health Report (checked {health.get('checked_at','?')}) ──\n")
    print(f"{'Site':<40} {'Fast':^6} {'Deep':^6} {'HTTP':^5} {'ms':^6} {'Ing':^4} {'Steps':^5}")
    print("─" * 75)

    for s in sites:
        r    = results.get(s["url"], {})
        fast = r.get("fast", {})
        deep = r.get("deep", {})
        fst  = fast.get("status", "—")
        dst  = deep.get("status", "—")
        code = fast.get("http_code", "—")
        ms   = fast.get("response_ms", "—")
        ing  = deep.get("ingredients", "—")
        stp  = deep.get("steps", "—")
        icon = "✓" if fst == "ok" else ("⚠" if fst == "warn" else "✗")
        print(f"  {icon} {s['name'][:38]:<38} {fst:^6} {dst:^6} {str(code):^5} {str(ms):^6} {str(ing):^4} {str(stp):^5}")

    # Summary
    f_ok = sum(1 for s in sites if results.get(s["url"],{}).get("fast",{}).get("status")=="ok")
    f_wn = sum(1 for s in sites if results.get(s["url"],{}).get("fast",{}).get("status")=="warn")
    f_dd = sum(1 for s in sites if results.get(s["url"],{}).get("fast",{}).get("status")=="dead")
    d_ok = sum(1 for s in sites if results.get(s["url"],{}).get("deep",{}).get("status")=="ok")
    d_wn = sum(1 for s in sites if results.get(s["url"],{}).get("deep",{}).get("status")=="warn")
    d_fl = sum(1 for s in sites if results.get(s["url"],{}).get("deep",{}).get("status")=="fail")
    print(f"\n  Fast:  {f_ok} ok · {f_wn} warn · {f_dd} dead")
    print(f"  Deep:  {d_ok} ok · {d_wn} warn · {d_fl} fail\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CraveBox Browse Cuisines health checker")
    parser.add_argument("--deep",        action="store_true", help="Run deep extraction check after fast check")
    parser.add_argument("--only-failed", action="store_true", help="Deep check only fast warn/dead sites")
    parser.add_argument("--site",        type=str,            help="Check a single site URL")
    parser.add_argument("--report",      action="store_true", help="Print summary from existing site_health.json")
    args = parser.parse_args()

    if args.report:
        print_report()
        return

    sites = load_browse()

    if args.site:
        match = [s for s in sites if s["url"].rstrip("/") == args.site.rstrip("/")]
        if not match:
            print(f"Site not found in browse_cuisines.json: {args.site}")
            sys.exit(1)
        sites = match
        run_fast(sites, workers=1)
        run_deep(sites, workers=1)
        print_report()
        return

    # Fast check
    results = run_fast(sites)

    # Deep check
    if args.deep:
        if args.only_failed:
            deep_sites = [s for s in sites
                          if results.get(s["url"], {}).get("fast", {}).get("status") in ("warn", "dead")]
            print(f"  Skipping {len(sites)-len(deep_sites)} ok sites — running deep on {len(deep_sites)} warn/dead")
        else:
            deep_sites = sites
        if deep_sites:
            run_deep(deep_sites)

    print_report()


if __name__ == "__main__":
    main()
