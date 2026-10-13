"""
site_manager.py — CraveBox World Sites local manager

Run:  python site_manager.py
Open: http://localhost:5001

Run plan.py first to generate data/schedule.json.
Requires: pip install flask requests
"""

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

try:
    from flask import Flask, jsonify, render_template_string, request, Response
except ImportError:
    print("Flask not found. Run:  python -m pip install flask requests")
    sys.exit(1)

import requests as req
from validate_sites import validate_site, passed
try:
    from rss_scraper import scrape_recipe_listing
except ImportError:
    scrape_recipe_listing = None

DATA_DIR      = Path(__file__).parent / "data"
MASTER_PATH   = DATA_DIR / "master_sites.json"
FEATURED_PATH = DATA_DIR / "featured.json"
SCHED_PATH    = DATA_DIR / "schedule.json"
HISTORY_PATH  = DATA_DIR / "used_history.json"
HEADERS       = {"User-Agent": "CraveBox-Rotator/1.0"}
SLOT_HOURS    = 2   # must match rotate.py
DAYS          = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

app = Flask(__name__)

# ── Data helpers ───────────────────────────────────────────────────────────────

def load_master():
    with open(MASTER_PATH, encoding="utf-8") as f:
        return json.load(f)

def save_master(sites):
    with open(MASTER_PATH, "w", encoding="utf-8") as f:
        json.dump(sites, f, indent=2, ensure_ascii=False)

def load_featured():
    # Pull latest from GitHub so the dashboard always reflects what the app sees
    try:
        subprocess.run(
            ["git", "pull", "--ff-only", "-q"],
            cwd=Path(__file__).parent,
            capture_output=True,
            timeout=6,
        )
    except Exception:
        pass  # offline or conflict — use whatever's local
    if not FEATURED_PATH.exists():
        return None
    with open(FEATURED_PATH, encoding="utf-8") as f:
        return json.load(f)

def load_schedule():
    if not SCHED_PATH.exists():
        return None
    with open(SCHED_PATH, encoding="utf-8") as f:
        return json.load(f)

def save_schedule(sched):
    with open(SCHED_PATH, "w", encoding="utf-8") as f:
        json.dump(sched, f, indent=2, ensure_ascii=False)

def pool_count(n):
    return n // 4

def current_slot_idx(sched):
    if not sched:
        return 0
    starts = sched[0].get("starts","")
    try:
        s = starts + "T00:00:00+00:00" if len(starts)==10 else starts.replace("Z","+00:00")
        first_dt = datetime.fromisoformat(s)
    except Exception:
        return 0
    elapsed = max(0,(datetime.now(timezone.utc)-first_dt).total_seconds()/3600)
    idx = int(elapsed // SLOT_HOURS)
    return min(idx, len(sched)-1)

def renumber(sched):
    for i,w in enumerate(sched):
        w["week"] = i+1
    return sched

def load_history():
    if not HISTORY_PATH.exists():
        return {"recipe_urls": [], "site_names": [], "pruned_weeks": 0, "last_pruned": None}
    with open(HISTORY_PATH, encoding="utf-8") as f:
        return json.load(f)

def save_history(h):
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=2, ensure_ascii=False)

def archive_weeks(weeks):
    """Add aired weeks' recipes + sites to used_history.json."""
    h = load_history()
    seen_urls  = set(h["recipe_urls"])
    seen_sites = set(h["site_names"])
    for week in weeks:
        for r in (week.get("recipes") or {}).values():
            if r and (r.get("url") or r.get("recipe_url")):
                seen_urls.add(r.get("url") or r.get("recipe_url"))
        for s in (week.get("sites") or []):
            if s.get("name"):
                seen_sites.add(s["name"])
    h["recipe_urls"]   = sorted(seen_urls)
    h["site_names"]    = sorted(seen_sites)
    h["pruned_weeks"]  = h.get("pruned_weeks", 0) + len(weeks)
    h["last_pruned"]   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_history(h)

def _fetch_og_image(url):
    """Grab og:image from an article page (used as image fallback)."""
    try:
        r = req.get(url, timeout=8, headers=HEADERS)
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text) or
             re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text))
        return m.group(1) if m else ""
    except Exception:
        return ""

