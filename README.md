# Claude Usage Dashboard

Shows Claude Code token and model usage trends so you can choose a model per
session and pace usage across 5-hour blocks and the week.

- `collect.py` reads Claude Code logs through ccusage and writes `data/usage.json`.
- `dashboard.html` is one self-contained page (Chart.js from cdnjs). It reads
  `data/usage.json`, or falls back to `data/sample-usage.json`.

Only aggregate numbers leave the logs: timestamps, model names, token counts,
session ids and the working-directory basename. The collector never reads
message content into its output.

## Setup (Phase 1, on your machine)

```powershell
$repo = "D:\repositories\CCUsageDashboard"
if (Test-Path "$repo\.git") { git -C $repo pull origin main } else { git clone https://github.com/Kelvios/CCUsageDashBoard $repo }
cd $repo
powershell -ExecutionPolicy Bypass -File .\setup.ps1     # Windows
# ./setup.sh                                              # macOS / Linux / WSL
```

The setup script:
1. merges `cleanupPeriodDays: 365` into `~/.claude/settings.json` and writes a `.bak` copy first
2. reports how far back `~/.claude/projects/**/*.jsonl` goes
3. runs `npm i -g ccusage` and then `ccusage claude daily --breakdown`
4. runs the first `python collect.py`, which backfills every log that still exists

Requirements: Node 18 or later, and Python 3.9 or later (it uses `zoneinfo`). On
Windows you may also need `pip install tzdata`.

## Collector

```
python collect.py [--out data/usage.json] [--tz Australia/Brisbane] [--offline] [--rows data/rows.json]
```

- Runs `ccusage claude blocks|daily --breakdown|session --json --timezone Australia/Brisbane`.
  The `claude` subcommand keeps Codex, Gemini and other agents out of the data.
- Also scans the JSONL usage fields, because ccusage doesn't expose two things the
  dashboard needs: hourly buckets for the heatmap, and the per-model split of each block.
  The collector takes ccusage's exact day and block costs and splits them across
  hours and models using standard price ratios (output 5×, cache write 1.25×, cache read 0.1×).
- **Idempotent.** Each run merges into the existing file, keyed by block id, date,
  session id and hour. Records that are still in the logs get refreshed (the active
  block and today keep growing). Records whose logs have been pruned are kept.
- `--rows` also writes a flat array `[{kind, key, ...}]`, one row per artifact-db document.
  `key` is `block:<id>`, `day:<date>`, `session:<id>` or `hour:<YYYY-MM-DDTHH>`.

Output shape (a superset of the brief's: it adds `hours`, `cost_by_model`, `active` and `last`):

```jsonc
{ "generated": "2026-11-02T07:57:36+10:00", "timezone": "Australia/Brisbane",
  "blocks":   [{ "id", "start", "end", "last", "active", "entries",
                 "tokens": { "<model>": { "in", "out", "cache_read", "cache_write" } },
                 "cost", "cost_by_model": { "<model>": usd } }],
  "days":     [{ "date", "tokens": {…}, "cost", "sessions" }],
  "sessions": [{ "id", "project", "start", "end", "model_mix": { "<model>": costShare },
                 "tokens": {…}, "cost" }],
  "hours":    [{ "hour": "2026-11-02T07", "tokens": {…}, "cost" }] }
```

## Dashboard

Preview locally:

```
python -m http.server 8000
# http://localhost:8000/dashboard.html          real data
# http://localhost:8000/dashboard.html?sample   synthetic sample (python tools/make_sample.py)
```

Panels:
- **Now:** the current block's burn, time left, projected end-of-block against your peak block; this week's projected total against your peak week; the last 7 days' model mix.
- **Advice:** rule-based flags (see below).
- **Trend:** daily usage stacked by model; rolling 7-day Fable share and cache hit rate.
- **Patterns:** an hour × weekday heatmap; usage by project, stacked by model.
- **5-hour blocks:** burn per block with a 2× median line, plus the sessions to review.

A toggle switches between API-equivalent $ and raw tokens. Cost is the better
proxy for plan-limit weight because it weights Fable, Opus and Sonnet tokens, and
cheap cache reads, the way limits do. All projections are relative to your own
observed peaks. No absolute caps are assumed.

Advice rules (thresholds live in `CONFIG` at the top of the script):
| Rule | Trigger |
|---|---|
| Route to Sonnet | Fable share (7d) > 50% and weekly pace > 100% of peak week (warning above 85%) |
| Block running hot | projected end-of-block > largest block so far |
| Cache churn | project cache hit < 30% (projects with ≥ 3% of the range only) |
| Outlier blocks | block > 2× median → sessions overlapping it go into "Sessions to review" |
| Headroom | weekly pace < 60% |

The week starts Monday 00:00 Brisbane time (`CONFIG.weekStartsOn`). Until 3 days
of the week have passed, the projection blends week-to-date with the trailing
7-day rate.

### Feeding it from the artifact db (Phase 4)

The page exposes `window.renderUsage(data)`. It accepts the `usage.json` object
or a flat array of `--rows` rows (`kind` ∈ block/day/session/hour, plus an
optional `kind: "meta"` row carrying `generated` and `timezone`). The db-backed
version only has to swap `load()` for a db query and pass the rows in.
