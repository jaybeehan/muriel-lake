#!/usr/bin/env python3
"""Build the data file for the Muriel Lake water level page.

Pulls water levels for Water Survey of Canada station 06AC007 (Muriel Lake
near Gurneyville, AB), merges the approved historical record with the
provisional real-time record, fills gaps by linear interpolation, and works
out the quarterly tables, year-to-year changes and July 1 projections.

Data sources
  * Approved historical daily means (HYDAT), via the GeoMet OGC API:
      https://api.weather.gc.ca/collections/hydrometric-daily-mean
  * Provisional real-time readings (about the last 18 months), via WaterOffice:
      https://wateroffice.ec.gc.ca/services/real_time_data/csv/inline

Approved data always wins. Real-time data only fills dates the approved
record doesn't have yet, so when Environment Canada publishes a new year of
approved data the page switches to it automatically on the next run.

Uses only the Python standard library. Writes site/data/lake.json.
"""

import bisect
import csv
import datetime as dt
import io
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATION = "06AC007"
STATION_NAME = "Muriel Lake near Gurneyville"
HYDAT_URL = "https://api.weather.gc.ca/collections/hydrometric-daily-mean/items"
REALTIME_URL = "https://wateroffice.ec.gc.ca/services/real_time_data/csv/inline"
PAGE_SIZE = 10000
REALTIME_MONTHS = 19          # WaterOffice keeps roughly 18 months
LST_OFFSET = dt.timedelta(hours=7)   # Alberta local standard time = UTC-7 (what HYDAT uses)
QUARTER_MONTHS = (1, 4, 7, 10)
QUARTER_NAMES = ("Jan 1", "Apr 1", "Jul 1", "Oct 1")
PROJECTION_YEARS = 50
OUT = Path(__file__).resolve().parent.parent / "site" / "data" / "lake.json"


