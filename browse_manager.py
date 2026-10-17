"""
browse_manager.py — CraveBox Handpicked Vault manager

Manages data/browse_cuisines.json — the site list shown in BrowseCuisinesActivity.
Edit sites here, push to GitHub, and the app picks up changes via OTA.

Run:  python browse_manager.py
Open: http://localhost:5002

Requires: pip install flask requests
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from flask import Flask, jsonify, render_template_string, request, Response
except ImportError:
    print("Flask not found. Run:  python -m pip install flask requests")
    sys.exit(1)

import requests as req
from validate_sites import validate_site, passed

DATA_DIR    = Path(__file__).parent / "data"
BROWSE_PATH = DATA_DIR / "browse_cuisines.json"
HEADERS     = {"User-Agent": "CraveBox-Rotator/1.0"}

app = Flask(__name__)

# ── Data helpers ───────────────────────────────────────────────────────────────

def load_browse():
    if not BROWSE_PATH.exists():
        return []
    with open(BROWSE_PATH, encoding="utf-8") as f:
        return json.load(f)

def save_browse(sites):
    seen = set()
    deduped = [s for s in sites if not (s["url"] in seen or seen.add(s["url"]))]
    with open(BROWSE_PATH, "w", encoding="utf-8") as f:
        json.dump(deduped, f, indent=2, ensure_ascii=False)

def grouped_by_cuisine(sites):
    groups = {}
    for s in sites:
        c = s.get("cuisine") or "International"
        groups.setdefault(c, []).append(s)
    return dict(sorted(groups.items()))

# ── Page HTML ──────────────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CraveBox Browse Manager</title>
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
.wrap{max-width:1400px;margin:0 auto;padding:20px 24px;display:flex;flex-direction:column;gap:20px}
.panel{background:#111;border:1px solid #1e1e1e;border-radius:10px;padding:18px 20px}
.panel-title{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
             color:#555;margin-bottom:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.panel-title span{font-weight:400;text-transform:none;letter-spacing:0;color:#888;font-size:12px}

/* Cuisine groups */
.cuisine-section{margin-bottom:28px}
.cuisine-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.8px;
               color:#555;padding:8px 0 8px;display:flex;align-items:center;gap:8px}
.cuisine-count{color:#333;font-weight:400;letter-spacing:0;text-transform:none}
.site-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}

/* Cards */
.card{background:#0f0f0f;border:1px solid #1e1e1e;border-radius:7px;overflow:hidden;
      display:flex;flex-direction:column;position:relative}
.card img{width:100%;height:80px;object-fit:cover;display:block;background:#1a1a1a}
.card .no-img{width:100%;height:80px;background:#141414;display:flex;
              align-items:center;justify-content:center;color:#2a2a2a;font-size:10px}
.card .ci{padding:6px 8px}
.card .cn{font-size:10px;font-weight:600;color:#ccc;overflow:hidden;
          text-overflow:ellipsis;white-space:nowrap;margin-bottom:1px}
.card .cs{font-size:9px;color:#555}
.card .rm{position:absolute;top:4px;right:4px;background:rgba(10,10,10,.8);
          border:1px solid #333;border-radius:4px;color:#666;font-size:10px;
          padding:2px 6px;cursor:pointer;line-height:1.4}
.card .rm:hover{background:#7f1d1d;border-color:#ef4444;color:#fca5a5}
.card .mv{position:absolute;top:4px;left:4px;display:flex;flex-direction:column;gap:2px}
.card .mv button{background:rgba(10,10,10,.8);border:1px solid #333;border-radius:3px;
                 color:#555;font-size:9px;padding:1px 4px;cursor:pointer;line-height:1.4}
.card .mv button:hover{background:#1e1e1e;color:#aaa}
.card .mv button:disabled{opacity:.2;cursor:not-allowed}

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
.btn-xs{padding:4px 10px;font-size:11px}

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
.spin{display:inline-block;width:13px;height:13px;border:2px solid #333;
      border-top-color:#ff6b35;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}

/* Search */
#search{max-width:300px;margin-bottom:16px}
</style>
</head>
<body>

<header>
  <h1>CraveBox Browse Manager</h1>
  <span class="badge badge-orange" id="hdr-total">{{ total }} sites</span>
  <span class="badge badge-gray" id="hdr-cuisines">{{ cuisine_count }} cuisines</span>
  <span style="margin-left:auto;font-size:11px;color:#444" id="hdr-clock"></span>
</header>

<div style="background:#0a0a0a;border-bottom:1px solid #1a1a1a;padding:8px 24px;font-size:11px;color:#888">
  <span style="color:#ff6b35;font-weight:700">browse_manager.py</span>
  &nbsp;—&nbsp;manages <b style="color:#ccc">data/browse_cuisines.json</b>
  &nbsp;·&nbsp;push to GitHub → app picks up changes OTA
</div>

<div style="background:#0d1f0d;border-bottom:1px solid #1a3a1a;padding:7px 24px;
            display:flex;align-items:center;gap:10px;font-size:11px;flex-wrap:wrap">
  <span style="color:#4ade80;font-weight:700">▲ Push to GitHub:</span>
  <code id="git-cmd" style="background:#0a150a;border:1px solid #1e3a1e;border-radius:4px;
       padding:4px 10px;color:#86efac;cursor:pointer;user-select:all"
       onclick="copyGit()" title="Click to copy">
    git add data/browse_cuisines.json; git commit -m "update browse cuisines"; git push
  </code>
  <button onclick="copyGit()" style="background:#166534;border:none;border-radius:4px;
          color:#4ade80;font-size:11px;font-weight:700;padding:4px 10px;cursor:pointer">
    Copy
  </button>
  <span id="git-copied" style="color:#4ade80;display:none">✓ Copied!</span>
</div>

<div class="wrap">

  <!-- PREVIEW (grouped by cuisine, like the app) -->
  <div class="panel">
    <div class="panel-title">
      Preview — Grouped by Cuisine
      <span>matches how BrowseCuisinesActivity displays sites</span>
      <input id="search" type="text" placeholder="Filter by name or cuisine…"
             oninput="filterCards(this.value)" style="max-width:220px;margin-left:auto">
    </div>
    <div id="cuisine-groups">
      {% for cuisine, sites in grouped.items() %}
      <div class="cuisine-section" data-cuisine="{{ cuisine }}">
        <div class="cuisine-label">
          {{ cuisine.upper() }}
          <span class="cuisine-count">· {{ sites|length }}</span>
        </div>
        <div class="site-grid">
          {% for site in sites %}
          <div class="card" data-name="{{ site.name|lower }}" data-cuisine="{{ cuisine|lower }}">
            <div class="mv">
              <button onclick="moveSite({{ site.idx }}, 'up')"   title="Move up"   {% if site.idx == 0 %}disabled{% endif %}>▲</button>
              <button onclick="moveSite({{ site.idx }}, 'down')" title="Move down" {% if site.idx == loop.length - 1 %}disabled{% endif %}>▼</button>
            </div>
            {% if site.image_url %}
            <img src="/proxy-image?url={{ site.image_url | urlencode }}"
                 onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"
                 onclick="window.open('{{ site.url }}','_blank')" style="cursor:pointer">
            <div class="no-img" style="display:none">no img</div>
            {% else %}
            <div class="no-img">no img</div>
            {% endif %}
            <div class="ci">
              <div class="cn" title="{{ site.name }}">{{ site.name }}</div>
              <div class="cs">{{ site.cuisine }}</div>
            </div>
            <button class="rm" onclick="removeSite({{ site.idx }}, '{{ site.name|e }}')">✕</button>
          </div>
          {% endfor %}
        </div>
      </div>
      {% endfor %}
    </div>
  </div>

  <!-- ADD NEW SITE -->
  <div class="panel">
    <div class="panel-title">Add Site to Browse Cuisines</div>
    <div class="form-grid">
      <div class="full"><label>Site URL *</label>
        <input id="f-url" type="url" placeholder="https://example.com"></div>
      <div><label>Display Name *</label>
        <input id="f-name" type="text" placeholder="My Recipe Blog"></div>
      <div><label>Cuisine * <span style="color:#444">(display category)</span></label>
        <input id="f-cuisine" type="text" placeholder="American"></div>
      <div class="full"><label>Tagline *</label>
        <input id="f-tagline" type="text" placeholder="One-line description"></div>
      <div class="full"><label>RSS URL <span style="color:#444">(leave blank to auto-detect)</span></label>
        <input id="f-rss" type="url" placeholder="https://example.com/feed/"></div>
      <div class="full"><label>Image URL <span style="color:#444">(leave blank — pulled from og:image on validate)</span></label>
        <input id="f-image" type="url" placeholder="https://example.com/image.jpg"></div>
    </div>
    <div class="actions">
      <button class="btn btn-orange" onclick="runValidate()">Validate</button>
      <button class="btn btn-green" id="btn-add" disabled onclick="addSite()">Add to Browse</button>
      <button class="btn btn-ghost" onclick="clearForm()">Clear</button>
      <span id="val-spinner" style="display:none"><span class="spin"></span> Validating…</span>
    </div>
    <div id="val-results"></div>
  </div>

  <!-- FLAT LIST -->
  <div class="panel">
    <div class="panel-title">
      All Sites <span id="list-count">{{ total }} sites</span>
    </div>
    <table>
      <thead><tr><th>#</th><th>Name</th><th>Cuisine</th><th>URL</th><th></th></tr></thead>
      <tbody id="site-tbody">
        {% for site in all_sites %}
        <tr id="row-{{ site.idx }}">
          <td class="t-idx">{{ site.idx + 1 }}</td>
          <td class="t-name">{{ site.name }}</td>
          <td><span class="cuisine-tag">{{ site.cuisine }}</span></td>
          <td><a class="t-url" href="{{ site.url }}" target="_blank">{{ site.url }}</a></td>
          <td>
            <button class="btn btn-ghost btn-xs" style="padding:3px 8px;font-size:10px"
              onclick="moveSite({{ site.idx }},'up')" {% if site.idx==0 %}disabled{% endif %}>↑</button>
            <button class="btn btn-ghost btn-xs" style="padding:3px 8px;font-size:10px"
              onclick="moveSite({{ site.idx }},'down')" {% if site.idx==loop.length-1 %}disabled{% endif %}>↓</button>
            <button class="btn btn-xs" style="background:#7f1d1d;color:#fca5a5;padding:3px 8px;font-size:10px"
              onclick="removeSite({{ site.idx }},'{{ site.name|e }}')">Remove</button>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

</div>

<script>
function copyGit() {
  const cmd = 'git add data/browse_cuisines.json; git commit -m "update browse cuisines"; git push';
  navigator.clipboard.writeText(cmd).then(() => {
    const el = document.getElementById('git-copied');
    el.style.display = 'inline';
    setTimeout(() => el.style.display = 'none', 2000);
  });
}

setInterval(() => {
  document.getElementById('hdr-clock').textContent =
    new Date().toUTCString().replace(' GMT',' UTC');
}, 1000);

function filterCards(q) {
  q = q.toLowerCase();
  document.querySelectorAll('.cuisine-section').forEach(section => {
    let visible = 0;
    section.querySelectorAll('.card').forEach(card => {
      const match = !q || card.dataset.name.includes(q) || card.dataset.cuisine.includes(q);
      card.style.display = match ? '' : 'none';
      if (match) visible++;
    });
    section.style.display = visible ? '' : 'none';
  });
}

async function removeSite(idx, name) {
  if (!confirm(`Remove "${name}" from Browse Cuisines?`)) return;
  const res  = await fetch('/remove', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ idx })
  });
  const data = await res.json();
  if (data.ok) location.reload();
  else alert('Error: ' + data.error);
}

async function moveSite(idx, direction) {
  const res  = await fetch('/move', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ idx, direction })
  });
  const data = await res.json();
  if (data.ok) location.reload();
  else alert('Move failed: ' + data.error);
}

let validationPassed = false;
let ogImage = '';

async function runValidate() {
  const url = document.getElementById('f-url').value.trim();
  if (!url) { alert('URL is required'); return; }
  validationPassed = false; ogImage = '';
  document.getElementById('btn-add').disabled = true;
  document.getElementById('val-spinner').style.display = 'inline';
  document.getElementById('val-results').style.display = 'none';
  try {
    const res  = await fetch('/validate', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ url,
        name:    document.getElementById('f-name').value.trim(),
        rss_url: document.getElementById('f-rss').value.trim() })
    });
    const data = await res.json();
    ogImage = data.og_image || '';
    if (ogImage && !document.getElementById('f-image').value.trim()) {
      document.getElementById('f-image').value = ogImage;
    }
    showValidation(data);
  } finally {
    document.getElementById('val-spinner').style.display = 'none';
  }
}

function iconCls(v) { return v.startsWith('✓')?'ok':v.startsWith('✗')?'fail':'warn'; }
function showValidation(data) {
  validationPassed = data.passed;
  let html = '';
  for (const [k, v] of Object.entries(data.checks))
    html += `<div class="chk"><span class="chk-name">${k}</span>
             <span class="chk-val ${iconCls(v)}">${v}</span></div>`;
  if (data.og_image)
    html += `<div class="chk"><span class="chk-name">og:image</span>
             <span class="chk-val ok"><a href="${data.og_image}" target="_blank" style="color:#60a5fa">${data.og_image.slice(0,80)}…</a></span></div>`;
  html += data.passed
    ? '<div class="banner banner-ok">✓ All checks passed — ready to add</div>'
    : '<div class="banner banner-fail">✗ Validation failed</div>';
  const el = document.getElementById('val-results');
  el.innerHTML = html; el.style.display = 'block';
  document.getElementById('btn-add').disabled = !data.passed;
}

async function addSite() {
  if (!validationPassed) return;
  const name    = document.getElementById('f-name').value.trim();
  const cuisine = document.getElementById('f-cuisine').value.trim();
  const tagline = document.getElementById('f-tagline').value.trim();
  const url     = document.getElementById('f-url').value.trim();
  const rss_url = document.getElementById('f-rss').value.trim();
  const image_url = document.getElementById('f-image').value.trim() || ogImage;
  if (!name || !cuisine || !tagline) { alert('Name, Cuisine, and Tagline required.'); return; }
  const res  = await fetch('/add', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name, url, cuisine, tagline, rss_url, image_url })
  });
  const data = await res.json();
  if (data.ok) { alert(`✓ "${name}" added! Total: ${data.total} sites`); location.reload(); }
  else alert('Error: ' + data.error);
}

function clearForm() {
  ['f-url','f-name','f-cuisine','f-tagline','f-rss','f-image'].forEach(
    id => document.getElementById(id).value = ''
  );
  document.getElementById('val-results').style.display = 'none';
  document.getElementById('btn-add').disabled = true;
  validationPassed = false; ogImage = '';
}
</script>
</body>
</html>"""

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/proxy-image")
def proxy_image():
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
    from urllib.parse import quote as urlencode
    sites   = load_browse()
    indexed = [{"idx": i, **s} for i, s in enumerate(sites)]
    grouped = {}
    for s in indexed:
        c = s.get("cuisine") or "International"
        grouped.setdefault(c, []).append(s)
    grouped = dict(sorted(grouped.items()))
    return render_template_string(
        PAGE,
        total         = len(sites),
        cuisine_count = len(grouped),
        grouped       = grouped,
        all_sites     = indexed,
        urlencode     = urlencode,
    )


