#!/usr/bin/env python3
"""Claude Code usage collector -> data/usage.json

Runs ccusage (Claude Code logs only) and normalises its output, plus a
metadata-only scan of ~/.claude/projects/**/*.jsonl for things ccusage does
not expose (hourly buckets for the heatmap, per-model split of each 5h block,
session -> project name).

Privacy: only timestamps, model names, token counts, session ids and the
basename of the working directory are read. Message content is never read
into the output.

Idempotent: re-running merges into the existing file keyed by block id /
date / session id / hour. Records that have since been pruned from the logs
are kept; records that still exist are refreshed (the active block and today
keep growing until they close).

Usage:  python collect.py [--out data/usage.json] [--tz Australia/Brisbane]
                          [--claude-dir ~/.claude] [--offline] [--rows FILE]
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Zones with no DST, usable without the tzdata package (Windows Python lacks it).
FIXED_ZONES = {"Australia/Brisbane": 10, "UTC": 0, "Etc/UTC": 0}


def get_tz(name: str) -> dt.tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name in FIXED_ZONES:
            return dt.timezone(dt.timedelta(hours=FIXED_ZONES[name]), name)
        sys.exit(f"No timezone data for {name!r}. Run: python -m pip install tzdata")

CATS = ("in", "out", "cache_read", "cache_write")
# Relative price weights shared by all Claude models (input=1). Used only to
# apportion ccusage's exact costs across hours / models inside a block.
WEIGHT = {"in": 1.0, "out": 5.0, "cache_read": 0.1, "cache_write": 1.25}


# ---------------------------------------------------------------- ccusage ---
def ccusage_cmd() -> list[str]:
    exe = shutil.which("ccusage")
    if exe:
        return [exe]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", "ccusage@latest"]
    sys.exit("ccusage not found. Install with: npm i -g ccusage")


def run_ccusage(args: list[str], tz: str, offline: bool) -> dict:
    cmd = ccusage_cmd() + ["claude"] + args + ["--json", "--timezone", tz]
    if offline:
        cmd.append("--offline")
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        sys.exit(f"ccusage failed ({' '.join(args)}):\n{res.stderr}")
    out = res.stdout
    return json.loads(out[out.index("{"):])  # tolerate any banner before JSON


def model_tokens(breakdowns: list[dict]) -> tuple[dict, dict]:
    tokens, costs = {}, {}
    for b in breakdowns or []:
        m = b.get("modelName") or "unknown"
        t = tokens.setdefault(m, dict.fromkeys(CATS, 0))
        t["in"] += b.get("inputTokens", 0)
        t["out"] += b.get("outputTokens", 0)
        t["cache_read"] += b.get("cacheReadTokens", 0)
        t["cache_write"] += b.get("cacheCreationTokens", 0)
        costs[m] = costs.get(m, 0.0) + (b.get("cost") or 0.0)
    return tokens, costs


def tot(tokens_by_model: dict) -> int:
    return sum(sum(t.values()) for t in tokens_by_model.values())


# ------------------------------------------------------------ jsonl scan ----
def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def scan_logs(claude_dir: str):
    """Yield (ts, session_id, cwd_basename, model, {cat: n}) for each unique
    assistant response. Reads usage metadata only."""
    seen = set()
    roots = [os.path.join(claude_dir, "projects")]
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        roots += [os.path.join(p, "projects") for p in cfg.split(",")]
    for root in dict.fromkeys(roots):
        for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            try:
                fh = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    model = msg.get("model")
                    ts = d.get("timestamp")
                    if not u or not ts or not model or model == "<synthetic>":
                        continue
                    key = (msg.get("id"), d.get("requestId"))
                    if key[0] and key[1]:
                        if key in seen:
                            continue
                        seen.add(key)
                    yield (
                        parse_ts(ts),
                        d.get("sessionId"),
                        os.path.basename((d.get("cwd") or "").rstrip("/\\")) or None,
                        model,
                        {
                            "in": u.get("input_tokens") or 0,
                            "out": u.get("output_tokens") or 0,
                            "cache_read": u.get("cache_read_input_tokens") or 0,
                            "cache_write": u.get("cache_creation_input_tokens") or 0,
                        },
                    )


def weight(t: dict) -> float:
    return sum(WEIGHT[c] * t[c] for c in CATS)


def add(dst: dict, model: str, t: dict):
    m = dst.setdefault(model, dict.fromkeys(CATS, 0))
    for c in CATS:
        m[c] += t[c]


# ----------------------------------------------------------------- build ----
def build(tz_name: str, claude_dir: str, offline: bool) -> dict:
    tz = get_tz(tz_name)
    raw_blocks = run_ccusage(["blocks"], tz_name, offline).get("blocks", [])
    raw_days = run_ccusage(["daily", "--breakdown"], tz_name, offline).get("daily", [])
    raw_sess = run_ccusage(["session"], tz_name, offline).get("sessions", [])

    # --- days (exact from ccusage)
    days, day_cost = {}, {}
    for d in raw_days:
        tokens, costs = model_tokens(d.get("modelBreakdowns"))
        date = d.get("date") or d.get("period")
        days[date] = {"date": date, "tokens": tokens,
                      "cost": round(d.get("totalCost", 0.0), 6), "sessions": 0}
        day_cost[date] = costs

    # --- blocks (non-gap) from ccusage
    blocks = []
    for b in raw_blocks:
        if b.get("isGap"):
            continue
        blocks.append({
            "id": b["id"], "start": b["startTime"], "end": b["endTime"],
            "last": b.get("actualEndTime"), "active": bool(b.get("isActive")),
            "entries": b.get("entries", 0), "tokens": {},
            "cost": round(b.get("costUSD", 0.0), 6),
            "_tc": b.get("tokenCounts", {}), "_models": b.get("models", []),
        })
    blocks.sort(key=lambda x: x["start"])
    starts = [parse_ts(b["start"]) for b in blocks]
    ends = [parse_ts(b["end"]) for b in blocks]

    # --- metadata scan
    hours = {}                      # local hour key -> tokens by model
    hour_w = defaultdict(float)     # (hourkey, model) -> weight
    day_w = defaultdict(float)      # (date, model) -> weight
    block_w = defaultdict(float)    # (block idx, model) -> weight
    sess_cwd, sess_days = {}, defaultdict(set)
    for ts, sid, cwd, model, t in scan_logs(claude_dir):
        local = ts.astimezone(tz)
        hk = local.strftime("%Y-%m-%dT%H")
        date = local.strftime("%Y-%m-%d")
        add(hours.setdefault(hk, {}), model, t)
        w = weight(t)
        hour_w[(hk, model)] += w
        day_w[(date, model)] += w
        if sid:
            if cwd:
                sess_cwd.setdefault(sid, cwd)
            sess_days[sid].add(date)
        i = bisect.bisect_right(starts, ts) - 1
        if 0 <= i < len(blocks) and ts < ends[i]:
            add(blocks[i]["tokens"], model, t)
            block_w[(i, model)] += w

    # block per-model cost split (apportion exact block cost by weight)
    for i, b in enumerate(blocks):
        tc, models = b.pop("_tc"), b.pop("_models")
        if not b["tokens"]:  # scan missed it (e.g. other config dir): coarse fallback
            b["tokens"] = {(models[0] if len(models) == 1 else "mixed"): {
                "in": tc.get("inputTokens", 0), "out": tc.get("outputTokens", 0),
                "cache_read": tc.get("cacheReadInputTokens", 0),
                "cache_write": tc.get("cacheCreationInputTokens", 0)}}
            continue
        wt = sum(block_w[(i, m)] for m in b["tokens"]) or 1.0
        b["cost_by_model"] = {m: round(b["cost"] * block_w[(i, m)] / wt, 6)
                              for m in b["tokens"]}

    # hours: apportion exact day/model cost across that day's hours
    hour_rows = []
    for hk, tokens in sorted(hours.items()):
        date = hk[:10]
        cost = 0.0
        for m in tokens:
            dw = day_w[(date, m)]
            if dw:
                cost += day_cost.get(date, {}).get(m, 0.0) * hour_w[(hk, m)] / dw
        hour_rows.append({"hour": hk, "tokens": tokens, "cost": round(cost, 6)})

    # --- sessions
    sessions = []
    for s in raw_sess:
        sid = s.get("sessionId") or s.get("period")
        tokens, costs = model_tokens(s.get("modelBreakdowns"))
        total_cost = s.get("totalCost", 0.0) or 0.0
        proj = sess_cwd.get(sid) or (s.get("projectPath") or "unknown").split("-")[-1]
        sessions.append({
            "id": sid, "project": proj,
            "start": s.get("firstActivity"), "end": s.get("lastActivity"),
            "model_mix": {m: round(c / total_cost, 4) if total_cost else 0
                          for m, c in costs.items()},
            "tokens": tokens, "cost": round(total_cost, 6),
        })
        for date in sess_days.get(sid, ()):
            if date in days:
                days[date]["sessions"] += 1

    return {
        "generated": dt.datetime.now(tz).isoformat(timespec="seconds"),
        "timezone": tz_name,
        "blocks": blocks,
        "days": sorted(days.values(), key=lambda d: d["date"]),
        "sessions": sorted(sessions, key=lambda s: s["start"] or ""),
        "hours": hour_rows,
    }


# ------------------------------------------------- plan-limit readings ----
# Conductor logs/usage.md, one line per reading (TEMPLATES.md B5):
# YYYY-MM-DD HH:MM | window <n>% resets <hh:mm> | week <n>% resets <day hh:mm> | limited: yes|no | running: <workers>
# "window ?" / "week ?" means the figure could not be read: stored as null, never 0.
import re

_READING = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2})\s*\|"
    r"\s*window\s+(?P<w>\d+(?:\.\d+)?|\?)\s*%?(?:\s+resets\s+(?P<wr>\d{1,2}:\d{2}|\?))?\s*\|"
    r"\s*week\s+(?P<k>\d+(?:\.\d+)?|\?)\s*%?(?:\s+resets\s+(?P<kr>[A-Za-z]{3,9}\.?\s+\d{1,2}:\d{2}|\?))?\s*\|"
    r"\s*limited:\s*(?P<lim>yes|no|\?)\s*\|"
    r"\s*running:\s*(?P<run>.*?)\s*$", re.I)
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _next_time(base: dt.datetime, hhmm: str, weekday: int | None = None) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    t = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if weekday is None:
        return t if t > base else t + dt.timedelta(days=1)
    t += dt.timedelta(days=(weekday - base.weekday()) % 7)
    return t if t > base else t + dt.timedelta(days=7)


def parse_usage_log(path: str, tz: dt.tzinfo) -> tuple[list[dict], int]:
    """Return (readings, unparsed_line_count). Only the fields above are kept."""
    out, bad = [], 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not re.match(r"^\s*(?:[-*]\s*)?\d{4}-\d{2}-\d{2}", line):
                continue  # headings, blank lines, prose
            m = _READING.match(line)
            if not m:
                bad += 1
                continue
            ts = dt.datetime.strptime(m["ts"].replace("T", " "), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            num = lambda v: None if v == "?" else float(v)
            w_reset = k_reset = None
            if m["wr"] and m["wr"] != "?":
                w_reset = _next_time(ts, m["wr"])
            if m["kr"] and m["kr"] != "?":
                day, hhmm = m["kr"].split()
                wd = _DAYS.index(day[:3].lower()) if day[:3].lower() in _DAYS else None
                if wd is not None:
                    k_reset = _next_time(ts, hhmm, wd)
            run = m["run"].strip()
            workers = [] if run.lower() in ("", "-", "none", "nothing", "idle") else \
                [w.strip() for w in re.split(r"[,;]", run) if w.strip()]
            iso = lambda d: d.isoformat(timespec="minutes") if d else None
            out.append({
                "id": ts.strftime("%Y-%m-%dT%H:%M"), "at": iso(ts),
                "window_pct": num(m["w"]), "window_resets": iso(w_reset),
                "week_pct": num(m["k"]), "week_resets": iso(k_reset),
                "limited": {"yes": True, "no": False}.get(m["lim"].lower()),
                "running": workers,
            })
    return out, bad


# ----------------------------------------------------------------- merge ----
def total_tokens(rec: dict) -> int:
    return tot(rec.get("tokens", {}))


def merge(old: dict, new: dict) -> dict:
    out = dict(new)
    for key, idf in (("blocks", "id"), ("days", "date"), ("sessions", "id"), ("hours", "hour"), ("readings", "id")):
        merged = {r[idf]: r for r in old.get(key, [])}
        for r in new.get(key, []):
            prev = merged.get(r[idf])
            # Fresh data wins unless logs were pruned and it shrank.
            if prev is None or key == "readings" or total_tokens(r) >= total_tokens(prev):
                merged[r[idf]] = r
        if key == "blocks":  # only the freshest run can have an active block
            fresh = {r["id"] for r in new.get("blocks", [])}
            for r in merged.values():
                if r["id"] not in fresh:
                    r["active"] = False
        sort_key = {"blocks": "start", "days": "date", "sessions": "start", "hours": "hour", "readings": "id"}[key]
        out[key] = sorted(merged.values(), key=lambda r: r.get(sort_key) or "")
    return out


def to_rows(usage: dict) -> list[dict]:
    """Flat rows for the artifact database: one doc per record, keyed by kind+id."""
    rows = []
    for kind, coll, idf in (("block", "blocks", "id"), ("day", "days", "date"), ("session", "sessions", "id"),
                            ("hour", "hours", "hour"), ("usage", "readings", "id")):
        for r in usage.get(coll, []):
            rows.append({"kind": kind, "key": f"{kind}:{r[idf]}", **r})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--out", default=os.path.join(here, "data", "usage.json"))
    ap.add_argument("--tz", default="Australia/Brisbane")
    ap.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"))
    ap.add_argument("--offline", action="store_true", help="use ccusage's cached pricing")
    ap.add_argument("--rows", help="also write flat db rows (JSON array) to this path")
    ap.add_argument("--usage-log", help="Conductor logs/usage.md with plan-limit readings (optional)")
    ap.add_argument("--changed-rows", help="write only rows that are new or changed since the previous run")
    a = ap.parse_args()

    new = build(a.tz, a.claude_dir, a.offline)
    if a.usage_log:
        if os.path.exists(a.usage_log):
            readings, bad = parse_usage_log(a.usage_log, get_tz(a.tz))
            new["readings"] = readings
            print(f"usage log: {len(readings)} readings" + (f", {bad} unparsed line(s) skipped" if bad else ""))
        else:
            print(f"usage log not found, skipped: {a.usage_log}")
    old = {}
    if os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as fh:
            old = json.load(fh)
    usage = merge(old, new)

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(usage, fh, indent=1)
    os.replace(tmp, a.out)
    if a.rows:
        with open(a.rows, "w", encoding="utf-8") as fh:
            json.dump(to_rows(usage), fh)
    if a.changed_rows:
        before = {r["key"]: r for r in to_rows(old)} if old else {}
        changed = [r for r in to_rows(usage) if before.get(r["key"]) != r]
        changed.append({"kind": "meta", "key": "meta", "generated": usage["generated"],
                        "timezone": usage.get("timezone")})
        with open(a.changed_rows, "w", encoding="utf-8") as fh:
            json.dump(changed, fh)
        print(f"  changed rows: {len(changed)} -> {a.changed_rows}")

    n = {k: (len(old.get(k, [])), len(usage[k])) for k in ("blocks", "days", "sessions", "hours", "readings")}
    first = usage["days"][0]["date"] if usage["days"] else "-"
    print(f"wrote {a.out}  (data from {first})")
    for k, (o, c) in n.items():
        print(f"  {k:9s} {c:6d}  (+{c - o})")


if __name__ == "__main__":
    main()