def fetch_rss_candidates(site, max_items=15):
    """Fetch RSS items from a site; falls back to og:image scraping for missing images."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    rss_url = site.get("rss_url", site["url"].rstrip("/") + "/feed/")
    results = []
    try:
        r = req.get(rss_url, timeout=10, headers=HEADERS)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns   = {"atom": "http://www.w3.org/2005/Atom",
                "media": "http://search.yahoo.com/mrss/"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for item in items[:max_items]:
            title = (item.findtext("title") or
                     item.findtext("atom:title", None, ns) or "").strip()
            link  = (item.findtext("link") or
                     item.findtext("atom:link", None, ns) or "").strip()
            if not title or not link:
                continue
            # Skip non-recipe posts (collections, travel, roundups, deals)
            skip_words = ["instagram stories", "best deals", "prime day", "travel guide",
                          "what to cook", "salads that eat", "labor day recipes",
                          "gift guide", "roundup", "itinerary"]
            if any(w in title.lower() for w in skip_words):
                continue
            image_url = ""
            mc = item.find("media:content", ns)
            if mc is not None:
                candidate = mc.get("url", "")
                if candidate and not candidate.startswith("https://www.youtube"):
                    image_url = candidate
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
                "title":      title,
                "url":        link,
                "recipe_url": link,
                "image_url":  image_url,
                "site_name":  site.get("name", ""),
                "cuisine":    site.get("cuisine", ""),
            })
    except Exception as e:
        print(f"[refresh] RSS failed for {site.get('name')}: {e}")
        return results

    # For items still missing images, scrape og:image in parallel
    missing = [(i, r) for i, r in enumerate(results) if not r["image_url"]]
    if missing:
        print(f"[refresh] {site.get('name')}: fetching og:image for {len(missing)} items…")
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(_fetch_og_image, r["url"]): i for i, r in missing}
            for future in as_completed(futures):
                idx = futures[future]
                img = future.result()
                if img:
                    results[idx]["image_url"] = img

    return results

# ── HTML ───────────────────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CraveBox Site Manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0a0a0a;color:#e0e0e0;font-size:14px}
header{background:#111;border-bottom:1px solid #222;padding:14px 24px;
       display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:100}
header h1{font-size:17px;font-weight:700;color:#fff}
.badge{font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px}
.badge-orange{background:#ff6b35;color:#fff}
.badge-gray{background:#1e293b;color:#64748b}
.badge-green{background:#16a34a;color:#fff}
.badge-blue{background:#1e3a5f;color:#60a5fa}
.badge-air{background:#166534;color:#4ade80;animation:glow 2s infinite}
@keyframes glow{0%,100%{box-shadow:0 0 4px #4ade8055}50%{box-shadow:0 0 10px #4ade8099} }
.wrap{max-width:1400px;margin:0 auto;padding:20px 24px;display:flex;flex-direction:column;gap:20px}
.panel{background:#111;border:1px solid #1e1e1e;border-radius:10px;padding:18px 20px}
.panel-title{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
             color:#555;margin-bottom:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.panel-title span{font-weight:400;text-transform:none;letter-spacing:0;color:#888;font-size:12px}

/* ── Cards ── */
.card{background:#0f0f0f;border:1px solid #1e1e1e;border-radius:7px;
      overflow:hidden;display:flex;flex-direction:column;transition:border-color .15s}
.card.clickable{cursor:pointer}
.card.clickable:hover{border-color:#333}
.card img{width:100%;object-fit:cover;display:block;background:#1a1a1a}
.card .no-img{width:100%;background:#141414;display:flex;
              align-items:center;justify-content:center;color:#2a2a2a;font-size:10px}
.card .ci{padding:6px 8px;flex:1}
.card .cn{font-size:11px;font-weight:600;color:#ccc;overflow:hidden;
          text-overflow:ellipsis;white-space:nowrap;margin-bottom:1px}
.card .cn.wrap2{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;
                -webkit-box-orient:vertical}
.card .cs{font-size:10px;color:#555}

/* Live Now */
.now-sites-row{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px}
.now-recipes-row{display:grid;grid-template-columns:repeat(7,1fr);gap:8px}
.now-site img,.now-site .no-img{height:75px}
.now-recipe img,.now-recipe .no-img{height:85px}
.now-dot{display:inline-block;width:7px;height:7px;background:#22c55e;
         border-radius:50%;margin-right:5px;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4} }

/* Week blocks */
.week-block{border:1px solid #1e1e1e;border-radius:8px;overflow:hidden;margin-bottom:2px}
.week-block.on-air{border-color:#166534;box-shadow:0 0 0 1px #166534}
.week-header{background:#0f0f0f;border-bottom:1px solid #1a1a1a;
             padding:10px 14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.week-title{font-size:13px;font-weight:700;color:#ddd}
.week-dates{font-size:11px;color:#555}
.week-body{padding:14px}

/* Insert bar */
.insert-bar{display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer;
            opacity:.3;transition:opacity .15s}
.insert-bar:hover{opacity:1}
.insert-bar .ib-line{flex:1;height:1px;background:#1e1e1e}
.insert-bar .ib-btn{font-size:10px;font-weight:700;color:#555;background:#111;
                    border:1px solid #222;border-radius:10px;padding:2px 10px;
                    white-space:nowrap;cursor:pointer}
.insert-bar:hover .ib-btn{color:#ff6b35;border-color:#ff6b35}

/* Sites row */
.week-sites{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
.site-wrap{position:relative}
.sched-site img,.sched-site .no-img{height:55px}
.site-swap-btn{position:absolute;top:4px;right:4px;background:#1a1a1a;border:1px solid #2a2a2a;
               border-radius:4px;color:#666;font-size:10px;padding:2px 5px;cursor:pointer;z-index:2}
.site-swap-btn:hover,.site-swap-btn.active{background:#ff6b35;border-color:#ff6b35;color:#fff}
.site-picker{width:100%;background:#0a0a0a;border:1px solid #ff6b35;border-radius:6px;
             color:#e0e0e0;padding:6px 8px;font-size:11px;outline:none;margin-top:4px}

/* Day slots */
.week-days{display:grid;grid-template-columns:repeat(7,1fr);gap:8px;margin-bottom:14px}
.day-col{display:flex;flex-direction:column;gap:4px}
.day-label{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
           color:#444;text-align:center}
.day-slot{position:relative}
.day-slot .card{border:1px solid #1e1e1e}
.day-slot.target .card{border-color:#ff6b35;box-shadow:0 0 0 1px #ff6b35}
.sched-recipe img,.sched-recipe .no-img{height:70px}
.swap-btn{position:absolute;top:4px;right:4px;background:#1a1a1a;border:1px solid #2a2a2a;
          border-radius:4px;color:#666;font-size:10px;padding:2px 5px;
          cursor:pointer;z-index:2;line-height:1.4}
.swap-btn:hover{background:#2a2a2a;color:#aaa}
.day-slot.target .swap-btn{background:#ff6b35;border-color:#ff6b35;color:#fff}

/* Candidates */
.cand-label{font-size:11px;color:#444;font-weight:600;margin-bottom:6px;
            display:flex;align-items:center;gap:8px}
.cand-hint{font-size:11px;color:#ff6b35;font-weight:400}
.candidates-scroll{display:grid;grid-auto-flow:column;grid-auto-columns:130px;
                   gap:8px;overflow-x:auto;padding-bottom:6px}
.candidates-scroll::-webkit-scrollbar{height:4px}
.candidates-scroll::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:2px}
.sched-candidate img,.sched-candidate .no-img{height:65px}
.sched-candidate{cursor:pointer;transition:border-color .12s,transform .12s}
.sched-candidate:hover{border-color:#ff6b35;transform:translateY(-2px)}
.sched-candidate.used{opacity:.3;pointer-events:none}

/* no-plan */
.no-plan{text-align:center;padding:40px;color:#333}
.no-plan code{background:#161616;border:1px solid #222;border-radius:4px;
              padding:2px 7px;font-size:13px;color:#666}

/* Forms */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.form-grid .full{grid-column:1/-1}
label{font-size:11px;color:#666;display:block;margin-bottom:4px}
input,select{width:100%;background:#0a0a0a;border:1px solid #2a2a2a;border-radius:6px;
             color:#e0e0e0;padding:8px 10px;font-size:13px;outline:none}
input:focus,select:focus{border-color:#ff6b35}
input::placeholder{color:#444}
.actions{display:flex;gap:8px;margin-top:14px;align-items:center;flex-wrap:wrap}

/* Buttons */
.btn{padding:7px 14px;border:none;border-radius:6px;font-size:12px;font-weight:600;
     cursor:pointer;transition:opacity .15s}
.btn:hover{opacity:.85}
.btn:disabled{opacity:.3;cursor:not-allowed}
.btn-orange{background:#ff6b35;color:#fff}
.btn-green{background:#16a34a;color:#fff}
.btn-ghost{background:#1e1e1e;color:#aaa}
.btn-red{background:#7f1d1d;color:#fca5a5}
.btn-air{background:#166534;color:#4ade80;font-weight:700}
.btn-xs{padding:4px 10px;font-size:11px}
.btn-xxs{padding:3px 7px;font-size:10px}

/* Validation */
#val-results{margin-top:12px;display:none}
.chk{display:flex;gap:8px;align-items:baseline;padding:4px 0;border-bottom:1px solid #161616;font-size:12px}
.chk:last-child{border-bottom:none}
.chk-name{color:#666;min-width:120px;flex-shrink:0}
.chk-val{word-break:break-all}
.ok{color:#4ade80}.fail{color:#f87171}.warn{color:#fbbf24}
.banner{border-radius:6px;padding:8px 12px;margin-top:10px;font-size:13px;font-weight:600}
.banner-ok{background:#14532d;border:1px solid #22c55e;color:#86efac}
.banner-fail{background:#450a0a;border:1px solid #ef4444;color:#fca5a5}

/* Site table */
.pool-label{font-size:11px;font-weight:700;color:#333;text-transform:uppercase;
            letter-spacing:.06em;padding:8px 0 4px;margin-top:6px}
table{width:100%;border-collapse:collapse}
th{font-size:10px;color:#444;text-align:left;padding:4px 8px;text-transform:uppercase;letter-spacing:.05em}
td{padding:6px 8px;border-bottom:1px solid #161616;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#141414}
.t-name{font-weight:600;color:#ddd;font-size:13px}
.t-url{color:#60a5fa;font-size:11px;text-decoration:none}
.t-url:hover{text-decoration:underline}
.cuisine-tag{background:#1e293b;color:#94a3b8;font-size:10px;padding:2px 7px;border-radius:10px}
.t-idx{color:#333;font-size:11px;width:28px}
.card.dup{border:2px solid #ef4444!important;box-shadow:0 0 6px rgba(239,68,68,.4)}
.spin{display:inline-block;width:13px;height:13px;border:2px solid #333;
      border-top-color:#ff6b35;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)} }
</style>
</head>
<body>

<header>
  <h1>CraveBox Site Manager</h1>
  <span class="badge badge-orange">{{ total }} sites</span>
  <span class="badge badge-gray">{{ pools }} pools</span>
  <span style="margin-left:auto;font-size:11px;color:#444" id="hdr-now"></span>
</header>
<div style="background:#0a0a0a;border-bottom:1px solid #1a1a1a;padding:8px 24px;font-size:11px;color:#888;line-height:1.7">
  <span style="color:#ff6b35;font-weight:700">Quick Guide:</span>
  &nbsp;
  <span><b style="color:#ccc">site_manager.py</b> — manage weekly schedule: add/swap sites, pick recipes, refresh candidates (↺), push live (▶ Now)</span>
  &nbsp;·&nbsp;
  <span><b style="color:#ccc">plan.py</b> — auto-generate new weeks from RSS feeds, skipping already-used sites &amp; recipes. Run: <code style="color:#86efac">python plan.py --append --weeks 13</code> to add weeks after the last one &mdash; <b style="color:#f87171">⚠ without --append it OVERWRITES the schedule!</b></span>
  &nbsp;·&nbsp;
  <span><b style="color:#ccc">rotate.py</b> — runs via GitHub Actions cron every hour, writes featured.json, applies fallbacks if a site or recipe is down</span>
  &nbsp;·&nbsp;
  <span><b style="color:#ccc">After deleting a week</b> — run <code style="color:#86efac">python resequence.py</code> to fix all dates downstream, then push</span>
</div>
<div style="background:#0d1f0d;border-bottom:1px solid #1a3a1a;padding:7px 24px;
            display:flex;align-items:center;gap:10px;font-size:11px;flex-wrap:wrap">
  <span style="color:#4ade80;font-weight:700">▲ Push to GitHub:</span>
  <code id="git-cmd" style="background:#0a150a;border:1px solid #1e3a1e;border-radius:4px;
       padding:4px 10px;color:#86efac;cursor:pointer;user-select:all"
       onclick="copyGit()" title="Click to copy">
    git add data/featured.json data/schedule.json; git commit -m "update featured live"; git push
  </code>
  <button onclick="copyGit()" style="background:#166534;border:none;border-radius:4px;
          color:#4ade80;font-size:11px;font-weight:700;padding:4px 10px;cursor:pointer">
    Copy
  </button>
  <span id="git-copied" style="color:#4ade80;display:none">✓ Copied!</span>
</div>
<script>
function copyGit() {
  const cmd = 'git add data/featured.json data/schedule.json; git commit -m "update featured live"; git push';
  navigator.clipboard.writeText(cmd).then(() => {
    const el = document.getElementById('git-copied');
    el.style.display = 'inline';
    setTimeout(() => el.style.display = 'none', 2000);
  });
}
</script>

<div class="wrap">

  <!-- LIVE NOW -->
  <div class="panel">
    <div class="panel-title">
      <span class="now-dot"></span> Live Now
      <span id="now-updated-lbl"></span>
    </div>
    <div id="now-sites" class="now-sites-row">
      <div style="color:#444;font-size:12px;grid-column:1/-1">Loading…</div>
    </div>
    <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
                color:#444;margin-bottom:6px">Recipes of the Week</div>
    <div id="now-recipes" class="now-recipes-row">
      <div style="color:#444;font-size:12px;grid-column:1/-1">—</div>
    </div>
  </div>

  <!-- 4-WEEK SCHEDULE -->
  <div class="panel">
    <div class="panel-title">
      Schedule
      <span>ON AIR = currently live · Swap ⇄ on site or recipe · click card to open</span>
      <span id="history-lbl" style="font-size:11px;color:#333;margin-left:auto"></span>
      <button class="btn btn-ghost btn-xs" onclick="cleanUpPast()" id="btn-cleanup" style="display:none">🗑 Clean Up Past</button>
      <button class="btn btn-ghost btn-xs" onclick="loadPlan()">↺ Refresh</button>
    </div>
    <div id="schedule-view">
      <div style="color:#444;font-size:12px">Loading…</div>
    </div>
  </div>

  <!-- ADD NEW SITE -->
  <div class="panel">
    <div class="panel-title">Add New Site</div>
    <div class="form-grid">
      <div class="full"><label>Site URL *</label>
        <input id="f-url" type="url" placeholder="https://example.com"></div>
      <div><label>Display Name *</label>
        <input id="f-name" type="text" placeholder="My Recipe Blog"></div>
      <div><label>Cuisine * <span style="color:#444">(short tag)</span></label>
        <input id="f-cuisine" type="text" placeholder="American"></div>
      <div class="full"><label>Tagline *</label>
        <input id="f-tagline" type="text" placeholder="One-line description"></div>
      <div class="full"><label>RSS URL <span style="color:#444">(leave blank to auto-detect)</span></label>
        <input id="f-rss" type="url" placeholder="https://example.com/feed/"></div>
    </div>
    <div class="actions">
      <button class="btn btn-orange" onclick="runValidate()">Validate</button>
      <button class="btn btn-green" id="btn-add" disabled onclick="addSite()">Add to Master</button>
      <button class="btn btn-ghost" onclick="clearForm()">Clear</button>
      <span id="val-spinner" style="display:none"><span class="spin"></span> Validating…</span>
    </div>
    <div id="val-results"></div>
  </div>

  <!-- SITE LIST -->
  <div class="panel">
    <div class="panel-title" style="margin-bottom:10px">
      All Sites <span>{{ total }} sites · {{ pools }} pools of 4</span>
    </div>
    {% for pool_idx, pool in pools_data %}
    <div>
      <div class="pool-label">Pool {{ pool_idx + 1 }}</div>
      <table>
        <thead><tr><th>#</th><th>Name</th><th>Cuisine</th><th>URL</th><th></th></tr></thead>
        <tbody>
          {% for site in pool %}
          <tr>
            <td class="t-idx">{{ site.idx + 1 }}</td>
            <td class="t-name">{{ site.name }}</td>
            <td><span class="cuisine-tag">{{ site.cuisine }}</span></td>
            <td><a class="t-url" href="{{ site.url }}" target="_blank">{{ site.url }}</a></td>
            <td><button class="btn btn-red btn-xxs" onclick="removeSite({{ site.idx }},'{{ site.name|e }}')">Remove</button></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% endfor %}
  </div>

</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let planData      = [];
let currentSlot   = 0;
let liveWeekNum   = null;   // week number currently in featured.json
let swapTarget    = null;   // { weekIdx, day }
let siteSwap      = null;   // { weekIdx, siteIdx }
const DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
const MASTER_SITES = {{ all_sites_json | safe }};

// ── Init ───────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  updateClock(); setInterval(updateClock, 10000);
  loadNow();
  loadPlan();
});
function updateClock() {
  document.getElementById('hdr-now').textContent =
    new Date().toUTCString().replace(' GMT','') + ' UTC';
}
function openUrl(url) { if (url) window.open(url, '_blank'); }

// ── Card helpers ───────────────────────────────────────────────────────────
function imgOrEmpty(url, h) {
  const src = url ? `/proxy-image?url=${encodeURIComponent(url)}` : '';
  return src
    ? `<img src="${src}" style="height:${h}" onerror="this.style.display='none'">`
    : `<div class="no-img" style="height:${h}">no img</div>`;
}
function siteCard(s, cls='', h='55px') {
  return `<div class="card clickable ${cls}" onclick="openUrl('${s.url||''}')">
    ${imgOrEmpty(s.image_url, h)}
    <div class="ci">
      <div class="cn" title="${s.name}">${s.name}</div>
      <div class="cs">${s.cuisine||''}</div>
    </div></div>`;
}
function recipeCard(r, cls='', h='70px') {
  const url = r.url||r.recipe_url||'';
  return `<div class="card clickable ${cls}" onclick="openUrl('${url}')">
    ${imgOrEmpty(r.image_url, h)}
    <div class="ci">
      <div class="cn wrap2">${r.title||'—'}</div>
      <div class="cs">${r.site_name||''}</div>
    </div></div>`;
}
function emptyCard(h='70px') {
  return `<div class="card" style="opacity:.25">
    <div class="no-img" style="height:${h}">empty</div>
    <div class="ci"><div class="cn">—</div></div></div>`;
}

// ── Live Now ───────────────────────────────────────────────────────────────
async function loadNow() {
  const res  = await fetch('/now');
  const data = await res.json();
  if (!data.ok) {
    document.getElementById('now-sites').innerHTML =
      '<div style="color:#555;font-size:12px;grid-column:1/-1">featured.json not found — run rotate.py first.</div>';
    document.getElementById('now-recipes').innerHTML = '';
    return;
  }
  const f = data.featured;
  liveWeekNum = f.week || null;
  // Re-render schedule badges if plan already loaded
  if (planData.length) renderPlan();
  document.getElementById('now-updated-lbl').textContent = 'Updated ' + f.updated_at;
  document.getElementById('now-sites').innerHTML =
    (f.featured_sites||[]).map(s => siteCard(s,'now-site','75px')).join('');
  // Build recipes dict — support both new (featured_recipes dict) and old (featured_recipe single)
  let recipesDict = {};
  if (f.featured_recipes && typeof f.featured_recipes === 'object' && !Array.isArray(f.featured_recipes)) {
    recipesDict = f.featured_recipes;
  } else if (f.featured_recipe) {
    // old format: single recipe — show under Mon as placeholder
    recipesDict = { Mon: f.featured_recipe };
  }
  const hasAny = Object.values(recipesDict).some(r => r);
  document.getElementById('now-recipes').innerHTML = hasAny
    ? DAYS.map(d => {
        const r = recipesDict[d];
        return `<div>
          <div style="font-size:9px;font-weight:700;text-transform:uppercase;
               letter-spacing:.05em;color:#444;margin-bottom:3px;text-align:center">${d}</div>
          ${r ? recipeCard(r,'now-recipe','85px') : emptyCard('85px')}
        </div>`;
      }).join('')
    : '<div style="color:#444;font-size:12px;grid-column:1/-1">No recipes yet</div>';
}

// ── Schedule ───────────────────────────────────────────────────────────────
async function loadPlan() {
  const [pr, cr] = await Promise.all([fetch('/plan'), fetch('/plan/current-slot')]);
  const pd = await pr.json();
  const cd = await cr.json();
  currentSlot = cd.slot_idx || 0;
  const el = document.getElementById('schedule-view');
  if (!pd.ok) {
    el.innerHTML = `<div class="no-plan">
      <p style="margin-bottom:10px;color:#555">No schedule yet.</p>
      <p>Run <code>python plan.py</code> to generate the 4-week schedule, then refresh.</p>
    </div>`;
    return;
  }
  planData = pd.schedule;
  swapTarget = null; siteSwap = null;
  renderPlan();
}

function updateCleanupBtn() {
  const liveIdx = liveWeekNum !== null
    ? planData.findIndex(w => w && w.week === liveWeekNum)
    : currentSlot;
  const past = planData.filter((_,i) => i < liveIdx).length;
  const lbl  = document.getElementById('history-lbl');
  const btn  = document.getElementById('btn-cleanup');
  if (!lbl || !btn) return;
  if (past > 0) {
    lbl.textContent = `${past} aired week${past>1?'s':''} · ${planData.length - currentSlot} remaining`;
    btn.style.display = 'inline-block';
  } else {
    lbl.textContent = `${planData.length - currentSlot} week${planData.length-currentSlot!==1?'s':''} remaining`;
    btn.style.display = 'none';
  }
}

async function cleanUpPast() {
  const past = planData.filter((_,i) => i < currentSlot).length;
  if (!confirm(`Archive and remove ${past} aired week${past>1?'s':''} from schedule?\nThey'll be saved to used_history.json so plan.py can skip them.`)) return;
  const res = await fetch('/plan/cleanup', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({})
  });
  const data = await res.json();
  if (data.ok) {
    planData    = data.schedule;
    currentSlot = data.current_slot;
    swapTarget  = null; siteSwap = null;
    renderPlan();
    alert(`✓ Archived ${data.removed} week${data.removed!==1?'s':''}. History now has ${data.history_count} recipe URLs tracked.`);
  } else {
    alert('Cleanup failed: ' + data.error);
  }
}

function buildDupSets() {
  const recipeCount = {}, siteCount = {};
  planData.forEach(week => {
    if (!week) return;
    (week.sites||[]).forEach(s => {
      const k = s.name||''; if (k) siteCount[k] = (siteCount[k]||0)+1;
    });
    Object.values(week.recipes||{}).forEach(r => {
      if (!r) return;
      const k = r.url||r.recipe_url||''; if (k) recipeCount[k] = (recipeCount[k]||0)+1;
    });
  });
  return {
    dupRecipes: new Set(Object.keys(recipeCount).filter(k => recipeCount[k] > 1)),
    dupSites:   new Set(Object.keys(siteCount).filter(k => siteCount[k] > 1)),
  };
}

function renderPlan() {
  const { dupRecipes, dupSites } = buildDupSets();
  let html = insertBar(-1);
  planData.forEach((week, wi) => {
    html += renderWeek(week, wi, dupRecipes, dupSites);
    html += insertBar(wi);
  });
  document.getElementById('schedule-view').innerHTML = html;
  updateCleanupBtn();
}

function insertBar(afterIdx) {
  return `<div class="insert-bar" onclick="insertWeek(${afterIdx})">
    <div class="ib-line"></div>
    <div class="ib-btn">+ Insert Week</div>
    <div class="ib-line"></div>
  </div>`;
}

function renderWeek(week, wi, dupRecipes=new Set(), dupSites=new Set()) {
  if (!week) return '';
  const isLive   = liveWeekNum !== null ? week.week === liveWeekNum : wi === currentSlot;
  const liveIdx  = liveWeekNum !== null
    ? planData.findIndex(w => w && w.week === liveWeekNum)
    : currentSlot;
  const isNextUp = wi === liveIdx + 1;
  const isActive = isLive;
  const start = new Date(week.starts.length === 10 ? week.starts + 'T00:00:00Z' : week.starts);
  const end   = new Date(start); end.setDate(end.getDate()+6);
  const fmt   = d => d.toLocaleDateString('en-US',{month:'short',day:'numeric'});
  const usedUrls = new Set(
    Object.values(week.recipes||{}).filter(Boolean).map(r=>r.url||r.recipe_url||'')
  );

  // Sites
  const sitesHtml = (week.sites||[]).map((s,si) => {
    const isSwapping = siteSwap && siteSwap.weekIdx===wi && siteSwap.siteIdx===si;
    const isDupSite  = dupSites.has(s.name||'');
    const picker = isSwapping
      ? `<select class="site-picker" onchange="pickSite(${wi},${si},this.value)" onclick="event.stopPropagation()">
          <option value="">— pick a site —</option>
          ${MASTER_SITES.map(ms=>`<option value="${ms.name}"${ms.name===s.name?' selected':''}>${ms.name} (${ms.cuisine})</option>`).join('')}
        </select>` : '';
    return `<div class="site-wrap">
      ${siteCard(s,'sched-site'+(isDupSite?' dup':''),'55px')}
      <button class="site-swap-btn${isSwapping?' active':''}"
        onclick="toggleSiteSwap(${wi},${si})">⇄</button>
      ${picker}
    </div>`;
  }).join('');

  // Day slots
  const daysHtml = DAYS.map(day => {
    const r = (week.recipes||{})[day];
    const isTarget  = swapTarget && swapTarget.weekIdx===wi && swapTarget.day===day;
    const isDupRecipe = r && dupRecipes.has(r.url||r.recipe_url||'');
    return `<div class="day-col">
      <div class="day-label">${day}</div>
      <div class="day-slot${isTarget?' target':''}" id="ds-${wi}-${day}">
        ${r ? recipeCard(r,'sched-recipe'+(isDupRecipe?' dup':''),'70px') : emptyCard()}
        <button class="swap-btn" onclick="toggleSwap(${wi},'${day}')">
          ${isTarget ? '✕' : '⇄'}
        </button>
      </div>
    </div>`;
  }).join('');

  // Candidates
  const cands = week.candidates||[];
  const candsHtml = cands.length
    ? cands.map((r,ci) => {
        const used = usedUrls.has(r.url||r.recipe_url||'');
        return `<div class="card sched-candidate${used?' used':''}"
          onclick="assignCandidate(${wi},${ci})" title="${r.title}">
          ${imgOrEmpty(r.image_url,'65px')}
          <div class="ci">
            <div class="cn wrap2">${r.title||'—'}</div>
            <div class="cs">${r.site_name||''}</div>
          </div></div>`;
      }).join('')
    : '<div style="color:#333;font-size:12px">No candidates — regenerate week.</div>';

  const hint = swapTarget && swapTarget.weekIdx===wi
    ? `<span class="cand-hint">← pick a replacement for ${swapTarget.day}</span>` : '';

  return `<div class="week-block${isActive?' on-air':''}" id="week-${wi}">
    <div class="week-header">
      ${isLive   ? '<span class="badge badge-air">● LIVE</span>' : ''}
      ${isNextUp ? '<span class="badge badge-blue" style="background:#1e3a5f;color:#93c5fd">⏭ NEXT UP</span>' : ''}
      <div class="week-title">Week ${week.week}</div>
      <div class="week-dates">${fmt(start)} – ${fmt(end)}</div>
      <span class="badge badge-blue">Pool ${week.pool}</span>
      <span style="font-size:10px;color:#333">gen ${(week.generated_at||'').slice(0,10)}</span>
      <div style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn btn-ghost btn-xxs" onclick="moveWeek(${wi},'up')" ${wi===0?'disabled':''}>↑</button>
        <button class="btn btn-ghost btn-xxs" onclick="moveWeek(${wi},'down')" ${wi===planData.length-1?'disabled':''}>↓</button>
        <button class="btn btn-ghost btn-xxs" onclick="regenerateWeek(${week.week},${wi},'${week.starts||''}')">↺ Regen</button>
        <button class="btn btn-red btn-xxs" onclick="deleteWeek(${wi})">Delete</button>
        <button class="btn btn-ghost btn-xxs" title="Insert before current live slot" onclick="queueWeek(${wi},'before')">⏮ Before</button>
        <button class="btn btn-ghost btn-xxs" title="Insert after current live slot"  onclick="queueWeek(${wi},'after')">⏭ Next</button>
        <button class="btn btn-air btn-xs"   title="Go live immediately"              onclick="pushLive(${wi})">▶ Now</button>
      </div>
    </div>
    <div class="week-body">
      <div class="week-sites">${sitesHtml}</div>
      <div class="week-days">${daysHtml}</div>
      <div class="cand-label">Candidates (${cands.length}) ${hint}
        <button onclick="refreshCandidates(${wi})" title="Re-fetch from current sites"
          style="background:#111;border:1px solid #2a2a2a;border-radius:4px;color:#555;
                 font-size:10px;padding:2px 7px;cursor:pointer;margin-left:auto">↺ Refresh</button>
      </div>
      <div class="candidates-scroll" id="cands-${wi}">${candsHtml}</div>
    </div>
  </div>`;
}

// ── Recipe swap ────────────────────────────────────────────────────────────
function toggleSwap(weekIdx, day) {
  swapTarget = (swapTarget && swapTarget.weekIdx===weekIdx && swapTarget.day===day)
    ? null : { weekIdx, day };
  siteSwap = null;
  renderPlan();
  if (swapTarget) document.getElementById(`cands-${weekIdx}`)
    ?.scrollIntoView({behavior:'smooth',block:'nearest'});
}

async function assignCandidate(weekIdx, candidateIdx) {
  if (!swapTarget || swapTarget.weekIdx !== weekIdx) {
    alert('Click ⇄ on a day slot first, then pick a candidate.');
    return;
  }
  const recipe = planData[weekIdx].candidates[candidateIdx];
  const day    = swapTarget.day;
  planData[weekIdx].recipes[day] = recipe;
  swapTarget = null;
  renderPlan();
  const res = await fetch('/plan/assign', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ week_idx: weekIdx, day, recipe })
  });
  const data = await res.json();
  if (!data.ok) alert('Save failed: ' + data.error);
}

// ── Site swap ──────────────────────────────────────────────────────────────
function toggleSiteSwap(weekIdx, siteIdx) {
  siteSwap = (siteSwap && siteSwap.weekIdx===weekIdx && siteSwap.siteIdx===siteIdx)
    ? null : { weekIdx, siteIdx };
  swapTarget = null;
  renderPlan();
}

async function pickSite(weekIdx, siteIdx, siteName) {
  if (!siteName) return;
  const newSite = MASTER_SITES.find(s => s.name === siteName);
  if (!newSite) return;
  planData[weekIdx].sites[siteIdx] = newSite;
  siteSwap = null;
  renderPlan();
  const res = await fetch('/plan/update-site', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ week_idx: weekIdx, site_idx: siteIdx, site_name: siteName })
  });
  const data = await res.json();
  if (!data.ok) { alert('Save failed: ' + data.error); return; }
  // Refresh candidates from the updated site mix
  const cEl = document.getElementById(`cands-${weekIdx}`);
  if (cEl) cEl.innerHTML = '<div style="color:#555;font-size:12px;padding:8px">Refreshing candidates…</div>';
  await refreshCandidates(weekIdx);
}

async function refreshCandidates(weekIdx) {
  const r = await fetch('/plan/refresh-candidates', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ week_idx: weekIdx })
  });
  const d = await r.json();
  if (d.ok) {
    planData[weekIdx].candidates = d.candidates;
    renderPlan();
  } else {
    alert('Candidate refresh failed: ' + d.error);
  }
}

// ── Queue Before / Next ────────────────────────────────────────────────────
async function queueWeek(weekIdx, position) {
  const week  = planData[weekIdx];
  const label = `Week ${week.week}`;
  const where = position === 'before' ? 'before current live slot' : 'as next up after current';
  if (!confirm(`Queue ${label} ${where}?`)) return;
  const res = await fetch('/plan/queue', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ week_idx: weekIdx, position })
  });
  const data = await res.json();
  if (data.ok) {
    planData = data.schedule;
    currentSlot = data.current_slot;
    swapTarget = null; siteSwap = null;
    renderPlan();
  } else {
    alert('Queue failed: ' + data.error);
  }
}

// ── Push Live ──────────────────────────────────────────────────────────────
async function pushLive(weekIdx) {
  const week = planData[weekIdx];
  const label = `Week ${week.week} (${week.starts})`;
  if (!confirm(`Push ${label} live now?\nThis overwrites featured.json immediately.`)) return;
  const res = await fetch('/plan/push-live', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ week_idx: weekIdx })
  });
  const data = await res.json();
  if (data.ok) {
    currentSlot  = weekIdx;
    liveWeekNum  = planData[weekIdx].week;
    renderPlan();
    loadNow();
    alert(`✓ ${label} is now live.`);
  } else {
    alert('Push failed: ' + data.error);
  }
}

// ── Week management ────────────────────────────────────────────────────────
async function insertWeek(afterIdx) {
  const res = await fetch('/plan/insert', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ after_idx: afterIdx, source: 'duplicate' })
  });
  const data = await res.json();
  if (data.ok) { planData = data.schedule; swapTarget=null; siteSwap=null; renderPlan(); }
  else alert('Insert failed: ' + data.error);
}

async function deleteWeek(weekIdx) {
  const week = planData[weekIdx];
  if (!confirm(`Delete Week ${week.week}? This cannot be undone.`)) return;
  const res = await fetch('/plan/delete', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ week_idx: weekIdx })
  });
  const data = await res.json();
  if (data.ok) { planData = data.schedule; swapTarget=null; siteSwap=null; renderPlan(); }
  else alert('Delete failed: ' + data.error);
}

async function moveWeek(weekIdx, direction) {
  const res = await fetch('/plan/move', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ week_idx: weekIdx, direction })
  });
  const data = await res.json();
  if (data.ok) { planData = data.schedule; swapTarget=null; siteSwap=null; renderPlan(); }
  else alert('Move failed: ' + data.error);
}

async function regenerateWeek(weekNum, weekIdx, starts) {
  if (!confirm(`Regenerate Week ${weekNum}? Re-fetches RSS + validates images (1-3 min).`)) return;
  const btn = event.target;
  btn.textContent='…'; btn.disabled=true;
  const res = await fetch('/plan/regenerate', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ week: weekNum, week_idx: weekIdx, starts })
  });
  btn.textContent='↺ Regen'; btn.disabled=false;
  const data = await res.json();
  if (!data.ok) { alert('Regenerate failed:\n' + data.error); return; }
  planData[weekIdx] = data.week;
  swapTarget=null; siteSwap=null;
  renderPlan();
}

// ── Add New Site ───────────────────────────────────────────────────────────
let validationPassed = false;
async function runValidate() {
  const url = document.getElementById('f-url').value.trim();
  if (!url) { alert('URL is required'); return; }
  validationPassed = false;
  document.getElementById('btn-add').disabled = true;
  document.getElementById('val-spinner').style.display = 'inline';
  document.getElementById('val-results').style.display = 'none';
  try {
    const res = await fetch('/validate', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ url, name: document.getElementById('f-name').value.trim(),
                             rss_url: document.getElementById('f-rss').value.trim() })
    });
    showValidation(await res.json());
  } finally { document.getElementById('val-spinner').style.display = 'none'; }
}
function iconCls(v) { return v.startsWith('✓')?'ok':v.startsWith('✗')?'fail':'warn'; }
function showValidation(data) {
  validationPassed = data.passed;
  let html = '';
  for (const [k,v] of Object.entries(data.checks))
    html += `<div class="chk"><span class="chk-name">${k}</span>
             <span class="chk-val ${iconCls(v)}">${v}</span></div>`;
  html += data.passed
    ? '<div class="banner banner-ok">✓ All checks passed — ready to add</div>'
    : '<div class="banner banner-fail">✗ Validation failed</div>';
  const el = document.getElementById('val-results');
  el.innerHTML = html; el.style.display = 'block';
  document.getElementById('btn-add').disabled = !data.passed;
}
async function addSite() {
  if (!validationPassed) return;
  const name=document.getElementById('f-name').value.trim(),
        cuisine=document.getElementById('f-cuisine').value.trim(),
        tagline=document.getElementById('f-tagline').value.trim(),
        url=document.getElementById('f-url').value.trim(),
        rss_url=document.getElementById('f-rss').value.trim();
  if (!name||!cuisine||!tagline) { alert('Name, Cuisine, and Tagline required.'); return; }
  const res = await fetch('/add', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name, url, cuisine, tagline, rss_url })
  });
  const data = await res.json();
  if (data.ok) { alert(`✓ "${name}" added! Total: ${data.total} sites (${data.pools} pools)`); location.reload(); }
  else alert('Error: ' + data.error);
}
function clearForm() {
  ['f-url','f-name','f-cuisine','f-tagline','f-rss'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('val-results').style.display='none';
  document.getElementById('btn-add').disabled=true;
  validationPassed=false;
}

// ── Remove Site ────────────────────────────────────────────────────────────
async function removeSite(idx, name) {
  if (!confirm(`Remove "${name}"?`)) return;
  const res = await fetch('/remove', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ idx })
  });
  const data = await res.json();
  if (data.ok) { alert(`Removed. Total: ${data.total}`); location.reload(); }
  else alert('Error: ' + data.error);
}
</script>
</body>
</html>"""

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/proxy-image")
def proxy_image():
    """Fetch an image server-side (with its own domain as Referer) to bypass hotlink protection."""
    from urllib.parse import urlparse
    url = request.args.get("url", "")
    if not url or not url.startswith("http"):
        return "", 404
    try:
        parsed  = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        r = req.get(url, timeout=10, stream=True,
                    headers={**HEADERS, "Referer": referer, "Accept": "image/*"})
        if r.status_code != 200:
            return "", 404
        ct = r.headers.get("Content-Type", "image/jpeg")
        return Response(r.iter_content(8192), content_type=ct)
    except Exception:
        return "", 404


