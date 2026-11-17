#!/usr/bin/env python3
"""BountyTracker — daily snapshots of bug bounty program report counts.

    python3 tracker.py add <program-url-or-slug>
    python3 tracker.py collect
    python3 tracker.py serve [--port 8787]
"""
import argparse
import os
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from platforms import ADAPTERS, resolve

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = DATA / "bountytracker.db"
LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    slug TEXT NOT NULL,
    title TEXT, company TEXT, currency TEXT, url TEXT,
    added_at TEXT NOT NULL,
    last_error TEXT,
    UNIQUE(platform, slug)
);
CREATE TABLE IF NOT EXISTS snapshots (
    program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    total_reports INTEGER, last24h INTEGER, last7d INTEGER, month INTEGER,
    avg_first_response INTEGER, bounty_min INTEGER, bounty_max INTEGER,
    scopes_count INTEGER,
    PRIMARY KEY (program_id, day)
);
CREATE TABLE IF NOT EXISTS scope_snapshots (
    program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    scope TEXT NOT NULL,
    type TEXT, asset_value TEXT, report_count INTEGER,
    PRIMARY KEY (program_id, day, scope)
);
"""


def db():
    DATA.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def token_for(platform):
    """Optional auth token: env YWH_TOKEN, or data/yeswehack.token."""
    env = {"yeswehack": "YWH_TOKEN"}.get(platform)
    if env and os.environ.get(env):
        return os.environ[env].strip()
    f = DATA / f"{platform}.token"
    return f.read_text().strip() if f.exists() else None


# ---------------------------------------------------------------- collecting

def collect_one(con, prog):
    adapter = ADAPTERS[prog["platform"]]
    try:
        d = adapter.fetch(prog["slug"], token_for(prog["platform"]))
    except Exception as e:  # keep going; surface the error in the UI
        con.execute("UPDATE programs SET last_error=? WHERE id=?", (str(e), prog["id"]))
        con.commit()
        return str(e)
    day = date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pid = prog["id"]
    con.execute(
        "UPDATE programs SET title=?, company=?, currency=?, url=?, last_error=NULL WHERE id=?",
        (d["title"], d["company"], d["currency"], d["url"], pid),
    )
    con.execute(
        "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pid, day, now, d["total_reports"], d["last24h"], d["last7d"], d["month"],
         d["avg_first_response"], d["bounty_min"], d["bounty_max"], len(d["scopes"])),
    )
    con.execute("DELETE FROM scope_snapshots WHERE program_id=? AND day=?", (pid, day))
    con.executemany(
        "INSERT OR REPLACE INTO scope_snapshots VALUES (?,?,?,?,?,?)",
        [(pid, day, s["scope"], s["type"], s["asset_value"], s["report_count"])
         for s in d["scopes"]],
    )
    con.commit()
    return None


def sync_programs():
    """Track every publicly listed program on each platform.

    Rows are inserted in alphabetical order with one shared timestamp, so ids
    and dates say nothing about which program was of interest first.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    for adapter in ADAPTERS.values():
        if not hasattr(adapter, "list_public"):
            continue
        slugs = sorted(set(adapter.list_public()))
        with LOCK:
            con = db()
            for slug in slugs:
                added += con.execute(
                    "INSERT OR IGNORE INTO programs (platform, slug, added_at) VALUES (?,?,?)",
                    (adapter.NAME, slug, now)).rowcount
            con.commit()
            con.close()
    return added


def collect_all():
    with LOCK:
        con = db()
        results = {}
        for prog in con.execute("SELECT * FROM programs ORDER BY id").fetchall():
            results[prog["slug"]] = collect_one(con, prog) or "ok"
            time.sleep(2)  # stay human-paced
        con.close()
        return results


