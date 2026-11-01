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
from zoneinfo import ZoneInfo

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
    tz = ZoneInfo(tz_name)
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


# ----------------------------------------------------------------- merge ----
def total_tokens(rec: dict) -> int:
    return tot(rec.get("tokens", {}))


def merge(old: dict, new: dict) -> dict:
    out = dict(new)
    for key, idf in (("blocks", "id"), ("days", "date"), ("sessions", "id"), ("hours", "hour")):
        merged = {r[idf]: r for r in old.get(key, [])}
        for r in new.get(key, []):
            prev = merged.get(r[idf])
            # Fresh data wins unless logs were pruned and it shrank.
            if prev is None or total_tokens(r) >= total_tokens(prev):
                merged[r[idf]] = r
        if key == "blocks":  # only the freshest run can have an active block
            fresh = {r["id"] for r in new.get("blocks", [])}
            for r in merged.values():
                if r["id"] not in fresh:
                    r["active"] = False
        sort_key = {"blocks": "start", "days": "date", "sessions": "start", "hours": "hour"}[key]
        out[key] = sorted(merged.values(), key=lambda r: r.get(sort_key) or "")
    return out


def to_rows(usage: dict) -> list[dict]:
    """Flat rows for the artifact database: one doc per record, keyed by kind+id."""
    rows = []
    for kind, idf in (("block", "id"), ("day", "date"), ("session", "id"), ("hour", "hour")):
        for r in usage.get(kind + "s", []):
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
    a = ap.parse_args()

    new = build(a.tz, a.claude_dir, a.offline)
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

    n = {k: (len(old.get(k, [])), len(usage[k])) for k in ("blocks", "days", "sessions", "hours")}
    first = usage["days"][0]["date"] if usage["days"] else "-"
    print(f"wrote {a.out}  (data from {first})")
    for k, (o, c) in n.items():
        print(f"  {k:9s} {c:6d}  (+{c - o})")


if __name__ == "__main__":
    main()
