# Conductor guidance: spend headroom on better models

For the Conductor. This adds to the working-patterns brief
(`briefs/2026-11-02-cond-working-patterns.md`, Part A) and does not replace it. The
92% pacing bands, the Sonnet floor for Builders, the size-band budgets, and the rule
against unattended Fable all still apply.

## Why

From 1 Oct to 2 Nov there were 106 five-hour blocks. The median block used about 8% of
the largest one. 91 blocks stayed under a quarter of it, and about a third of all hours
had no block running at all. Allowance a window doesn't use is lost when it resets. Most
windows have room to spare, so that room should go to better models on the work where
quality pays off. Adding more parallel Sonnet workers is the wrong use for it, because
the digests show parallel workers fighting over memory.

## 1. Measure headroom before every dispatch

Use the latest reading in `logs/usage.md`:

- **Window headroom** = 92 − window %
- **Time left** = window reset − now
- **Week guard** = the projected week fill at reset (week % ÷ fraction of the week elapsed).
  The dashboard's Plan limits panel shows this.

If the reading is `?` or more than 90 minutes old, take a fresh reading. If you still
can't read the figures, use the dashboard's "Current 5-hour block" panel as a stand-in.
Treat the window as having headroom only when the block so far is under 25% of the
peak block and at least 3 hours are left. Otherwise make no upgrade.

## 2. Upgrade rules

| Condition | What to do with the next eligible dispatch |
|---|---|
| Week guard > 92% | No upgrades, whatever the window shows |
| Window ≥ 70%, or headroom < 20 points | No upgrades (the finish-only band) |
| Headroom 20–40 points, ≥ 2h left | Upgrade **small and medium** briefs one tier |
| Headroom > 40 points, ≥ 2h left | Upgrade any eligible brief one tier |
| < 90 min left in the window, headroom > 30 points, nothing queued | Start a **review run** (see 4) instead of leaving the window idle |

"One tier" means:
- **Sonnet to Opus** for Builders and fix runs.
- **Opus to Fable** for plans, cycle briefs and reviews, attended only.
- **Opus at a higher effort level** when Fable isn't allowed, such as unattended or overnight runs.

Upgrade one dispatch at a time. Take a new reading before upgrading the next one.

## 3. Which work gets upgraded first

1. **Plans and briefs for open problems** (Architect, Chronicler, cycle-start). Use Fable
   when Kelvin is around. The digests trace most repeat failures back to defects in the briefs.
2. **Reviews before a merge.** Use Opus by default, and Fable for multi-role or security reviews.
   The reviewer gets only the diff and the brief.
3. **Builders on hard-to-undo code:** the rules engine, TimeShift, schema or save-format
   changes. Use Opus.
4. **Fix runs after a failed acceptance test.** Use Opus, so the fix lands on the first try.

**Never upgrade:** docs, boilerplate, filing, formatting, lint fixes, the board regenerate,
or map-summary refreshes. These stay on Sonnet whatever the headroom.

## 4. Review runs for the end of a window

When a window is about to reset with headroom unused and nothing queued, start one Opus
reviewer on the most recent merged diff that hasn't been reviewed yet, with its brief.
Set a small budget and file its findings as a normal report. The run turns allowance
that would otherwise expire into code quality.

## 5. Record it

Add to the dispatch line: `upgrade: <from>→<to>, headroom <n> pts, <h>h left`. Keep
filing the hourly `logs/usage.md` readings so the dashboard can show each window's
fill against the 92% line.

After two weeks, compare upgraded and non-upgraded dispatches on three counts: whether a
fix run was needed, whether the acceptance test passed first time, and cost per brief.
Report the result in the digest. If upgrades don't reduce fix runs, drop rule 3.3 first.