@app.route("/")
def index():
    sites      = load_master()
    total      = len(sites)
    pools      = pool_count(total)
    indexed    = [{"idx": i, **s} for i, s in enumerate(sites)]
    num_p      = pools
    pools_data = [(p, indexed[p*4:p*4+4]) for p in range(num_p) if indexed[p*4:p*4+4]]
    remainder  = indexed[num_p*4:]
    if remainder:
        pools_data.append((num_p, remainder))
    all_sites_json = json.dumps(sites, ensure_ascii=False)
    return render_template_string(PAGE, total=total, pools=pools,
                                  pools_data=pools_data,
                                  all_sites_json=all_sites_json)


@app.route("/now")
def now():
    f = load_featured()
    if not f:
        return jsonify({"ok": False})
    return jsonify({"ok": True, "featured": f})


@app.route("/plan")
def get_plan():
    sched = load_schedule()
    if sched is None:
        return jsonify({"ok": False})
    return jsonify({"ok": True, "schedule": sched})


@app.route("/plan/current-slot")
def get_current_slot():
    sched = load_schedule()
    if not sched:
        return jsonify({"ok": True, "slot_idx": 0})
    return jsonify({"ok": True, "slot_idx": current_slot_idx(sched)})


@app.route("/plan/assign", methods=["POST"])
def plan_assign():
    data     = request.get_json()
    week_idx = data.get("week_idx")
    day      = data.get("day")
    recipe   = data.get("recipe")
    sched    = load_schedule()
    if sched is None or week_idx is None or week_idx >= len(sched):
        return jsonify({"ok": False, "error": "invalid week index"})
    if day not in DAYS:
        return jsonify({"ok": False, "error": "invalid day"})
    sched[week_idx]["recipes"][day] = recipe
    save_schedule(sched)
    return jsonify({"ok": True})


