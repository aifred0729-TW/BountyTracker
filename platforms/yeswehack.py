"""YesWeHack adapter — the base template for platform adapters.

An adapter exposes:
    NAME                      platform id stored in the DB
    parse(url_or_slug)        -> slug or None
    fetch(slug, token=None)   -> normalized dict (see fetch() below)

Only the public program endpoint is used (one GET per program per collect).
Per-scope report counts are only returned by YesWeHack to authenticated
hunters, so they stay None unless a token is supplied.
"""
import re
import time

import requests

NAME = "yeswehack"
API = "https://api.yeswehack.com"
UA = "BountyTracker/0.1"


def parse(text):
    text = text.strip()
    m = re.search(r"yeswehack\.com/programs/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]+", text):
        return text
    return None


def list_public():
    """Slugs of every publicly listed program (a couple of GETs)."""
    slugs, page, pages = [], 1, 1
    while page <= pages:
        r = requests.get(f"{API}/programs", params={"page": page, "resultsPerPage": 50},
                         headers={"Accept": "application/json", "User-Agent": UA}, timeout=30)
        r.raise_for_status()
        d = r.json()
        pages = (d.get("pagination") or {}).get("nb_pages", 1)
        slugs += [p["slug"] for p in d.get("items", [])
                  if p.get("slug") and not p.get("disabled") and not p.get("archived")]
        page += 1
        time.sleep(1)
    return slugs


def fetch(slug, token=None):
    headers = {"Accept": "application/json", "User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(f"{API}/programs/{slug}", headers=headers, timeout=30)
    if r.status_code == 401 and token:
        raise RuntimeError("YesWeHack 拒絕了 token(可能已過期)")
    if r.status_code == 404:
        raise RuntimeError(f"找不到 program:{slug}")
    r.raise_for_status()
    d = r.json()
    stats = d.get("stats") or {}
    bu = d.get("business_unit") or {}
    return {
        "slug": d.get("slug") or slug,
        "title": d.get("title") or slug,
        "company": bu.get("name"),
        "currency": bu.get("currency"),
        "url": f"https://yeswehack.com/programs/{slug}",
        "total_reports": stats.get("total_reports", d.get("reports_count")),
        "last24h": stats.get("total_reports_last24_hours"),
        "last7d": stats.get("total_reports_last7_days"),
        "month": stats.get("total_reports_current_month"),
        "avg_first_response": stats.get("average_first_time_response"),
        "bounty_min": d.get("bounty_reward_min"),
        "bounty_max": d.get("bounty_reward_max"),
        "platform_updated_at": d.get("last_update_at"),
        "scopes": [
            {
                "scope": s.get("scope"),
                "type": s.get("scope_type_name") or s.get("scope_type"),
                "asset_value": s.get("asset_value"),
                "report_count": s.get("report_count"),
            }
            for s in (d.get("scopes") or [])
            if s.get("scope")
        ],
    }