def add_program(text):
    adapter, slug = resolve(text)
    if not adapter:
        raise ValueError("無法辨識的 program 連結(目前支援 yeswehack.com/programs/<slug>)")
    with LOCK:
        con = db()
        con.execute(
            "INSERT OR IGNORE INTO programs (platform, slug, added_at) VALUES (?,?,?)",
            (adapter.NAME, slug, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        con.commit()
        prog = con.execute(
            "SELECT * FROM programs WHERE platform=? AND slug=?", (adapter.NAME, slug)
        ).fetchone()
        err = collect_one(con, prog)
        has_data = con.execute(
            "SELECT 1 FROM snapshots WHERE program_id=?", (prog["id"],)
        ).fetchone()
        if err and not has_data:  # never fetched successfully: don't keep a dead row
            con.execute("DELETE FROM programs WHERE id=?", (prog["id"],))
            con.commit()
            con.close()
            raise ValueError(err)
        con.close()
        return prog["id"]


# ------------------------------------------------------------------- queries

def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def program_summary(con, prog):
    snaps = con.execute(
        "SELECT * FROM snapshots WHERE program_id=? ORDER BY day DESC LIMIT 2", (prog["id"],)
    ).fetchall()
    out = {k: prog[k] for k in prog.keys() if k != "added_at"}
    if snaps:
        cur = snaps[0]
        out.update({k: cur[k] for k in cur.keys() if k != "program_id"})
        out["delta"] = (
            cur["total_reports"] - snaps[1]["total_reports"]
            if len(snaps) > 1 and None not in (cur["total_reports"], snaps[1]["total_reports"])
            else None
        )
        out["prev_day"] = snaps[1]["day"] if len(snaps) > 1 else None
    return out


def program_detail(con, prog):
    pid = prog["id"]
    snaps = [dict(r) for r in con.execute(
        "SELECT * FROM snapshots WHERE program_id=? ORDER BY day", (pid,))]
    rows = con.execute(
        "SELECT * FROM scope_snapshots WHERE program_id=? ORDER BY day", (pid,)).fetchall()
    by_day = {}
    for r in rows:
        by_day.setdefault(r["day"], {})[r["scope"]] = r

    events, history = [], []
    scope_info = {}  # scope -> first_seen / last_increase / history
    prev = None
    for s in snaps:
        day, cur_scopes = s["day"], by_day.get(s["day"], {})
        delta = None
        if prev:
            if None not in (s["total_reports"], prev["total_reports"]):
                delta = s["total_reports"] - prev["total_reports"]
                if delta:
                    events.append({"day": day, "kind": "program_reports", "scope": None,
                                   "delta": delta, "since": prev["day"],
                                   "total": s["total_reports"]})
            old = by_day.get(prev["day"], {})
            for name in cur_scopes.keys() - old.keys():
                events.append({"day": day, "kind": "scope_added", "scope": name})
            for name in old.keys() - cur_scopes.keys():
                events.append({"day": day, "kind": "scope_removed", "scope": name})
            for name in cur_scopes.keys() & old.keys():
                a, b = old[name]["report_count"], cur_scopes[name]["report_count"]
                if a is not None and b is not None and b != a:
                    events.append({"day": day, "kind": "scope_reports", "scope": name,
                                   "delta": b - a, "since": prev["day"], "total": b})
        for name, r in cur_scopes.items():
            info = scope_info.setdefault(name, {"first_seen": day, "last_increase": None,
                                                "history": []})
            h = info["history"]
            if h and r["report_count"] is not None and h[-1]["count"] is not None \
                    and r["report_count"] > h[-1]["count"]:
                info["last_increase"] = day
            h.append({"day": day, "count": r["report_count"]})
        history.append({**{k: s[k] for k in ("day", "taken_at", "total_reports", "last24h",
                                              "last7d", "month", "scopes_count")},
                        "delta": delta})
        prev = s

    scopes = []
    if snaps:
        latest_day = snaps[-1]["day"]
        for name, r in by_day.get(latest_day, {}).items():
            info = scope_info[name]
            h = info["history"]
            d = None
            if len(h) > 1 and None not in (h[-1]["count"], h[-2]["count"]):
                d = h[-1]["count"] - h[-2]["count"]
            scopes.append({"scope": name, "type": r["type"], "asset_value": r["asset_value"],
                           "report_count": r["report_count"], "delta": d, **info})
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        scopes.sort(key=lambda x: (-(x["report_count"] or 0), order.get(x["asset_value"], 9)))

    stats = {"tracked_days": len(snaps), "avg_7d": None, "avg_month": None, "avg_tracked": None}
    if snaps:
        last = snaps[-1]
        if last["last7d"] is not None:
            stats["avg_7d"] = round(last["last7d"] / 7, 2)
        if last["month"] is not None:
            stats["avg_month"] = round(last["month"] / date.fromisoformat(last["day"]).day, 2)
        span = days_between(snaps[0]["day"], last["day"])
        if span > 0 and None not in (snaps[0]["total_reports"], last["total_reports"]):
            stats["avg_tracked"] = round(
                (last["total_reports"] - snaps[0]["total_reports"]) / span, 2)
    return {
        "program": program_summary(con, prog),
        "history": history,
        "scopes": scopes,
        "scope_counts_available": any(s["report_count"] is not None for s in scopes),
        "events": sorted(events, key=lambda e: e["day"], reverse=True),
        "stats": stats,
    }


# ----------------------------------------------------------------------- web

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/api/programs")
def api_programs():
    con = db()
    out = [program_summary(con, p) for p in con.execute(
        "SELECT * FROM programs ORDER BY COALESCE(title, slug) COLLATE NOCASE")]
    con.close()
    return jsonify(out)


@app.get("/api/config")
def api_config():
    return jsonify({"read_only": app.config.get("READ_ONLY", False)})


@app.before_request
def block_writes():
    if app.config.get("READ_ONLY") and request.method not in ("GET", "HEAD"):
        return jsonify({"error": "read-only"}), 403


@app.post("/api/programs")
def api_add():
    url = (request.get_json(silent=True) or {}).get("url", "")
    try:
        return jsonify({"id": add_program(url)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/programs/<int:pid>")
def api_detail(pid):
    con = db()
    prog = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
    if not prog:
        con.close()
        return jsonify({"error": "not found"}), 404
    out = program_detail(con, prog)
    con.close()
    return jsonify(out)


@app.delete("/api/programs/<int:pid>")
def api_delete(pid):
    con = db()
    con.execute("DELETE FROM programs WHERE id=?", (pid,))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.post("/api/collect")
def api_collect():
    return jsonify(collect_all())


def scheduler(hours, sync):
    while True:
        try:
            if sync:
                sync_programs()
            collect_all()
        except Exception as e:
            print(f"[collect] {e}", file=sys.stderr)
        time.sleep(hours * 3600)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("url")
    sub.add_parser("collect")
    sub.add_parser("sync", help="track every public program, then collect")
    s = sub.add_parser("serve")
    s.add_argument("--sync-all", action="store_true",
                   help="track every public program on each platform")
    s.add_argument("--read-only", action="store_true",
                   help="disable add / delete / manual collect over HTTP")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--interval-hours", type=float, default=6,
                   help="background collect interval; 0 disables it")
    args = ap.parse_args()

    if args.cmd == "add":
        try:
            print(f"added program id={add_program(args.url)}")
        except ValueError as e:
            sys.exit(str(e))
    elif args.cmd == "collect":
        for slug, res in collect_all().items():
            print(f"{slug}: {res}")
    elif args.cmd == "sync":
        print(f"new programs: {sync_programs()}")
        for slug, res in collect_all().items():
            print(f"{slug}: {res}")
    else:
        app.config["READ_ONLY"] = args.read_only
        if args.interval_hours > 0:
            threading.Thread(target=scheduler, args=(args.interval_hours, args.sync_all),
                             daemon=True).start()
        app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
