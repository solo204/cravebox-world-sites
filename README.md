# cravebox-world-sites

Auto-rotating featured recipe sites and recipe of the day for the CraveBox app.

## How it works

- **`data/master_sites.json`** — curated list of quality free recipe sites (add new sites here)
- **`data/featured.json`** — current featured content; auto-updated by GitHub Actions every 2 hours
- **`rotate.py`** — picks 4 sites for the current time slot + fetches the latest recipe via RSS
- **`.github/workflows/rotate.yml`** — schedules the rotator

## App fetch URL

```
https://raw.githubusercontent.com/solo204/cravebox-world-sites/main/data/featured.json
```

## Adding new sites

Edit `data/master_sites.json` — add an entry with `name`, `url`, `cuisine`, `tagline`, `image_url`, and `rss_url`.

## Switching to weekly rotation

In `rotate.py`, change:
```python
SLOT_HOURS = 2   →   SLOT_HOURS = 168
```
In `.github/workflows/rotate.yml`, change the cron:
```yaml
cron: "0 */2 * * *"   →   cron: "0 1 * * 1"
```
