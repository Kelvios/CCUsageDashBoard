#!/usr/bin/env bash
# Phase 1 local setup (macOS / Linux / WSL). Run from the repo folder: ./setup.sh
set -euo pipefail
CL="$HOME/.claude"; S="$CL/settings.json"; mkdir -p "$CL"
[ -f "$S" ] && cp "$S" "$S.bak"
python3 - "$S" <<'PY'
import json, os, sys
p = sys.argv[1]
d = json.load(open(p)) if os.path.exists(p) and os.path.getsize(p) else {}
d["cleanupPeriodDays"] = 365
json.dump(d, open(p, "w"), indent=2)
print(f"cleanupPeriodDays=365 set in {p}")
PY
python3 - "$CL/projects" <<'PY'
import glob, os, sys, time
f = glob.glob(os.path.join(sys.argv[1], "**", "*.jsonl"), recursive=True)
if f:
    o = min(os.path.getmtime(x) for x in f)
    print(f"{len(f)} log files; oldest last-modified {time.strftime('%Y-%m-%d', time.localtime(o))} ({int((time.time()-o)/86400)} days ago)")
else:
    print("No ~/.claude/projects/**/*.jsonl found")
PY
npm i -g ccusage
ccusage claude daily --breakdown --timezone Australia/Brisbane
python3 collect.py