@app.route("/plan/update-site", methods=["POST"])
def plan_update_site():
    data      = request.get_json()
    week_idx  = data.get("week_idx")
    site_idx  = data.get("site_idx")
    site_name = data.get("site_name")
    sched     = load_schedule()
    master    = load_master()
    by_name   = {s["name"]: s for s in master}
    if sched is None or week_idx is None or week_idx >= len(sched):
        return jsonify({"ok": False, "error": "invalid week"})
    if site_name not in by_name:
        return jsonify({"ok": False, "error": "site not found"})
    sites = sched[week_idx].get("sites", [])
    if site_idx is None or site_idx < 0 or site_idx >= len(sites):
        return jsonify({"ok": False, "error": "invalid site index"})
    sites[site_idx] = by_name[site_name]
    save_schedule(sched)
    return jsonify({"ok": True})


@app.route("/plan/refresh-candidates", methods=["POST"])
def plan_refresh_candidates():
    data     = request.get_json()
    week_idx = data.get("week_idx")
    sched    = load_schedule()
    if sched is None or week_idx is None or week_idx >= len(sched):
        return jsonify({"ok": False, "error": "invalid week"})
    # Always use fresh site data from master_sites.json (rss_url may have changed)
    master  = load_master()
    by_name = {s["name"]: s for s in master}
    sites   = [by_name.get(s["name"], s) for s in sched[week_idx].get("sites", [])]
    candidates = []
    for site in sites:
        if not site.get("rss_url") and site.get("scrape_url") and scrape_recipe_listing:
            candidates.extend(scrape_recipe_listing(site))
        else:
            candidates.extend(fetch_rss_candidates(site))
    sched[week_idx]["candidates"] = candidates
    save_schedule(sched)
    return jsonify({"ok": True, "candidates": candidates})


