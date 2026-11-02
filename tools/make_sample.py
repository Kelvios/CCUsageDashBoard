#!/usr/bin/env python3
"""Generate synthetic data/sample-usage.json (same shape as collect.py output)
so dashboard.html can be previewed without real logs. Not real usage."""
import datetime as dt, json, os, random
from collections import defaultdict
from zoneinfo import ZoneInfo

random.seed(7)
TZ = ZoneInfo("Australia/Brisbane")
MODELS = {  # $ per Mtok: in, out, cache_read, cache_write
    "claude-fable-5-1": (15, 75, 1.5, 18.75),
    "claude-opus-5-5": (5, 25, 0.5, 6.25),
    "claude-sonnet-5-5": (3, 15, 0.3, 3.75),
    "claude-haiku-5-5": (1, 5, 0.1, 1.25),
}
PROJECTS = {"api-server": .6, "mobile-app": .45, "docs-site": .15, "infra": .35, "ccusagedashboard": .55}
CATS = ("in", "out", "cache_read", "cache_write")
now = dt.datetime.now(TZ).replace(minute=random.randint(5, 50), second=0, microsecond=0)
start_day = (now - dt.timedelta(days=69)).date()

def cost(m, t): return sum(t[c] * p for c, p in zip(CATS, MODELS[m])) / 1e6
def add(d, m, t):
    x = d.setdefault(m, dict.fromkeys(CATS, 0))
    for c in CATS: x[c] += t[c]

events = []  # (ts, session, project, model, tokens)
sid = 0
for i in range(70):
    day = start_day + dt.timedelta(days=i)
    wk = day.weekday()
    n_sess = random.choice([0, 1, 2, 2, 3, 4]) if wk < 5 else random.choice([0, 0, 1, 2])
    fable_bias = min(0.75, 0.15 + i / 110)  # Fable share creeping up
    for _ in range(n_sess):
        sid += 1
        proj = random.choices(list(PROJECTS), weights=[3, 2, 1, 2, 2])[0]
        hour = random.choice([7, 8, 9, 9, 10, 11, 13, 14, 15, 16, 19, 20, 21])
        st = dt.datetime.combine(day, dt.time(hour, random.randint(0, 59)), TZ)
        if st > now: continue
        model = random.choices(list(MODELS), weights=[fable_bias, .45, .35 - fable_bias / 3, .08])[0]
        big = 3.5 if random.random() < .05 else 1
        for k in range(int(random.randint(15, 90) * big)):
            ts = st + dt.timedelta(minutes=k * random.uniform(1, 4))
            if ts > now: break
            m = "claude-haiku-5-5" if random.random() < .12 else model
            ctx = random.randint(20_000, 120_000)
            hit = PROJECTS[proj] + random.uniform(-.15, .15)
            t = {"in": random.randint(2, 40), "out": random.randint(200, 4000),
                 "cache_read": int(ctx * max(hit, .02)), "cache_write": int(ctx * (1 - max(hit, .02)) * .15)}
            events.append((ts, f"sample-{sid:04d}", proj, m, t))
# guarantee an in-progress block for the "Now" panel
st = now - dt.timedelta(minutes=130)
for k in range(60):
    ts = st + dt.timedelta(minutes=k * 2)
    if ts > now: break
    events.append((ts, "sample-live", "ccusagedashboard", "claude-fable-5-1" if k % 3 else "claude-opus-5-5",
                   {"in": 10, "out": 2500, "cache_read": 70_000, "cache_write": 6_000}))
events.sort(key=lambda e: e[0])

# blocks: ccusage semantics (start floored to hour, 5h, gap closes)
blocks, cur = [], None
for e in events:
    ts = e[0].astimezone(dt.timezone.utc)
    if cur is None or ts >= cur["_end"] or (ts - cur["_last"]) > dt.timedelta(hours=5):
        s = ts.replace(minute=0, second=0)
        cur = {"_start": s, "_end": s + dt.timedelta(hours=5), "_last": ts, "tokens": {}, "cost": 0, "entries": 0, "cost_by_model": defaultdict(float)}
        blocks.append(cur)
    cur["_last"] = ts; cur["entries"] += 1
    add(cur["tokens"], e[3], e[4]); c = cost(e[3], e[4]); cur["cost"] += c; cur["cost_by_model"][e[3]] += c
iso = lambda d: d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
out_blocks = [{"id": iso(b["_start"]), "start": iso(b["_start"]), "end": iso(b["_end"]), "last": iso(b["_last"]),
               "active": b["_end"] > now, "entries": b["entries"], "tokens": b["tokens"], "cost": round(b["cost"], 6),
               "cost_by_model": {k: round(v, 6) for k, v in b["cost_by_model"].items()}} for b in blocks]

days, hours, sess = {}, {}, {}
for ts, s, p, m, t in events:
    d = ts.strftime("%Y-%m-%d"); h = ts.strftime("%Y-%m-%dT%H"); c = cost(m, t)
    dd = days.setdefault(d, {"date": d, "tokens": {}, "cost": 0, "_s": set()}); add(dd["tokens"], m, t); dd["cost"] += c; dd["_s"].add(s)
    hh = hours.setdefault(h, {"hour": h, "tokens": {}, "cost": 0}); add(hh["tokens"], m, t); hh["cost"] += c
    ss = sess.setdefault(s, {"id": s, "project": p, "start": iso(ts), "end": iso(ts), "tokens": {}, "cost": 0, "_c": defaultdict(float)})
    ss["end"] = iso(ts); add(ss["tokens"], m, t); ss["cost"] += c; ss["_c"][m] += c
for d in days.values(): d["sessions"] = len(d.pop("_s")); d["cost"] = round(d["cost"], 6)
for h in hours.values(): h["cost"] = round(h["cost"], 6)
for s in sess.values():
    cm = s.pop("_c"); s["model_mix"] = {k: round(v / s["cost"], 4) for k, v in cm.items()}; s["cost"] = round(s["cost"], 6)

# Conductor-style plan-limit readings (row kind "usage")
readings = []
for i, (h, w, k, lim, run) in enumerate([(-30, 55, 38, False, ["api-server"]), (-27, 93, 41, True, ["api-server", "infra"]),
                                          (-20, None, None, False, []), (-8, 71, 52, False, ["mobile-app"]), (-1, 34, 58, False, ["ccusagedashboard"])]):
    t = (now + dt.timedelta(hours=h)).replace(second=0, microsecond=0)
    rs = (t.replace(minute=0) + dt.timedelta(hours=2 + i % 3))
    nxt = (t + dt.timedelta(days=(3 - t.weekday()) % 7 or 7)).replace(hour=9, minute=0)
    readings.append({"id": t.strftime("%Y-%m-%dT%H:%M"), "at": t.isoformat(timespec="minutes"), "window_pct": w,
                     "window_resets": rs.isoformat(timespec="minutes") if w is not None else None, "week_pct": k,
                     "week_resets": nxt.isoformat(timespec="minutes") if k is not None else None, "limited": lim, "running": run})
out = {"readings": readings, "generated": now.isoformat(timespec="seconds"), "timezone": "Australia/Brisbane", "sample": True,
       "blocks": out_blocks, "days": sorted(days.values(), key=lambda x: x["date"]),
       "sessions": sorted(sess.values(), key=lambda x: x["start"]), "hours": sorted(hours.values(), key=lambda x: x["hour"])}
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "sample-usage.json")
json.dump(out, open(p, "w"), indent=0)
print(p, {k: len(v) for k, v in out.items() if isinstance(v, list)})
