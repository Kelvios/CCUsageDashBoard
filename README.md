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

Requirements: Node 18 or later, and Python 3.9 or later. Windows Python ships
without timezone data. For Australia/Brisbane (no daylight saving) the collector
falls back to a fixed UTC+10 offset. Any other `--tz` needs `python -m pip install tzdata`.

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

`load()` now does this: when the db runtime is present it reads the `usage`
collection (paged by `key`, 1000 docs per query) and falls back to
`data/usage.json` for local preview. The published artifact is private.

### Daily collection and push

1. **06:00, Windows Task Scheduler** runs `daily.ps1` (collect only):
   `python collect.py --changed-rows data\pending\<yyyyMMdd-HHmm>.json` (plus the
   Conductor's `logs\usage.md` when present). It logs `COLLECT OK <file> (<n> rows)` or
   `COLLECT FAILED` to `data\daily.log` and writes the outcome to `data\last-collect.json`.
2. **06:30, claude.ai scheduled task "Usage dashboard daily push"** wakes the Claude
   session running `claude remote-control` in this folder. It reads `data\last-collect.json`
   and uploads `data\pending\*.json` to the artifact's `usage` collection (ArtifactData
   batch writes keyed by `key`) **only if the collection is from today and succeeded**.
   Otherwise it uploads nothing and reports why. A file is deleted only after all its rows
   are written; anything left goes up after the next successful collection.

Register the collector once (runs only while you are logged on):

```powershell
schtasks /Create /SC DAILY /ST 06:00 /TN "ClaudeUsageDashboard" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File D:\repositories\CCUsageDashboard\daily.ps1"
```

Requirements: the PC is on at 06:00–06:30 and `claude remote-control` stays running in
this folder. The scheduled task is bound to that one remote-control session; if it is
restarted, the task must be pointed at the new session. (Headless `claude -p` cannot use
ArtifactData, which is why the upload runs in the remote-control session.)

To catch up during the day, tell that session: "run the usage push now".

## Plan-limit readings from the Conductor (row kind `usage`)

`python collect.py --usage-log <Conductor>/logs/usage.md` parses one reading per line
(TEMPLATES.md B5):

```
YYYY-MM-DD HH:MM | window <n>% resets <hh:mm> | week <n>% resets <day hh:mm> | limited: yes|no | running: <workers>
```

Each line becomes a `usage` row (`key` = `usage:YYYY-MM-DDTHH:MM`) with `window_pct`,
`window_resets`, `week_pct`, `week_resets` (reset times resolved to the next occurrence
after the reading, in the collector's timezone), `limited` and `running` (list of worker names).
`window ?` / `week ?` are stored as `null` and shown as *unknown*, never 0. Lines that
start with a date but don't match are skipped and counted. `daily.ps1` passes
`D:\Repositories\Conductor\logs\usage.md` when that file exists; the readings live on the
Conductor's `dev/campaign1-play` branch, so that checkout must be on it (or the path changed).

The dashboard's **Plan limits** section (hidden when there are no readings) shows:
- **Five-hour windows**: one row per window (grouped by reset time), latest fill against a
  92% dashed line; windows with `limited: yes` in red, with when they ran out and the
  `running` workers named.
- **Weeks**: latest fill per week against 92%, plus an on-pace mark (92% × fraction of the
  week elapsed) and the projected fill at reset.

The target is `CONFIG.limitTarget` (92) in `dashboard.html`.