@app.route("/plan/push-live", methods=["POST"])
def plan_push_live():
    data     = request.get_json()
    week_idx = data.get("week_idx")
    sched    = load_schedule()
    if sched is None or week_idx is None or week_idx >= len(sched):
        return jsonify({"ok": False, "error": "invalid week"})
    entry    = sched[week_idx]
    now      = datetime.now(timezone.utc)
    recipes  = entry.get("recipes") or {}
    today    = DAYS[now.weekday()]
    today_r  = recipes.get(today) or next((r for r in recipes.values() if r), None)
    output   = {
        "updated_at":       now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slot_hours":       SLOT_HOURS,
        "slot_idx":         week_idx,
        "week":             entry.get("week"),
        "featured_sites":   entry.get("sites", []),
        "featured_recipes": recipes,
        "featured_recipe":  today_r,
    }
    with open(FEATURED_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    return jsonify({"ok": True})


@app.route("/plan/insert", methods=["POST"])
def plan_insert():
    data      = request.get_json()
    after_idx = data.get("after_idx", -1)
    sched     = load_schedule() or []
    if after_idx >= 0 and after_idx < len(sched):
        new_entry = json.loads(json.dumps(sched[after_idx]))   # deep copy
        new_entry["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        new_entry = {
            "week": 0, "starts": "", "pool": 1,
            "sites": [],
            "recipes": {d: None for d in DAYS},
            "candidates": [],
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    sched.insert(after_idx + 1, new_entry)
    save_schedule(renumber(sched))
    return jsonify({"ok": True, "schedule": sched})


@app.route("/plan/delete", methods=["POST"])
def plan_delete():
    data     = request.get_json()
    week_idx = data.get("week_idx")
    sched    = load_schedule() or []
    if week_idx is None or week_idx < 0 or week_idx >= len(sched):
        return jsonify({"ok": False, "error": "invalid index"})
    sched.pop(week_idx)
    save_schedule(renumber(sched))
    return jsonify({"ok": True, "schedule": sched})


@app.route("/plan/move", methods=["POST"])
def plan_move():
    data      = request.get_json()
    week_idx  = data.get("week_idx")
    direction = data.get("direction")
    sched     = load_schedule() or []
    if direction == "up" and week_idx and week_idx > 0:
        sched[week_idx], sched[week_idx-1] = sched[week_idx-1], sched[week_idx]
    elif direction == "down" and week_idx is not None and week_idx < len(sched)-1:
        sched[week_idx], sched[week_idx+1] = sched[week_idx+1], sched[week_idx]
    else:
        return jsonify({"ok": False, "error": "cannot move"})
    save_schedule(renumber(sched))
    return jsonify({"ok": True, "schedule": sched})


@app.route("/plan/queue", methods=["POST"])
def plan_queue():
    data     = request.get_json()
    week_idx = data.get("week_idx")
    position = data.get("position")   # 'before' | 'after'
    sched    = load_schedule() or []
    if week_idx is None or week_idx < 0 or week_idx >= len(sched):
        return jsonify({"ok": False, "error": "invalid week index"})

    cur = current_slot_idx(sched)

    # Pull the week out, then reinsert at target position
    week = sched.pop(week_idx)

    # Recalculate cur after removal (if week was before cur, cur shifts)
    if week_idx < cur:
        cur -= 1

    if position == "before":
        target = max(cur, 0)          # slot before current live
    else:
        target = min(cur + 1, len(sched))   # slot right after current live

    sched.insert(target, week)
    save_schedule(renumber(sched))
    new_cur = current_slot_idx(sched)
    return jsonify({"ok": True, "schedule": sched, "current_slot": new_cur})


@app.route("/plan/cleanup", methods=["POST"])
def plan_cleanup():
    sched = load_schedule() or []
    cur   = current_slot_idx(sched)
    if cur == 0:
        return jsonify({"ok": False, "error": "No past weeks to clean up"})
    past   = sched[:cur]
    future = sched[cur:]
    archive_weeks(past)
    save_schedule(renumber(future))
    h = load_history()
    new_cur = current_slot_idx(future)
    return jsonify({
        "ok":            True,
        "removed":       len(past),
        "schedule":      future,
        "current_slot":  new_cur,
        "history_count": len(h["recipe_urls"]),
    })


@app.route("/plan/regenerate", methods=["POST"])
def plan_regenerate():
    data      = request.get_json()
    week_num  = data.get("week")
    week_idx  = data.get("week_idx")
    starts    = data.get("starts", "")
    if week_num is None:
        return jsonify({"ok": False, "error": "week required"})
    # Use --idx + --starts for reliable regeneration regardless of schedule length
    cmd = [sys.executable, str(Path(__file__).parent / "plan.py"),
           "--idx", str(week_idx if week_idx is not None else week_num - 1)]
    if starts:
        cmd += ["--starts", starts]
    try:
        result = subprocess.run(cmd,
            capture_output=True, text=True, timeout=360,
            cwd=str(Path(__file__).parent)
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": (result.stderr or result.stdout)[-600:]})
        sched     = load_schedule()
        week_data = sched[week_num-1] if sched and week_num-1 < len(sched) else None
        return jsonify({"ok": True, "week": week_data, "log": result.stdout[-2000:]})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timed out (>6 min)"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/validate", methods=["POST"])
def validate():
    data    = request.get_json()
    url     = (data.get("url") or "").rstrip("/")
    name    = data.get("name") or url
    rss_url = data.get("rss_url") or ""
    site    = {"name": name, "url": url}
    if rss_url:
        site["rss_url"] = rss_url
    checks = validate_site(site)
    return jsonify({"checks": checks, "passed": passed(checks)})


@app.route("/add", methods=["POST"])
def add_site():
    data    = request.get_json()
    name    = (data.get("name") or "").strip()
    url     = (data.get("url") or "").rstrip("/")
    cuisine = (data.get("cuisine") or "").strip()
    tagline = (data.get("tagline") or "").strip()
    rss_url = (data.get("rss_url") or "").strip()
    if not all([name, url, cuisine, tagline]):
        return jsonify({"ok": False, "error": "name, url, cuisine, tagline all required"})
    site = {"name": name, "url": url}
    if rss_url:
        site["rss_url"] = rss_url
    if not passed(validate_site(site)):
        return jsonify({"ok": False, "error": "Validation failed"})
    sites = load_master()
    if any(s["url"].rstrip("/") == url for s in sites):
        return jsonify({"ok": False, "error": "URL already in master_sites.json"})
    entry = {"name": name, "url": url, "cuisine": cuisine,
             "tagline": tagline, "image_url": ""}
    if rss_url:
        entry["rss_url"] = rss_url
    sites.append(entry)
    save_master(sites)
    total = len(sites)
    return jsonify({"ok": True, "total": total, "pools": pool_count(total)})


@app.route("/remove", methods=["POST"])
def remove_site():
    data  = request.get_json()
    idx   = data.get("idx")
    sites = load_master()
    if idx is None or idx < 0 or idx >= len(sites):
        return jsonify({"ok": False, "error": "invalid index"})
    sites.pop(idx)
    save_master(sites)
    total = len(sites)
    return jsonify({"ok": True, "total": total, "pools": pool_count(total)})


# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("CraveBox Site Manager → http://localhost:5001")
    app.run(port=5001, debug=False)