def fetch(url, params, tries=4):
    full = url + "?" + urllib.parse.urlencode(params, safe="[]:,", quote_via=urllib.parse.quote)
    req = urllib.request.Request(full, headers={"User-Agent": "muriel-lake-levels (github.com/jaybeehan/muriel-lake)"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read().decode("utf-8-sig", errors="replace")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == tries - 1:
                raise
            print(f"  retrying after error: {e}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))


# ---------------------------------------------------------------- fetching

def fetch_hydat():
    """Approved daily means: {date: level_m}."""
    out = {}
    offset = 0
    while True:
        text = fetch(HYDAT_URL, {
            "STATION_NUMBER": STATION, "f": "csv", "limit": PAGE_SIZE,
            "offset": offset, "sortby": "DATE", "properties": "DATE,LEVEL",
        })
        rows = list(csv.DictReader(io.StringIO(text)))
        for row in rows:
            d, lev = (row.get("DATE") or "").strip(), (row.get("LEVEL") or "").strip()
            if d and lev:
                try:
                    out[dt.date.fromisoformat(d[:10])] = float(lev)
                except ValueError:
                    pass
        print(f"  HYDAT offset {offset}: {len(rows)} rows")
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


def fetch_realtime(today):
    """Provisional readings averaged to local-standard-time days: {date: level_m}."""
    sums, counts = {}, {}
    first_of_month = today.replace(day=1)
    for k in range(REALTIME_MONTHS):
        y, m = first_of_month.year, first_of_month.month - k
        while m < 1:
            m += 12
            y -= 1
        start = dt.date(y, m, 1)
        end = dt.date(y + (m == 12), m % 12 + 1, 1)
        try:
            text = fetch(REALTIME_URL, {
                "stations[]": STATION, "parameters[]": 46,
                "start_date": f"{start.isoformat()} 07:00:00",
                "end_date": f"{end.isoformat()} 07:00:00",
            })
        except Exception as e:  # a missing month shouldn't sink the whole run
            print(f"  real-time {start:%Y-%m}: failed ({e})", file=sys.stderr)
            continue
        reader = csv.reader(io.StringIO(text))
        next(reader, None)  # header
        n = 0
        for row in reader:
            if len(row) < 4:
                continue
            try:
                ts = dt.datetime.fromisoformat(row[1].strip().replace("Z", "+00:00"))
                val = float(row[3])
            except ValueError:
                continue
            day = (ts.replace(tzinfo=None) - LST_OFFSET).date()
            if day >= end:   # the closing reading belongs to next month's chunk
                continue
            sums[day] = sums.get(day, 0.0) + val
            counts[day] = counts.get(day, 0) + 1
            n += 1
        print(f"  real-time {start:%Y-%m}: {n} readings")
    return {d: sums[d] / counts[d] for d in sums}


# ---------------------------------------------------------------- maths

def merge(hydat, realtime):
    """Approved data first; real-time only for dates HYDAT doesn't have."""
    series = {d: (v, "approved") for d, v in hydat.items()}
    for d, v in realtime.items():
        if d not in series:
            series[d] = (v, "provisional")
    dates = sorted(series)
    return dates, [series[d][0] for d in dates], [series[d][1] for d in dates]


def interpolate(dates, levels, sources, target):
    """Level on `target`: measured if present, otherwise straight line between neighbours."""
    i = bisect.bisect_left(dates, target)
    if i < len(dates) and dates[i] == target:
        return levels[i], "measured" if sources[i] == "approved" else "measured (provisional)"
    if i == 0 or i == len(dates):
        return None, None
    d0, d1 = dates[i - 1], dates[i]
    v0, v1 = levels[i - 1], levels[i]
    frac = (target - d0).days / (d1 - d0).days
    return v0 + (v1 - v0) * frac, "interpolated"


def percentile(values, p):
    """Same as Google Sheets PERCENTILE (inclusive, linear)."""
    s = sorted(values)
    pos = (len(s) - 1) * p
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def linear_fit(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, my - slope * mx


def r(x, n=3):
    return None if x is None else round(x, n)


def build():
    today = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - LST_OFFSET).date()
    print("Fetching approved historical data…")
    hydat = fetch_hydat()
    if not hydat:
        sys.exit("No historical data came back; leaving the old data in place.")
    print("Fetching real-time data…")
    realtime = fetch_realtime(today)

    dates, levels, sources = merge(hydat, realtime)
    print(f"Merged series: {len(dates)} days, {dates[0]} to {dates[-1]}")

    # ---- quarterly table
    quarterly = []
    for year in range(dates[0].year, dates[-1].year + 1):
        vals, srcs = [], []
        for m in QUARTER_MONTHS:
            v, s = interpolate(dates, levels, sources, dt.date(year, m, 1))
            vals.append(r(v))
            srcs.append(s)
        quarterly.append({"year": year, "level": vals, "source": srcs})

    # ---- change tables (cm): total since first value in each column, and vs previous year
    first = [next((q["level"][c] for q in quarterly if q["level"][c] is not None), None) for c in range(4)]
    for i, q in enumerate(quarterly):
        q["total_cm"] = [r((v - first[c]) * 100, 1) if v is not None else None for c, v in enumerate(q["level"])]
        prev = quarterly[i - 1]["level"] if i else [None] * 4
        q["yoy_cm"] = [r((v - p) * 100, 1) if v is not None and p is not None else None
                       for v, p in zip(q["level"], prev)]

    # ---- July 1 projection (same method as the Google Sheet)
    last = quarterly[-1]
    anchor_c = max(c for c in range(4) if last["level"][c] is not None)
    anchor_level = last["level"][anchor_c]
    shift = 1 if anchor_c >= 2 else 0          # anchored on Jul/Oct -> project next year's July
    target_year = last["year"] + shift
    deltas = []
    for i, q in enumerate(quarterly):
        j = i + shift
        if j < len(quarterly):
            a, b = q["level"][anchor_c], quarterly[j]["level"][2]
            if a is not None and b is not None:
                deltas.append(b - a)
    jul = [(q["year"], q["level"][2]) for q in quarterly if q["level"][2] is not None]
    last_jul_year, last_jul = jul[-1]
    scen = []
    for name, p in [("Driest on record", 0), ("Dry year", .10), ("Below normal", .25), ("Typical (median)", .50),
                    ("Above normal", .75), ("Wet year", .90), ("Wettest on record", 1)]:
        lev = anchor_level + percentile(deltas, p)
        scen.append({"name": name, "percentile": round(p * 100), "level": r(lev),
                     "change_cm": r((lev - anchor_level) * 100, 1),
                     "vs_last_jul_cm": r((lev - last_jul) * 100, 1)})

    ys, vs = [y for y, _ in jul], [v for _, v in jul]
    s_all, i_all = linear_fit(ys, vs)
    s_10, i_10 = linear_fit(ys[-10:], vs[-10:])
    med_change = statistics.median(b - a for a, b in zip(vs, vs[1:]))
    long_term = []
    for y in range(target_year, target_year + PROJECTION_YEARS):
        long_term.append({"year": y,
                          "trend_all": r(s_all * y + i_all),
                          "trend_10": r(s_10 * y + i_10),
                          "typical": r(last_jul + med_change * (y - last_jul_year))})

    # ---- level relative to earliest quarterly reading
    qbase = next((q["year"], c, q["level"][c]) for q in quarterly for c in range(4) if q["level"][c] is not None)

    data = {
        "station": STATION,
        "station_name": STATION_NAME,
        "updated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z",
        "latest": {"date": dates[-1].isoformat(), "level": r(levels[-1]),
                   "source": "approved" if sources[-1] == "approved" else "provisional"},
        "approved_until": max(hydat).isoformat(),
        "quarter_names": QUARTER_NAMES,
        "quarterly": quarterly,
        "quarterly_base": {"date": dt.date(qbase[0], QUARTER_MONTHS[qbase[1]], 1).isoformat(), "level": qbase[2]},
        "daily": {
            "base_date": dates[0].isoformat(),
            "base_level": r(levels[0]),
            "dates": [d.isoformat() for d in dates],
            "rel_m": [r(v - levels[0]) for v in levels],
        },
        "projection": {
            "anchor": {"date": dt.date(last["year"], QUARTER_MONTHS[anchor_c], 1).isoformat(), "level": anchor_level},
            "target": dt.date(target_year, 7, 1).isoformat(),
            "years_of_history": len(deltas),
            "last_july": {"year": last_jul_year, "level": last_jul},
            "scenarios": scen,
            "median_july_change_cm": r(med_change * 100, 1),
            "long_term": long_term,
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    print(f"Wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    build()