@app.route("/validate", methods=["POST"])
def validate():
    data    = request.get_json()
    url     = (data.get("url") or "").rstrip("/")
    name    = data.get("name") or url
    rss_url = data.get("rss_url") or ""
    site    = {"name": name, "url": url}
    if rss_url:
        site["rss_url"] = rss_url
    checks  = validate_site(site)

    # Try to grab og:image while we're here
    og_image = ""
    try:
        r = req.get(url, timeout=8, headers=HEADERS)
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text) or
             re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text))
        if m:
            og_image = m.group(1)
    except Exception:
        pass

    return jsonify({"checks": checks, "passed": passed(checks), "og_image": og_image})


@app.route("/add", methods=["POST"])
def add_site():
    data      = request.get_json()
    name      = (data.get("name") or "").strip()
    url       = (data.get("url") or "").rstrip("/")
    cuisine   = (data.get("cuisine") or "").strip()
    tagline   = (data.get("tagline") or "").strip()
    rss_url   = (data.get("rss_url") or "").strip()
    image_url = (data.get("image_url") or "").strip()
    if not all([name, url, cuisine, tagline]):
        return jsonify({"ok": False, "error": "name, url, cuisine, tagline all required"})
    sites = load_browse()
    if any(s["url"].rstrip("/") == url for s in sites):
        return jsonify({"ok": False, "error": "URL already in browse_cuisines.json"})
    entry = {"name": name, "url": url, "cuisine": cuisine,
             "tagline": tagline, "image_url": image_url}
    if rss_url:
        entry["rss_url"] = rss_url
    sites.append(entry)
    save_browse(sites)
    return jsonify({"ok": True, "total": len(sites)})


@app.route("/remove", methods=["POST"])
def remove_site():
    data  = request.get_json()
    idx   = data.get("idx")
    sites = load_browse()
    if idx is None or idx < 0 or idx >= len(sites):
        return jsonify({"ok": False, "error": "invalid index"})
    sites.pop(idx)
    save_browse(sites)
    return jsonify({"ok": True, "total": len(sites)})


@app.route("/move", methods=["POST"])
def move_site():
    data      = request.get_json()
    idx       = data.get("idx")
    direction = data.get("direction")
    sites     = load_browse()
    if idx is None or idx < 0 or idx >= len(sites):
        return jsonify({"ok": False, "error": "invalid index"})
    if direction == "up" and idx > 0:
        sites[idx], sites[idx - 1] = sites[idx - 1], sites[idx]
    elif direction == "down" and idx < len(sites) - 1:
        sites[idx], sites[idx + 1] = sites[idx + 1], sites[idx]
    else:
        return jsonify({"ok": False, "error": "cannot move"})
    save_browse(sites)
    return jsonify({"ok": True})


# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("CraveBox Browse Manager → http://localhost:5002")
    app.run(port=5002, debug=False)
