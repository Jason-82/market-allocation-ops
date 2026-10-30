#!/usr/bin/env python3
"""Liquidity/Credit module calculator (stdlib only).

Implements supervisor/modules/liquidity_credit.yaml exactly:
  current_scoring_methodology (formula, component weights, every anchor table and overlay,
  genuine_scarcity_test, thresholds), data_pipeline (series ids, asof_selection_rule,
  incomplete_row_rule, splice_or_proxy_rules), calculations, and
  supervisor_integration.confidence_definition.

Arithmetic is carried at full precision; floor(x + 0.5) is applied only to the final score.
Basis-point quantities are rounded to 1e-6 bp before threshold tests so float noise such as
(3.95 - 3.90) * 100 = 5.0000000000000036 cannot flip a ">5 bp" test.

Session counting: 5/20/63-session changes for ICE OAS use the SIFMA bond-market calendar and
VIXCLS completed closes use the NYSE/Cboe calendar (holiday/weekend prints are dropped).

Confidence, ICE truncation: the agent-of-record convention deducts 2 whenever the FRED ICE OAS
history is the truncated ~3-year window (history starts after ICE_FULL_HISTORY_BEFORE). The
literal reading (deduct only when truncation prevents the 3-year context ending at the credit
cutoff) is computed and disclosed but not applied; switching needs Jason's approval.

Staleness: HY, VIX and the other core daily series are aged in Fed business days against
--report-date (not against the as-of found in the data). HY or VIX more than 3 business days
stale makes the score PROVISIONAL (incomplete_row_rule); more than 5 drops the series.

Data: FRED fredgraph.csv downloads (observation_date,<SERIES>), missing values "." or "".
fredgraph.csv ignores vintage_date, so a past date is recomputed from CURRENT vintages
(NFCI/STLFSI4 are revised weekly; this is disclosed, never tuned away).

Usage:
    python3 tools/liquidity_credit.py --fetch DIR
    python3 tools/liquidity_credit.py --cache DIR [--facts FACTS.json] [--asof YYYY-MM-DD]
        [--report-date YYYY-MM-DD] [--credit-asof YYYY-MM-DD]
        [--prior-score 10 --prior-asof 2026-09-17] [--formalized-updates D1,D2,...] [--json]
    python3 tools/liquidity_credit.py --cache DIR --self-test

Decision support only; not financial advice.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import ssl
import statistics
import sys
import time
import urllib.request

# --------------------------------------------------------------------------------------
# Spec constants (supervisor/modules/liquidity_credit.yaml). Keep in sync with the spec.
# --------------------------------------------------------------------------------------
SERIES = [
    "BAMLH0A0HYM2", "BAMLC0A0CM", "BAMLC0A4CBBB", "BAA10Y", "VIXCLS",
    "WALCL", "WRESBAL", "WTREGEN", "RRPONTSYD", "RPONTSYD", "SOFR", "IORB",
    "WLCFLPCL", "SWPT", "NFCI", "STLFSI4",
]
CORE_SERIES = ["BAMLH0A0HYM2", "BAMLC0A0CM", "BAMLC0A4CBBB", "BAA10Y", "VIXCLS",
               "WRESBAL", "WTREGEN", "SOFR", "IORB"]
AUX_SERIES = ["WALCL", "RRPONTSYD", "RPONTSYD", "WLCFLPCL", "SWPT", "NFCI", "STLFSI4"]
CORE_DAILY = ["BAMLH0A0HYM2", "BAMLC0A0CM", "BAMLC0A4CBBB", "BAA10Y", "VIXCLS", "SOFR"]

TOP_WEIGHTS = {"credit": 0.50, "volatility": 0.25, "liquidity": 0.25}
CREDIT_WEIGHTS = {"HY": 0.50, "IG": 0.15, "BBB": 0.20, "BAA10Y": 0.15}
RESERVE_WEIGHTS = {"abs": 0.40, "chg4w": 0.30, "onrrp": 0.15, "policy": 0.15}
LFS_WEIGHTS = {"reserve": 0.65, "funding": 0.20, "facility": 0.10, "broad": 0.05}
FUNDING_WEIGHTS = {"sofr": 0.60, "repo": 0.40}
FACILITY_WEIGHTS = {"primary": 0.60, "swaps": 0.40}
BROAD_WEIGHTS = {"nfci": 0.50, "stlfsi4": 0.50}
GENUINE_SCARCITY_POINTS = 10.0
VOL_CAP = 40.0

# packet_template.md contract: MODULE must be one of the five official module names.
MODULE_NAME = "Liquidity and Credit"

# Governance (agent of record). Set at the Supervisor's direction on 2026-10-30.
GOVERNANCE_NOTE = ("Supervisor self-run at Jason's request (2026-10-30: 'it would be much easier "
                   "going forward if you could simply run them all yourself'), replacing the "
                   "external agent; methodology unchanged.")

# incomplete_row_rule staleness thresholds (Fed business days, measured against --report-date).
HY_VIX_PROVISIONAL_BD = 3   # HY OAS or VIX older than this: no fully confident official score
CREDIT_MAX_STALE_BD = 5     # credit series older than this is treated as missing
VIX_MAX_STALE_BD = 5        # VIX older than this is treated as missing
CORE_DAILY_STALE_BD = 2     # confidence_definition: -5 for any core daily series > 2 bd stale

# methodology_history 2026-08-24: disclose the 1-3 point transition in NOTES for the first
# three updates under the formalized implementation. Append each official update's market
# as-of date here (or pass --formalized-updates) so the count stays explicit.
FORMALIZATION_DATE = "2026-08-24"
FORMALIZED_UPDATE_DATES = ["2026-09-17", "2026-10-28"]
TRANSITION_UPDATES = 3

# Session-calendar implementation change; its disclosure prints for report dates on/after this.
IMPL_CHANGE_DATE = "2026-10-30"

# ICE truncation convention (agent of record): full ICE OAS history begins 1996-12-31, so a
# FRED history starting after this date is the truncated ~3-year window and costs 2 points.
ICE_FULL_HISTORY_BEFORE = "2000-01-01"

CREDIT_IDS = {"HY": "BAMLH0A0HYM2", "IG": "BAMLC0A0CM", "BBB": "BAMLC0A4CBBB",
              "BAA10Y": "BAA10Y"}

A_HY = [(2.50, 0), (3.25, 20), (4.00, 40), (5.00, 60), (7.00, 80), (10.00, 100)]
A_IG = [(0.65, 0), (0.90, 20), (1.20, 40), (1.50, 60), (2.25, 80), (3.50, 100)]
A_BBB = [(0.80, 0), (1.10, 20), (1.50, 40), (2.00, 60), (3.00, 80), (4.50, 100)]
A_BAA = [(1.25, 0), (1.75, 20), (2.25, 40), (3.00, 60), (4.00, 80), (5.50, 100)]
CREDIT_ANCHORS = {"HY": A_HY, "IG": A_IG, "BBB": A_BBB, "BAA10Y": A_BAA}
A_VIX = [(12, 0), (15, 10), (20, 30), (25, 50), (30, 65), (40, 85), (60, 100)]
A_WRESBAL_T = [(2.20, 100), (2.40, 95), (2.60, 80), (2.80, 60), (3.00, 35), (3.15, 15),
               (3.30, 0)]
A_WRESBAL_4W_B = [(-500, 100), (-300, 85), (-200, 70), (-100, 50), (-50, 30), (0, 10),
                  (100, 0)]
A_ONRRP_B = [(0, 60), (1, 60), (5, 45), (25, 25), (100, 10), (500, 0)]
A_SOFR_IORB_BP = [(0, 0), (5, 15), (10, 35), (20, 60), (40, 85), (75, 100)]
A_REPO_B = [(1, 0), (10, 20), (25, 40), (50, 60), (100, 80), (200, 100)]
A_PRIMARY_B = [(10, 0), (25, 20), (50, 40), (100, 65), (250, 85), (500, 100)]
A_SWAPS_B = [(1, 0), (5, 20), (25, 40), (100, 65), (250, 85), (500, 100)]
A_NFCI = [(-0.50, 0), (0.00, 20), (0.50, 50), (1.00, 75), (2.00, 100)]
A_STLFSI4 = [(-0.50, 0), (0.00, 20), (1.00, 50), (2.00, 75), (4.00, 100)]

# 20-trading-day cross-spread widening thresholds (bp) and tightening thresholds (bp).
CROSS_WIDEN_BP = {"IG": 15.0, "BBB": 20.0, "BAA10Y": 25.0}
CROSS_TIGHTEN_BP = {"IG": -15.0, "BBB": -20.0, "BAA10Y": -25.0}

# RPONTSYD proxy: announced small-value tests are 0.001B per operation. FRED shows only a
# daily total, so a daily total <= this many billions (a handful of $1M tests) is treated as
# test-only and excluded. Any residual amount below 1B scores 0 on the anchor anyway.
REPO_TEST_MAX_B = 0.005

# Fed business-day holidays used only for the calendar-turn window and staleness counts.
HOLIDAYS = {
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-05-26", "2025-06-19", "2025-07-04",
    "2025-09-01", "2025-10-13", "2025-11-11", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-10-12", "2026-11-11", "2026-11-26", "2026-12-25",
    "2027-01-01",
}
# Session calendars for counting 5/20/63-session changes and "completed closes".
# ICE prints OAS on bond-market holidays and some month-end weekends (carried values); those
# are not trading sessions. ICE OAS series count SIFMA-recommended full-close days as
# non-sessions; VIXCLS counts NYSE/Cboe sessions (Columbus Day and Veterans Day are Cboe
# trading days, so a VIX close on those dates is a genuine completed close).
# Coverage starts 2023-10-30 (first ICE observation in the FRED 3-year window). Uncertain
# dates are deliberately omitted rather than guessed: 2023-11-10 (Veterans Day observed;
# ICE and BAA10Y both printed) and Good Friday 2026-04-03 (ICE did not print, so omission is
# harmless). Extend both sets each year from the SIFMA and NYSE published schedules.
CALENDAR_COVERAGE_START = "2023-10-30"
SIFMA_HOLIDAYS = {
    "2023-11-23", "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19",
    "2024-07-04", "2024-09-02", "2024-10-14", "2024-11-11", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
    "2025-07-04", "2025-09-01", "2025-10-13", "2025-11-11", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-10-12", "2026-11-11", "2026-11-26", "2026-12-25",
    "2027-01-01",
}
NYSE_HOLIDAYS = {
    "2023-11-23", "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19",
    "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01",
}
ICE_IDS = ["BAMLH0A0HYM2", "BAMLC0A0CM", "BAMLC0A4CBBB"]
SESSION_CALENDAR = {sid: SIFMA_HOLIDAYS for sid in ICE_IDS}
SESSION_CALENDAR["VIXCLS"] = NYSE_HOLIDAYS
ICE_CONTEXT_YEARS = 3  # history_windows.credit_spreads "3-year context"
# Major tax dates (corporate estimated taxes; April individual). Day and next business day.
TAX_DATES_MD = {"04-15", "06-15", "09-15", "12-15"}

# Policy facts used only by --self-test for the 2026-09-17 validation (agent-of-record packet).
VALIDATION_FACTS = {
    "operating_period": "2026-09-15 through 2026-10-14",
    "reserve_management_purchases_planned_B": 0.0,
    "reinvestment_purchases_planned_B": 15.6,
    "active_qt": False,
    "schedule_confirmed_primary": True,
    "schedule_status": "as recorded in the agent-of-record 2026-09-17 packet",
    "fomc_2026_10_28": {"confirmed": True},
    "sources": [],
}
# Last known schedule, carried forward (stale) when --facts is not supplied.
LAST_KNOWN_FACTS = {
    "operating_period": "2026-09-15 through 2026-10-14 (STALE, carried forward)",
    "reserve_management_purchases_planned_B": 0.0,
    "reinvestment_purchases_planned_B": 15.6,
    "active_qt": False,
    "schedule_confirmed_primary": False,
    "schedule_status": "last known schedule carried forward; current period unconfirmed",
    "fomc_2026_10_28": {"confirmed": False},
    "sources": [],
}

VALIDATION = {
    "asof": "2026-09-17", "credit_asof": "2026-09-16",
    "score": 10, "CreditStress": 7.4, "VolatilityStress": 6.8,
    "LiquidityFundingStress": 17.8, "confidence": 93,
    "HY": 2.70, "IG": 0.78, "BBB": 0.96, "BAA10Y": 1.43, "VIX": 15.44,
    "WRESBAL_T": 3.013794, "WRESBAL_4w_B": 78.507, "WTREGEN_T": 0.877028,
    "WTREGEN_4w_B": -76.584, "WALCL_T": 6.746548, "RRP_B": 0.276,
    "SOFR": 3.85, "IORB": 3.90, "SOFR_IORB_bp": -5.0, "PRIMARY_B": 6.879,
    "SWAPS_B": 0.094, "NFCI": -0.560, "STLFSI4": -0.8477, "RMP_B": 0.0,
}

DEFAULT_CACHE = os.environ.get("LIQUIDITY_CREDIT_CACHE", "")
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
CA_BUNDLE = "/root/.ccr/ca-bundle.crt"


# --------------------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------------------
def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def interp(x: float, anchors: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation; clamp below first / above last anchor."""
    if x <= anchors[0][0]:
        return float(anchors[0][1])
    if x >= anchors[-1][0]:
        return float(anchors[-1][1])
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            return y0 + (x - x0) * (y1 - y0) / (x1 - x0)
    raise AssertionError("unreachable")


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def bp(x: float) -> float:
    """Percentage points -> basis points, de-noised to 1e-6 bp."""
    return round(x * 100.0, 6)


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def is_business_day(day: dt.date) -> bool:
    return day.weekday() < 5 and day.isoformat() not in HOLIDAYS


def next_business_day(day: dt.date) -> dt.date:
    day += dt.timedelta(days=1)
    while not is_business_day(day):
        day += dt.timedelta(days=1)
    return day


def business_days_between(a: str, b: str) -> int:
    """Business days in (a, b]."""
    da, db = d(a), d(b)
    n, cur = 0, da
    while cur < db:
        cur += dt.timedelta(days=1)
        if is_business_day(cur):
            n += 1
    return n


def is_session(day: dt.date, holidays: set[str]) -> bool:
    return day.weekday() < 5 and day.isoformat() not in holidays


def sessions_only(series: list[tuple[str, float]], holidays: set[str]
                  ) -> list[tuple[str, float]]:
    """Drop prints on non-sessions (weekends, holidays) so lags count trading sessions."""
    return [p for p in series if is_session(d(p[0]), holidays)]


def years_back(day: dt.date, n: int) -> dt.date:
    try:
        return day.replace(year=day.year - n)
    except ValueError:  # Feb 29 -> Feb 28
        return day.replace(year=day.year - n, day=28)


def first_session_on_or_after(day: dt.date, holidays: set[str]) -> dt.date:
    while not is_session(day, holidays):
        day += dt.timedelta(days=1)
    return day


def ice_context_check(series_by_id: dict, cutoff: str) -> dict:
    """supervisor_integration: deduct 2 only when ICE history truncation prevents the
    desired (3-year, per history_windows.credit_spreads) context ending at `cutoff`."""
    start = years_back(d(cutoff), ICE_CONTEXT_YEARS)
    first = first_session_on_or_after(start, SIFMA_HOLIDAYS)
    starts = {sid: s[0][0] for sid, s in series_by_id.items() if s}
    short = {sid: st for sid, st in starts.items() if st > first.isoformat()}
    return {"window_start": start.isoformat(), "first_session": first.isoformat(),
            "history_start": starts, "truncated": short, "deduct": bool(short)}


def ice_convention_check(series_by_id: dict) -> dict:
    """Agent-of-record convention: deduct 2 whenever FRED ICE OAS history is the truncated
    ~3-year window, i.e. it starts after ICE_FULL_HISTORY_BEFORE (one deduction in total)."""
    starts = {sid: s[0][0] for sid, s in series_by_id.items() if s}
    short = {sid: st for sid, st in starts.items() if st > ICE_FULL_HISTORY_BEFORE}
    return {"history_start": starts, "truncated": short, "deduct": bool(short)}


def transition_update_number(asof: str, updates: list[str]) -> int:
    """1-based position of this run among updates under the formalized implementation
    (dates after FORMALIZATION_DATE). A run whose as-of is not listed counts as the next one."""
    listed = sorted(u for u in set(updates) if FORMALIZATION_DATE < u)
    if asof in listed:
        return listed.index(asof) + 1
    return sum(u < asof for u in listed) + 1


def month_bd_edges(year: int, month: int) -> tuple[dt.date, dt.date, dt.date, dt.date]:
    """(first, second, penultimate, last) Fed business days of a month."""
    first = dt.date(year, month, 1)
    while not is_business_day(first):
        first += dt.timedelta(days=1)
    second = next_business_day(first)
    nm = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nm - dt.timedelta(days=1)
    while not is_business_day(last):
        last -= dt.timedelta(days=1)
    penult = last - dt.timedelta(days=1)
    while not is_business_day(penult):
        penult -= dt.timedelta(days=1)
    return first, second, penult, last


def next_month_end_turn(report_date: str) -> tuple[str, str]:
    """Remaining part of the next (or current) month-end turn window: penultimate/last business
    days of a month through the first two business days of the next month."""
    rd = d(report_date)
    y, m = rd.year, rd.month
    _, second_cur, _, _ = month_bd_edges(y, m)
    if rd <= second_cur:  # still inside the turn that started last month
        py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
        penult = month_bd_edges(py, pm)[2]
        end = second_cur
    else:
        penult = month_bd_edges(y, m)[2]
        ny, nmo = (y + 1, 1) if m == 12 else (y, m + 1)
        end = month_bd_edges(ny, nmo)[1]
    start = max(penult, rd)
    return start.isoformat(), end.isoformat()


def calendar_window_days(day: dt.date) -> bool:
    """Last two or first two business days of a month/quarter, or a major tax date (+1 bd)."""
    # first two business days of the month
    first = dt.date(day.year, day.month, 1)
    while not is_business_day(first):
        first += dt.timedelta(days=1)
    second = next_business_day(first)
    # last two business days of the month
    nm = dt.date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    last = nm - dt.timedelta(days=1)
    while not is_business_day(last):
        last -= dt.timedelta(days=1)
    penult = last - dt.timedelta(days=1)
    while not is_business_day(penult):
        penult -= dt.timedelta(days=1)
    if day in (first, second, last, penult):
        return True
    for md in TAX_DATES_MD:
        t = d(f"{day.year}-{md}")
        while not is_business_day(t):
            t += dt.timedelta(days=1)
        if day in (t, next_business_day(t)):
            return True
    return False


# --------------------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------------------
def fetch(dir_: str) -> None:
    os.makedirs(dir_, exist_ok=True)
    ctx = ssl.create_default_context()
    for sid in SERIES:
        for attempt in range(3):
            try:
                try:
                    raw = urllib.request.urlopen(FRED_URL.format(sid), timeout=90,
                                                 context=ctx).read()
                except ssl.SSLError:
                    ctx = ssl.create_default_context(cafile=CA_BUNDLE)
                    raw = urllib.request.urlopen(FRED_URL.format(sid), timeout=90,
                                                 context=ctx).read()
                with open(os.path.join(dir_, sid + ".csv"), "wb") as fh:
                    fh.write(raw)
                print(f"fetched {sid}: {len(raw)} bytes")
                break
            except Exception as exc:  # network retry
                print(f"retry {sid} ({attempt + 1}/3): {exc}", file=sys.stderr)
                time.sleep(3)
        else:
            raise SystemExit(f"failed to fetch {sid}")
    with open(os.path.join(dir_, "fetch_log.json"), "w") as fh:
        json.dump({"retrieved_utc": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                   "series": SERIES, "url": FRED_URL}, fh, indent=1)


def load_series(cache: str, sid: str) -> list[tuple[str, float]]:
    path = os.path.join(cache, sid + ".csv")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="") as fh:
        rdr = csv.reader(fh)
        next(rdr, None)
        for row in rdr:
            if len(row) < 2:
                continue
            v = row[1].strip()
            if v in ("", "."):
                continue
            out.append((row[0].strip(), float(v)))
    out.sort()
    return out


def upto(series: list[tuple[str, float]], cutoff: str) -> list[tuple[str, float]]:
    return [p for p in series if p[0] <= cutoff]


def lagged(series, n):
    """Observation n observations before the latest (own-series trading days)."""
    return series[-1 - n] if len(series) > n else None


def weekly_upto(series, report_date: str, lag_days: int):
    """Weekly observations whose publication (date + lag_days) is on/before report_date."""
    rd = d(report_date)
    return [p for p in series if d(p[0]) + dt.timedelta(days=lag_days) <= rd]


# --------------------------------------------------------------------------------------
# Overlays
# --------------------------------------------------------------------------------------
def hy_trend_overlay(change_bp: float) -> int:
    if change_bp <= -50:
        return -8
    if change_bp <= -25:
        return -5
    if change_bp < 25:
        return 0
    if change_bp < 50:
        return 5
    if change_bp < 100:
        return 10
    return 15


def vix_5d_overlay(pct: float) -> int:
    if pct <= -20:
        return -10
    if pct <= -5:
        return -5
    if pct < 5:
        return 0
    if pct < 20:
        return 5
    if pct < 40:
        return 10
    return 15


def vix_persistence_overlay(last5: list[float]) -> int:
    """Single highest applicable persistence overlay."""
    if sum(v > 25 for v in last5) >= 3:
        return 10
    if sum(v > 20 for v in last5) >= 3:
        return 5
    if len(last5) == 5 and all(v < 15 for v in last5):
        return -3
    return 0


def policy_score(rmp_b: float | None, wresbal_t: float, active_qt: bool) -> float:
    if active_qt:
        return 90.0
    if rmp_b is None:
        rmp_b = 0.0
    if rmp_b >= 40:
        return 0.0
    if rmp_b >= 10:
        return 15.0
    if rmp_b > 0:
        return 25.0
    return 30.0 if wresbal_t >= 3.0 else 60.0


# --------------------------------------------------------------------------------------
# Core computation
# --------------------------------------------------------------------------------------
def compute(cache: str, facts: dict, asof: str | None = None, report_date: str | None = None,
            credit_asof: str | None = None, prior_score: int | None = None,
            prior_asof: str | None = None, formalized_updates: list[str] | None = None
            ) -> dict:
    raw = {sid: load_series(cache, sid) for sid in SERIES}
    # Session-filtered views: ICE OAS on the SIFMA bond-market calendar, VIXCLS on NYSE/Cboe.
    # Lags (5/20/63 sessions) and "completed closes" are counted on these views only.
    ser = {sid: (sessions_only(raw[sid], SESSION_CALENDAR[sid]) if sid in SESSION_CALENDAR
                 else raw[sid]) for sid in SERIES}
    live = asof is None
    if report_date is None:
        report_date = dt.date.today().isoformat() if live else asof
    r: dict = {"report_date": report_date, "live": live, "notes": [], "missing_core": [],
               "credit_asof_override": credit_asof}
    r["vix_kept_bond_holidays"] = [
        p[0] for p in upto(raw["VIXCLS"], report_date)[-90:]
        if is_session(d(p[0]), NYSE_HOLIDAYS) and not is_session(d(p[0]), SIFMA_HOLIDAYS)]
    r["dropped_non_session_prints"] = {
        sid: [p[0] for p in upto(raw[sid], report_date)[-90:]
              if not is_session(d(p[0]), SESSION_CALENDAR[sid])]
        for sid in SESSION_CALENDAR}

    # ---- market as-of: latest close common to the daily credit + VIX series ----
    mkt_ids = ["BAMLH0A0HYM2", "BAMLC0A0CM", "BAMLC0A4CBBB", "BAA10Y", "VIXCLS"]
    if asof is None:
        lasts = {s: upto(ser[s], report_date)[-1][0] for s in mkt_ids if upto(ser[s], report_date)}
        # common close = latest session present in every available one of HY, IG, BBB and VIX
        # (BAA10Y may lag; it keeps its own date). A missing series must not block the as-of.
        sets = [set(p[0] for p in upto(ser[s], report_date))
                for s in ["BAMLH0A0HYM2", "BAMLC0A0CM", "BAMLC0A4CBBB", "VIXCLS"]
                if upto(ser[s], report_date)]
        common = set.intersection(*sets) if sets else set()
        if common:
            asof = max(common)
        elif lasts:
            asof = max(lasts.values())
        else:
            raise SystemExit("no credit or VIX observations on/before the report date")
        r["latest_by_series"] = lasts
    r["asof"] = asof
    c_cut = credit_asof or asof
    r["credit_cutoff"] = c_cut

    # ---- credit ----
    credit = {}
    for k, sid in CREDIT_IDS.items():
        s = upto(ser[sid], c_cut)
        if not s:
            credit[k] = None
            continue
        latest = s[-1]
        # Aged against the report date, not against the as-of found in the data.
        stale_bd = business_days_between(latest[0], report_date)
        if stale_bd > CREDIT_MAX_STALE_BD:
            credit[k] = None
            r.setdefault("stale_dropped", {})[sid] = (latest[0], stale_bd)
            continue
        p5, p20, p63 = lagged(s, 5), lagged(s, 20), lagged(s, 63)
        own = upto(raw[sid], c_cut)  # unfiltered own-print count, disclosure only
        own20 = lagged(own, 20)
        win21 = s[-21:]
        low20 = min(v for _, v in win21)
        credit[k] = {
            "id": sid, "date": latest[0], "value": latest[1], "stale_bd": stale_bd,
            "d5_bp": bp(latest[1] - p5[1]) if p5 else None, "d5_from": p5[0] if p5 else None,
            "d20_bp": bp(latest[1] - p20[1]) if p20 else None,
            "d20_from": p20[0] if p20 else None, "d20_from_value": p20[1] if p20 else None,
            "d63_bp": bp(latest[1] - p63[1]) if p63 else None,
            "low20": low20, "above_low20_bp": bp(latest[1] - low20),
            "score": interp(latest[1], CREDIT_ANCHORS[k]),
            "history_start": ser[sid][0][0],
            "d20_own_print_bp": bp(latest[1] - own20[1]) if own20 else None,
            "d20_own_print_from": own20[0] if own20 else None,
        }
    avail = {k: v for k, v in credit.items() if v is not None}
    for k in CREDIT_IDS:
        if credit[k] is None:
            r["missing_core"].append(CREDIT_IDS[k])
    wsum = sum(CREDIT_WEIGHTS[k] for k in avail)
    credit_level = sum(CREDIT_WEIGHTS[k] * avail[k]["score"] for k in avail) / wsum if wsum else 0.0
    if 0 < len(avail) < 4:
        r["notes"].append("credit weights renormalized over " + ",".join(avail)
                          + " (missing/stale > 5 business days: "
                          + ",".join(CREDIT_IDS[k] for k in CREDIT_IDS if k not in avail) + ")")
    hy = credit.get("HY")
    hy_overlay = hy_trend_overlay(hy["d20_bp"]) if hy and hy["d20_bp"] is not None else 0
    confirming = {k: credit[k] for k in ("IG", "BBB", "BAA10Y")
                  if credit.get(k) and credit[k]["d20_bp"] is not None}
    cross = 0
    if len(confirming) >= 2:
        widen = sum(confirming[k]["d20_bp"] >= CROSS_WIDEN_BP[k] for k in confirming)
        if widen >= 2:
            cross = 5
        elif len(confirming) == 3 and all(confirming[k]["d20_bp"] <= CROSS_TIGHTEN_BP[k]
                                          for k in confirming):
            cross = -3
    credit_stress = clamp(credit_level + hy_overlay + cross) if avail else None
    r["credit"] = credit
    r["credit_level_weighted"] = credit_level
    r["hy_trend_overlay"] = hy_overlay
    r["cross_spread_overlay"] = cross

    # max HY 20d change over the last 21 sessions (context only, not scored)
    hys = upto(ser["BAMLH0A0HYM2"], c_cut) if hy else []
    peak = None
    for i in range(max(0, len(hys) - 21), len(hys)):
        if i - 20 >= 0:
            ch = bp(hys[i][1] - hys[i - 20][1])
            if peak is None or ch > peak[1]:
                peak = (hys[i][0], ch)
    r["hy_peak_20d_change_recent"] = peak

    # ---- VIX (completed NYSE/Cboe closes only) ----
    vs = upto(ser["VIXCLS"], asof)
    v_stale = business_days_between(vs[-1][0], report_date) if vs else None
    if vs and v_stale is not None and v_stale > VIX_MAX_STALE_BD:
        r.setdefault("stale_dropped", {})["VIXCLS"] = (vs[-1][0], v_stale)
    if len(vs) >= 6 and v_stale is not None and v_stale <= VIX_MAX_STALE_BD:
        v_latest = vs[-1]
        v5 = lagged(vs, 5)
        v_pct = (v_latest[1] / v5[1] - 1.0) * 100.0
        last5 = [v for _, v in vs[-5:]]
        vix_level = interp(v_latest[1], A_VIX)
        vix_o5 = vix_5d_overlay(v_pct)
        vix_pers = vix_persistence_overlay(last5)
        vol_uncapped = clamp(vix_level + vix_o5 + vix_pers)
        r["vix"] = {"date": v_latest[0], "value": v_latest[1], "d5_from": v5[0],
                    "d5_from_value": v5[1], "d5_pct": v_pct, "last5": vs[-5:],
                    "n_above20": sum(v > 20 for v in last5),
                    "n_above25": sum(v > 25 for v in last5),
                    "level_score": vix_level, "o5": vix_o5, "persistence": vix_pers,
                    "stale_bd": v_stale}
    else:
        vol_uncapped = None
        r["vix"] = None
        r["missing_core"].append("VIXCLS")

    # ---- funding market ----
    f_cut = report_date
    sofr = upto(raw["SOFR"], f_cut)
    iorb = dict(upto(raw["IORB"], f_cut))
    pairs = [(dd, v, iorb[dd]) for dd, v in sofr if dd in iorb]
    win = pairs[-5:]
    spreads = [(dd, bp(s_ - i_)) for dd, s_, i_ in win]
    pos = [max(0.0, x) for _, x in spreads]
    s_med, s_max = statistics.median(pos), max(pos)
    sofr_score_raw = (0.60 * interp(s_med, A_SOFR_IORB_BP)
                      + 0.40 * interp(s_max, A_SOFR_IORB_BP))
    latest_pair = win[-1]
    repo_all = upto(raw["RPONTSYD"], f_cut)
    rwin = repo_all[-5:]
    rclean = [(dd, 0.0 if v <= REPO_TEST_MAX_B else v) for dd, v in rwin]
    rvals = [v for _, v in rclean]
    r_max, r_med = max(rvals), statistics.median(rvals)
    repo_score_raw = 0.60 * interp(r_max, A_REPO_B) + 0.40 * interp(r_med, A_REPO_B)

    # calendar adjustment
    cal_days = {dd for dd, _ in spreads + rclean if calendar_window_days(d(dd))}
    pressure = [dd for dd, x in spreads if x > 0] + [dd for dd, v in rclean if v >= 1.0]
    cal_touch = bool(cal_days)
    discount, cal_reason = False, ""
    if not pressure:
        cal_reason = "no SOFR/repo pressure in window; nothing to discount"
    elif any(dd not in cal_days for dd in pressure):
        off = sorted(dd for dd in pressure if dd not in cal_days)
        cal_reason = "pressure on non-calendar days " + ",".join(off) + "; not isolated"
    else:
        last_p = max(pressure)
        nxt = next_business_day(d(last_p)).isoformat()
        nxt_repo = dict(repo_all).get(nxt)
        hy20 = hy["d20_bp"] if hy else None
        if s_max > 10:
            cal_reason = "SOFR-IORB > 10 bp"
        elif nxt_repo is None:
            cal_reason = f"next-business-day repo ({nxt}) not yet published"
        elif nxt_repo > 5:
            cal_reason = f"repo did not normalize to <=5B on {nxt}"
        elif hy20 is not None and hy20 > 25:
            cal_reason = "HY widened > 25 bp"
        else:
            discount, cal_reason = True, f"isolated calendar spike {last_p}, normalized {nxt}"
    mult = 0.5 if discount else 1.0
    sofr_score = sofr_score_raw * mult
    repo_score = repo_score_raw * mult
    funding = FUNDING_WEIGHTS["sofr"] * sofr_score + FUNDING_WEIGHTS["repo"] * repo_score
    r["funding"] = {
        "sofr_date": latest_pair[0], "sofr": latest_pair[1], "iorb": latest_pair[2],
        "spread_bp": spreads[-1][1], "window": spreads, "median_pos_bp": s_med,
        "max_pos_bp": s_max, "sofr_score": sofr_score, "sofr_score_raw": sofr_score_raw,
        "repo_date": rclean[-1][0], "repo_latest_raw_B": rwin[-1][1],
        "repo_latest_B": rclean[-1][1], "repo_window": rclean, "repo_window_raw": rwin,
        "repo_max_B": r_max, "repo_median_B": r_med, "repo_score": repo_score,
        "repo_score_raw": repo_score_raw, "calendar_window_days": sorted(cal_days),
        "calendar_touch": cal_touch, "calendar_discount": discount,
        "calendar_reason": cal_reason, "score": funding,
    }

    # ---- reserves / balance sheet (H.4.1 Wednesday, published Thursday) ----
    def weekly(sid, lag=1):
        s = weekly_upto(raw[sid], report_date, lag)
        if not s:
            return None
        out = {"date": s[-1][0], "value_m": s[-1][1]}
        for n, key in ((1, "d1w"), (4, "d4w"), (13, "d13w")):
            p = lagged(s, n)
            out[key + "_B"] = (s[-1][1] - p[1]) / 1000.0 if p else None
            out[key + "_from"] = p[0] if p else None
        out["series"] = s
        return out

    wres, tga, walcl = weekly("WRESBAL"), weekly("WTREGEN"), weekly("WALCL")
    pc, sw = weekly("WLCFLPCL"), weekly("SWPT")
    wres_t = wres["value_m"] / 1e6
    abs_score = interp(wres_t, A_WRESBAL_T)
    chg_score = interp(wres["d4w_B"], A_WRESBAL_4W_B)
    rrp_s = upto(raw["RRPONTSYD"], f_cut)
    rrp = rrp_s[-1]
    rrp_score = interp(rrp[1], A_ONRRP_B)
    rmp = facts.get("reserve_management_purchases_planned_B")
    pol = policy_score(rmp, wres_t, bool(facts.get("active_qt")))
    reserve = (RESERVE_WEIGHTS["abs"] * abs_score + RESERVE_WEIGHTS["chg4w"] * chg_score
               + RESERVE_WEIGHTS["onrrp"] * rrp_score + RESERVE_WEIGHTS["policy"] * pol)
    if wres["d4w_B"] < 0 and tga["d4w_B"] > 0:
        tga_frac = min(1.0, tga["d4w_B"] / abs(wres["d4w_B"]))
    else:
        tga_frac = None
    r["reserves"] = {"wresbal": {k: v for k, v in wres.items() if k != "series"},
                     "tga": {k: v for k, v in tga.items() if k != "series"},
                     "walcl": {k: v for k, v in walcl.items() if k != "series"},
                     "wresbal_T": wres_t, "abs_score": abs_score, "chg4w_score": chg_score,
                     "rrp_date": rrp[0], "rrp_B": rrp[1], "rrp_score": rrp_score,
                     "policy_score": pol, "rmp_B": rmp, "score": reserve,
                     "tga_explained_fraction": tga_frac}

    # ---- facilities ----
    pc_b, sw_b = pc["value_m"] / 1000.0, sw["value_m"] / 1000.0
    pc_prev = lagged(pc["series"], 1)
    pc_1w_pct = (pc["value_m"] / pc_prev[1] - 1) * 100 if pc_prev and pc_prev[1] else None
    pc_prev2 = lagged(pc["series"], 2)
    pc_1w_b = (pc["value_m"] - pc_prev[1]) / 1000.0 if pc_prev else None
    pc_prior_1w_b = (pc_prev[1] - pc_prev2[1]) / 1000.0 if pc_prev and pc_prev2 else None
    sw_prev = lagged(sw["series"], 1)
    sw_1w_pct = (sw["value_m"] / sw_prev[1] - 1) * 100 if sw_prev and sw_prev[1] else None
    sw_1w_b = (sw["value_m"] - sw_prev[1]) / 1000.0 if sw_prev else None
    # change_my_mind trigger: "Primary credit > 25B and accelerating, or central-bank swaps
    # > 5B and rising". Accelerating = a 1w increase larger than the prior week's increase;
    # rising = a positive 1w change.
    pc_accel_trig = (pc_1w_b is not None and pc_prior_1w_b is not None and pc_1w_b > 0
                     and pc_1w_b > pc_prior_1w_b)
    fac_trigger = ((pc_b > 25 and pc_accel_trig)
                   or (sw_b > 5 and sw_1w_b is not None and sw_1w_b > 0))
    pc_score, sw_score = interp(pc_b, A_PRIMARY_B), interp(sw_b, A_SWAPS_B)
    facility = clamp(FACILITY_WEIGHTS["primary"] * pc_score + FACILITY_WEIGHTS["swaps"] * sw_score)
    r["facilities"] = {"date": pc["date"], "primary_B": pc_b, "primary_1w_pct": pc_1w_pct,
                       "primary_4w_max_B": max(v for _, v in pc["series"][-4:]) / 1000.0,
                       "swaps_date": sw["date"], "swaps_B": sw_b, "swaps_1w_pct": sw_1w_pct,
                       "primary_1w_B": pc_1w_b, "primary_prior_1w_B": pc_prior_1w_b,
                       "primary_accelerating": pc_accel_trig, "swaps_1w_B": sw_1w_b,
                       "trigger_met": bool(fac_trigger),
                       "primary_score": pc_score, "swaps_score": sw_score, "score": facility}

    # ---- broad stress (NFCI published Wed after Friday week-end; STLFSI4 Thursday) ----
    nf = weekly_upto(raw["NFCI"], report_date, 5)
    st = weekly_upto(raw["STLFSI4"], report_date, 6)
    nf_score, st_score = interp(nf[-1][1], A_NFCI), interp(st[-1][1], A_STLFSI4)
    broad = BROAD_WEIGHTS["nfci"] * nf_score + BROAD_WEIGHTS["stlfsi4"] * st_score
    r["broad"] = {"nfci_date": nf[-1][0], "nfci": nf[-1][1], "nfci_4w": nf[-1][1] - nf[-5][1],
                  "stlfsi4_date": st[-1][0], "stlfsi4": st[-1][1],
                  "stlfsi4_4w": st[-1][1] - st[-5][1],
                  "nfci_score": nf_score, "stlfsi4_score": st_score, "score": broad}

    # ---- flags ----
    noncal = lambda dd: dd not in cal_days  # noqa: E731
    sofr_gt10 = [dd for dd, x in spreads if x > 10 and (noncal(dd) or not discount)]
    repo_gt10 = [dd for dd, v in rclean if v > 10]
    repo_gt25_noncal = [dd for dd, v in rclean if v > 25 and noncal(dd)]
    pc_accel = pc_b > 25 and pc_1w_pct is not None and pc_1w_pct >= 50
    funding_conf = len(sofr_gt10) >= 3 or len(repo_gt10) >= 3 or bool(repo_gt25_noncal)
    scar_funding = funding_conf or pc_accel
    scar_qty = wres_t < 2.8 or wres["d4w_B"] < -200
    tga_s, wres_s = tga["series"], wres["series"]
    tga_1w = [(tga_s[-i][1] - tga_s[-i - 1][1]) for i in (1, 2)]
    res_1w = [(wres_s[-i][1] - wres_s[-i - 1][1]) for i in (1, 2)]
    funding_watch = spreads[-1][1] > 5 or rclean[-1][1] > 10
    persist_branch = all(x <= 0 for x in tga_1w) and all(x < 0 for x in res_1w) and funding_watch
    attrib_branch = tga_frac is not None and tga_frac < 0.60
    if wres["d4w_B"] < 0 and tga_frac is None:
        attrib_branch = True  # reserves fell while TGA did not rise: TGA explains 0%
    scar_persist = attrib_branch or persist_branch
    genuine = scar_qty and scar_funding and scar_persist
    overlay = GENUINE_SCARCITY_POINTS if genuine else 0.0

    hyv = hy["value"] if hy else float("nan")
    hy20 = hy["d20_bp"] if hy else None
    conf_lvl = ((credit.get("IG") and credit["IG"]["value"] >= 1.25)
                or (credit.get("BBB") and credit["BBB"]["value"] >= 1.50)
                or (credit.get("BAA10Y") and credit["BAA10Y"]["value"] >= 2.25))
    primary_rule = bool(hy and hyv >= 4.0 and hy20 is not None and hy20 > 0 and conf_lvl)
    n_conf_widen = sum(confirming[k]["d20_bp"] >= CROSS_WIDEN_BP[k] for k in confirming)
    shock_rule = bool(hy and hy["above_low20_bp"] >= 75 and n_conf_widen >= 2)
    credit_conf = primary_rule or shock_rule
    credit_watch = bool(hy and (hyv >= 3.5 or (hy20 is not None and hy20 >= 50))) or bool(
        (credit.get("IG") and credit["IG"]["value"] >= 1.2)
        or (credit.get("BBB") and credit["BBB"]["value"] >= 1.5)
        or (credit.get("BAA10Y") and credit["BAA10Y"]["value"] >= 2.25))
    severe_credit = bool(hy and (hyv >= 6.0 or (hy20 is not None and hy20 >= 200 and (
        (credit.get("IG") and (credit["IG"]["d20_bp"] or 0) >= 15)
        or (credit.get("BBB") and (credit["BBB"]["d20_bp"] or 0) >= 20)))))
    temp_tga = (wres["d4w_B"] < 0 and tga["d4w_B"] > 0 and tga_frac is not None
                and tga_frac >= 0.60 and s_med <= 5 and r_max <= 10 and pc_b < 25
                and not credit_conf)
    reserve_watch = wres_t < 3.0 or wres["d4w_B"] < -100
    vix_watch = bool(r["vix"] and r["vix"]["n_above20"] >= 3)
    r["flags"] = {
        "credit_watch": credit_watch, "credit_confirmation": credit_conf,
        "credit_confirmation_primary_rule": primary_rule,
        "credit_confirmation_shock_rule": shock_rule, "severe_credit_stress": severe_credit,
        "vix_watch": vix_watch, "funding_watch": funding_watch,
        "funding_confirmation": funding_conf, "reserve_watch": reserve_watch,
        "temporary_tga": bool(temp_tga), "genuine_scarcity": genuine,
        "scarcity_quantity_test": scar_qty, "scarcity_funding_test": scar_funding,
        "scarcity_persistence_test": scar_persist, "calendar_discount": discount,
        "calendar_touch": cal_touch, "primary_credit_accelerating": pc_accel,
        "primary_credit_1w_jump_ge50pct": pc_1w_pct is not None and pc_1w_pct >= 50,
        "sofr_gt10_days": sofr_gt10, "repo_gt10_days": repo_gt10,
    }

    # ---- liquidity/funding stress, volatility cap, total ----
    lfs = clamp(LFS_WEIGHTS["reserve"] * reserve + LFS_WEIGHTS["funding"] * funding
                + LFS_WEIGHTS["facility"] * facility + LFS_WEIGHTS["broad"] * broad + overlay)
    capped = (credit_stress is None or credit_stress < 30) and funding < 30
    if vol_uncapped is None:
        vol = None
    else:
        vol = min(vol_uncapped, VOL_CAP) if capped else vol_uncapped
    # Top-level blend. The spec gives no rule for a missing top-level component; when
    # CreditStress or VolatilityStress cannot be formed (HY/IG/BBB/BAA10Y all missing, or VIX
    # missing/stale > 5 business days) the remaining top-level weights are renormalized, the
    # score is flagged PROVISIONAL (incomplete_row_rule: no fully confident official score)
    # and confidence takes the 8-point missing-core deduction.
    parts = {"credit": credit_stress, "volatility": vol, "liquidity": lfs}
    tw = sum(TOP_WEIGHTS[k] for k, x in parts.items() if x is not None)
    unrounded = clamp(sum(TOP_WEIGHTS[k] * x for k, x in parts.items() if x is not None) / tw)
    one_dec = sum(TOP_WEIGHTS[k] * round(x, 1) for k, x in parts.items() if x is not None) / tw
    score = round_half_up(unrounded)
    r["top_weight_renormalized"] = [k for k, x in parts.items() if x is None]
    # incomplete_row_rule: HY OAS or VIX missing, or stale by more than 3 business days vs the
    # report date -> no fully confident official score.
    hv_stale = {}
    if hy:
        hv_stale["BAMLH0A0HYM2"] = (hy["date"], hy["stale_bd"])
    if r["vix"]:
        hv_stale["VIXCLS"] = (r["vix"]["date"], r["vix"]["stale_bd"])
    hv_stale.update({k: v for k, v in r.get("stale_dropped", {}).items()
                     if k in ("BAMLH0A0HYM2", "VIXCLS")})
    r["hy_vix_stale"] = {k: v for k, v in hv_stale.items() if v[1] > HY_VIX_PROVISIONAL_BD}
    r["provisional"] = (bool(r["top_weight_renormalized"]) or bool(r["hy_vix_stale"]) or any(
        s in r["missing_core"] for s in ("BAMLH0A0HYM2", "VIXCLS")))
    r["components"] = {
        "CreditStress": credit_stress, "VolatilityStress": vol,
        "VolatilityStress_uncapped": vol_uncapped, "vol_cap_eligible": capped,
        "vol_cap_binding": bool(capped and vol_uncapped is not None and vol_uncapped > VOL_CAP),
        "ReserveBufferScore": reserve, "FundingMarketScore": funding,
        "FacilityStress": facility, "BroadStress": broad, "genuine_scarcity_overlay": overlay,
        "LiquidityFundingStress": lfs, "unrounded": unrounded,
        "unrounded_from_1dp_components": one_dec,
    }
    r["contrib"] = {k: TOP_WEIGHTS[k] * x / tw for k, x in parts.items() if x is not None}
    r["score"] = score
    r["regime"] = regime(score)
    r["prior_score"], r["prior_asof"] = prior_score, prior_asof
    r["prior_hy"] = None
    if prior_asof == VALIDATION["asof"]:
        r["prior_hy"] = VALIDATION["HY"]  # as published (2026-09-16 close)
    elif prior_asof:
        ph = upto(raw["BAMLH0A0HYM2"], prior_asof)
        r["prior_hy"] = ph[-1][1] if ph else None
    r["score_delta"] = score - prior_score if prior_score is not None else None

    # ---- confidence ----
    ded = []
    # "5 for any core daily series more than 2 business days stale": one deduction, not per series.
    stale_list = []
    for sid in CORE_DAILY:
        if sid in r["missing_core"]:
            continue  # charged as missing (8) below, not also as stale
        cut = c_cut if sid in CREDIT_IDS.values() else (asof if sid == "VIXCLS" else f_cut)
        s = upto(ser[sid], cut)
        if not s:
            continue
        stale = business_days_between(s[-1][0], report_date)
        if stale > CORE_DAILY_STALE_BD:
            stale_list.append(f"{sid} {s[-1][0]} {stale}bd")
    r["core_daily_stale"] = stale_list
    if stale_list:
        ded.append((5, f"core daily series > {CORE_DAILY_STALE_BD} business days stale vs report "
                       f"date {report_date}: " + ", ".join(stale_list)))
    for wk in (wres, tga):
        if (d(report_date) - d(wk["date"])).days > 8:
            ded.append((5, "H.4.1 older than 8 calendar days"))
            break
    # "8 for each missing core series": absent from cache, or (credit, VIX) missing/stale > 5bd.
    missing = [sid for sid in CORE_SERIES if not raw[sid]]
    missing += [sid for sid in r["missing_core"] if sid not in missing]
    r["missing_core"] = missing
    for sid in missing:
        ded.append((8, f"missing core series {sid}" + (
            "" if not raw[sid] else " (stale > 5 business days; not scored)")))
    for sid in AUX_SERIES:
        if not raw[sid]:
            ded.append((3, f"missing auxiliary series {sid}"))
    ded.append((5, "RPONTSYD proxy substitutes for direct NY Fed standing-repo results"))
    if not facts.get("schedule_confirmed_primary") or not facts.get("fomc_2026_10_28", {}).get(
            "confirmed", True):
        ded.append((5, "current purchase schedule / implementation note unclear "
                       "(secondary-sourced or unconfirmed)"))
    fam = [credit_conf, funding_conf, scar_qty]
    if sum(fam) == 1:
        ded.append((5, "material conflict among credit, funding and reserve signals"))
    # ICE truncation: agent-of-record convention (history starts after 2000 -> -2, once).
    # The literal reading (deduct only when truncation prevents the 3-year context ending at
    # the credit cutoff) is computed for disclosure only; it is not applied.
    ice_conv = ice_convention_check({sid: raw[sid] for sid in ICE_IDS})
    ice_lit = ice_context_check({sid: ser[sid] for sid in ICE_IDS}, c_cut)
    r["ice_convention"], r["ice_context"] = ice_conv, ice_lit
    if ice_conv["deduct"]:
        ded.append((2, "ICE OAS history truncated on FRED ("
                       + ", ".join(f"{k} starts {v}" for k, v in ice_conv["truncated"].items())
                       + f", after {ICE_FULL_HISTORY_BEFORE}); agent-of-record convention"))
    base = 100 - sum(x for x, _ in ded)
    conf = int(clamp(base, 50, 95))
    lit_base = base + (2 if ice_conv["deduct"] else 0) - (2 if ice_lit["deduct"] else 0)
    r["confidence"] = conf
    r["confidence_literal_ice"] = int(clamp(lit_base, 50, 95))
    r["confidence_deductions"] = ded
    upd = FORMALIZED_UPDATE_DATES if formalized_updates is None else formalized_updates
    r["formalized_updates"] = sorted(set(upd))
    r["transition_update_n"] = transition_update_number(asof, upd)
    r["facts"] = facts
    return r


def regime(score: int) -> str:
    if score <= 14:
        return "very_easy"
    if score <= 34:
        return "easy_watchful"
    if score <= 54:
        return "neutral"
    if score <= 74:
        return "tightening_high_risk"
    return "acute_extreme"


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------
def f(x, nd=2, sign=False):
    if x is None:
        return "N/A"
    return (f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}")


def flagstr(b: bool) -> str:
    return "TRUE" if b else "FALSE"


def packet(r: dict) -> str:
    c, v, fu, rs, fa, br, fl = (r["credit"], r["vix"], r["funding"], r["reserves"],
                                r["facilities"], r["broad"], r["flags"])
    comp = r["components"]
    hy, ig, bbb, baa = c["HY"], c["IG"], c["BBB"], c["BAA10Y"]
    wr, tg, wa = rs["wresbal"], rs["tga"], rs["walcl"]
    facts = r["facts"]
    frac = rs["tga_explained_fraction"]

    def cm(k):
        x = c[k]
        if x is None:
            return "N/A (missing)"
        return (f"{x['value']:.2f}% ({x['date']} close; 5d {f(x['d5_bp'], 0, True)}bp, "
                f"20d {f(x['d20_bp'], 0, True)}bp vs {x['d20_from']})")

    sofr_line = (f"{f(fu['spread_bp'], 0, True)}bp ({fu['sofr_date']}; SOFR {fu['sofr']:.2f} / "
                 f"IORB {fu['iorb']:.2f}; 5d median-pos {f(fu['median_pos_bp'], 1)}bp, "
                 f"max-pos {f(fu['max_pos_bp'], 1)}bp)")
    asof_line = (f"{r['asof']} close (credit/VIX); BAA10Y {baa['date'] if baa else 'N/A'}; "
                 + (f"VIX {v['date']}; " if v and v["date"] != r["asof"] else
                    ("VIX N/A; " if not v else "")) +
                 f"SOFR/IORB {fu['sofr_date']}; ON RRP and repo proxy {rs['rrp_date']}; "
                 f"H.4.1 week ending {wr['date']}; NFCI week ending {br['nfci_date']}; "
                 f"STLFSI4 week ending {br['stlfsi4_date']}; purchase schedule "
                 f"{facts.get('operating_period', 'N/A')}; report date {r['report_date']}"
                 + ("; PROVISIONAL (" + ", ".join(f"{k} {v[1]}bd stale" for k, v in
                                                 sorted(r["hy_vix_stale"].items()))
                    + ")" if r["provisional"] and r["hy_vix_stale"] else
                    ("; PROVISIONAL" if r["provisional"] else "")))
    drivers = driver_lines(r)
    n_sofr10 = sum(x > 10 for _, x in fu["window"])
    n_repo10 = sum(x > 10 for _, x in fu["repo_window"])
    max_repo_noncal = max([x for dd, x in fu["repo_window"]
                           if dd not in fu["calendar_window_days"]] or [0.0])
    turn_start, turn_end = next_month_end_turn(r["report_date"])
    hy_low = (f"now {f(hy['above_low20_bp'], 0)}bp" if hy else "HY N/A")
    triggers = [
        f"Credit confirmation: HY >= 4.00% and rising over 20d with IG >= 1.25% / BBB >= 1.50% / "
        f"BAA10Y >= 2.25%, or HY >= 75bp above its 20d low ({hy_low}) "
        f"with two of IG +15 / BBB +20 / BAA10Y +25bp on 20d; HY >= 3.50% would set credit_watch",
        f"Funding: SOFR-IORB > 10bp on 3 of 5 days (now {n_sofr10} of {len(fu['window'])}; latest "
        f"{f(fu['spread_bp'], 0, True)}bp) or standing repo > 10B on 3 of 5 days (now {n_repo10} of "
        f"{len(fu['repo_window'])}) / > 25B on one non-calendar day (5d non-calendar max "
        f"{max_repo_noncal:.3f}B); check month-end turn {turn_start} to {turn_end} normalization",
        f"Reserves: WRESBAL < 2.80T (now {rs['wresbal_T']:.3f}T) or 4w change < -200B (now "
        f"{f(wr['d4w_B'], 1, True)}B) together with funding confirmation and TGA explaining "
        f"< 60% of the decline (now {f(frac * 100 if frac is not None else None, 0)}%)",
        policy_trigger(r),
        facility_trigger(r),
        (f"VIX > 20 on 3 of 5 completed closes (now {v['n_above20']} of 5) together with credit "
         f"widening; VIX alone is capped at {VOL_CAP:.0f} while credit and funding stay below 30"
         if v else "VIX: VIXCLS missing/stale; restore a completed close before relying on the "
                   "volatility leg"),
    ]
    lines = [
        f"MODULE: {MODULE_NAME}",
        f"ASOF: {asof_line}",
        f"SCORE_0_100: {r['score']}",
        f"CONFIDENCE_0_100: {r['confidence']}",
        "TOP_3_DRIVERS:",
        *[f"- {x}" for x in drivers],
        "CHANGE_MY_MIND (TRIGGERS):",
        *[f"- {x}" for x in triggers],
        "KEY_METRICS:",
        f"- HY_OAS_BAMLH0A0HYM2={cm('HY')}",
        f"- IG_OAS_BAMLC0A0CM={cm('IG')}",
        f"- BBB_OAS_BAMLC0A4CBBB={cm('BBB')}",
        f"- BAA10Y={cm('BAA10Y')}",
        (f"- VIXCLS={v['value']:.2f} ({v['date']} completed close; 5d {f(v['d5_pct'], 2, True)}% vs "
         f"{v['d5_from']} {v['d5_from_value']:.2f}; closes > 20: {v['n_above20']} of 5)" if v else
         "- VIXCLS=N/A (missing or > 5 business days stale; not supplied by cache)"),
        f"- WALCL={wa['value_m'] / 1e6:.6f}T ({wa['date']} Wednesday level; 1w "
        f"{f(wa['d1w_B'], 3, True)}B, 4w {f(wa['d4w_B'], 3, True)}B)",
        f"- WRESBAL={wr['value_m'] / 1e6:.6f}T (week ending {wr['date']}; 1w "
        f"{f(wr['d1w_B'], 3, True)}B, 4w {f(wr['d4w_B'], 3, True)}B vs {wr['d4w_from']})",
        f"- WTREGEN_TGA={tg['value_m'] / 1e6:.6f}T (week ending {tg['date']}; 1w "
        f"{f(tg['d1w_B'], 3, True)}B, 4w {f(tg['d4w_B'], 3, True)}B; TGA_explained_fraction "
        f"{f(frac * 100 if frac is not None else None, 1)}%)",
        f"- RRPONTSYD={rs['rrp_B']:.3f}B ({rs['rrp_date']})",
        f"- STANDING_REPO_ACCEPTED={fu['repo_latest_B']:.3f}B ({fu['repo_date']}; RPONTSYD proxy, "
        f"raw {fu['repo_latest_raw_B']:.3f}B; 5d max {fu['repo_max_B']:.3f}B, median "
        f"{fu['repo_median_B']:.3f}B after test exclusion)",
        f"- SOFR_IORB_SPREAD={sofr_line}",
        f"- PRIMARY_CREDIT={fa['primary_B']:.3f}B ({fa['date']}; 1w {f(fa['primary_1w_pct'], 1, True)}%, "
        f"4w max {fa['primary_4w_max_B']:.3f}B)",
        f"- CENTRAL_BANK_SWAPS={fa['swaps_B']:.3f}B ({fa['swaps_date']})",
        f"- NFCI={br['nfci']:.3f} (week ending {br['nfci_date']}; 4w {f(br['nfci_4w'], 3, True)})",
        f"- STLFSI4={br['stlfsi4']:.4f} (week ending {br['stlfsi4_date']}; 4w "
        f"{f(br['stlfsi4_4w'], 4, True)})",
        f"- RESERVE_MANAGEMENT_PURCHASES_PLANNED={f(facts.get('reserve_management_purchases_planned_B'), 1)}B "
        f"({facts.get('operating_period', 'N/A')}; reinvestment "
        f"{f(facts.get('reinvestment_purchases_planned_B'), 1)}B not counted; "
        f"{'primary-confirmed' if facts.get('schedule_confirmed_primary') else 'secondary-sourced, unconfirmed by NY Fed'})",
        f"- SUBSCORES=CreditStress {f(comp['CreditStress'])}; VolatilityStress {f(comp['VolatilityStress'])}; "
        f"ReserveBuffer {comp['ReserveBufferScore']:.2f}; FundingMarket {comp['FundingMarketScore']:.2f}; "
        f"Facility {comp['FacilityStress']:.2f}; Broad {comp['BroadStress']:.2f}; "
        f"LiquidityFundingStress {comp['LiquidityFundingStress']:.2f}; unrounded {comp['unrounded']:.3f}",
        f"- FLAGS=credit_watch {flagstr(fl['credit_watch'])}; credit_confirmation "
        f"{flagstr(fl['credit_confirmation'])}; funding_watch {flagstr(fl['funding_watch'])}; "
        f"funding_confirmation {flagstr(fl['funding_confirmation'])}; reserve_watch "
        f"{flagstr(fl['reserve_watch'])}; temporary_TGA {flagstr(fl['temporary_tga'])}; "
        f"genuine_scarcity {flagstr(fl['genuine_scarcity'])}; calendar_discount "
        f"{flagstr(fl['calendar_discount'])}",
        "NOTES:",
        *[f"- {x}" for x in notes_lines(r)],
    ]
    return "\n".join(lines)


def facility_trigger(r: dict) -> str:
    """change_my_mind_triggers: 'Primary credit > 25B and accelerating, or central-bank swaps
    > 5B and rising', with the current values."""
    fa = r["facilities"]
    return (f"Facilities: primary credit > 25B and accelerating (now {fa['primary_B']:.3f}B "
            f"{fa['date']}; 1w {f(fa['primary_1w_B'], 3, True)}B / "
            f"{f(fa['primary_1w_pct'], 1, True)}%, prior week {f(fa['primary_prior_1w_B'], 3, True)}B; "
            f"accelerating = 1w rise larger than the prior week's: "
            f"{flagstr(fa['primary_accelerating'])}) or central-bank swaps > 5B and rising (now "
            f"{fa['swaps_B']:.3f}B {fa['swaps_date']}; 1w {f(fa['swaps_1w_B'], 3, True)}B); "
            f"trigger met {flagstr(fa['trigger_met'])}")


def policy_trigger(r: dict) -> str:
    """Computed from policy_score(): which planned-purchase sizes move the policy subscore."""
    rs, facts = r["reserves"], r["facts"]
    wt, qt, cur = rs["wresbal_T"], bool(facts.get("active_qt")), rs["policy_score"]
    # overall-score points per policy-subscore point
    k = TOP_WEIGHTS["liquidity"] * LFS_WEIGHTS["reserve"] * RESERVE_WEIGHTS["policy"]
    if qt:
        return (f"Policy: active QT holds the policy subscore at {cur:.0f}; an end to runoff, or any "
                f"standing-repo access change, would re-score it")
    tiers = [(0.001, "> 0B"), (10.0, ">= 10B"), (40.0, ">= 40B")]
    moves = []
    for amt, label in tiers:
        new = policy_score(amt, wt, False)
        if new < cur:
            moves.append((label, new, (new - cur) * k))
    rmp = rs["rmp_B"] or 0.0
    if not moves:
        head = (f"Policy: planned reserve-management purchases {rmp:.1f}B already hold the policy "
                f"subscore at its floor {cur:.0f}")
    else:
        lab, new, pts = moves[0]
        head = (f"Policy: any planned reserve-management purchase {lab} moves the policy subscore "
                f"{cur:.0f} -> {new:.0f} ({pts:+.2f} overall pts)")
        if len(moves) > 1:
            head += " (" + ", ".join(f"{lb} -> {nw:.0f}, {pt:+.2f} pts" for lb, nw, pt
                                     in moves[1:]) + ")"
    floor = policy_score(0.0, wt, False)
    zero = (f"; a 0B schedule with WRESBAL back >= 3.0T would score "
            f"{policy_score(0.0, 3.0, False):.0f}" if rmp == 0 and floor == 60 else "")
    return (head + zero + f"; resumed QT -> {policy_score(None, wt, True):.0f}; a change in "
            "standing-repo access is a spec trigger; reinvestment purchases never count")


def driver_lines(r: dict) -> list[str]:
    c, v, fu, rs, comp = r["credit"], r["vix"], r["funding"], r["reserves"], r["components"]
    hy = c["HY"]
    frac = rs["tga_explained_fraction"]
    wr = rs["wresbal"]
    out = []
    items = sorted(r["contrib"].items(), key=lambda kv: -kv[1])
    for k, pts in items:
        if k == "liquidity":
            out.append(
                f"LiquidityFundingStress {comp['LiquidityFundingStress']:.1f} ({pts:.2f} pts): "
                f"reserves {rs['wresbal_T']:.3f}T, 4w {wr['d4w_B']:+.1f}B with TGA "
                f"{rs['tga']['d4w_B']:+.1f}B ({f(frac * 100 if frac is not None else None, 0)}% "
                f"TGA-explained), ON RRP {rs['rrp_B']:.3f}B, "
                f"{f(rs['rmp_B'], 0)}B reserve-management purchases (policy subscore "
                f"{rs['policy_score']:.0f}); SOFR-IORB 5d median {fu['median_pos_bp']:.0f}bp, "
                f"max {fu['max_pos_bp']:.0f}bp")
        elif k == "credit":
            def lv(key):
                return f"{c[key]['value']:.2f}%" if c.get(key) else "N/A"
            if hy:
                since = (f"is {bp(hy['value'] - r['prior_hy']):+.0f}bp vs the {r['prior_asof']} "
                         f"packet but " if r.get("prior_hy") is not None else "")
                hy_txt = (f"HY {hy['value']:.2f}% {since}{f(hy['d20_bp'], 0, True)}bp over 20 "
                          f"bond-market sessions vs {hy['d20_from']} (trend overlay "
                          f"{r['hy_trend_overlay']:+d})")
            else:
                hy_txt = "HY N/A (missing; credit weights renormalized, no trend overlay)"
            out.append(
                f"CreditStress {comp['CreditStress']:.1f} ({pts:.2f} pts): {hy_txt}; IG "
                f"{lv('IG')}, BBB {lv('BBB')}, BAA10Y "
                f"{lv('BAA10Y')}; cross-spread overlay {r['cross_spread_overlay']:+d}")
        else:
            out.append(
                f"VolatilityStress {comp['VolatilityStress']:.1f} ({pts:.2f} pts): VIX "
                f"{v['value']:.2f}, 5d {v['d5_pct']:+.1f}% (overlay {v['o5']:+d}), "
                f"{v['n_above20']} of 5 closes above 20 (persistence {v['persistence']:+d}); "
                f"anti-duplication cap {'binding' if comp['vol_cap_binding'] else 'not binding'}")
    kept = ", ".join(k for k in TOP_WEIGHTS if k not in r["top_weight_renormalized"])
    if r["vix"] is None:
        out.append(f"VolatilityStress N/A: VIXCLS missing or > {VIX_MAX_STALE_BD} business days "
                   f"stale vs report date; top-level weights renormalized over {kept}; score "
                   "PROVISIONAL")
    if comp["CreditStress"] is None:
        out.append(f"CreditStress N/A: no credit series within {CREDIT_MAX_STALE_BD} business days "
                   f"of the report date; top-level weights renormalized over {kept}; score "
                   "PROVISIONAL")
    return out[:3]


def notes_lines(r: dict) -> list[str]:
    fu, rs, fl, facts, comp = r["funding"], r["reserves"], r["flags"], r["facts"], r["components"]
    c, ice, conv = r["credit"], r["ice_context"], r["ice_convention"]
    hy = c.get("HY")
    ded_txt = [t for _, t in r["confidence_deductions"]]
    n = []
    if r.get("provisional"):
        why = []
        for sid, (dd, bd) in sorted(r["hy_vix_stale"].items()):
            why.append(f"{sid} last {dd}, {bd} business days stale vs report date "
                       f"{r['report_date']} (> {HY_VIX_PROVISIONAL_BD})")
        for sid in r["missing_core"]:
            if sid in ("BAMLH0A0HYM2", "VIXCLS") and sid not in r["hy_vix_stale"]:
                why.append(f"{sid} missing")
        legs = r["top_weight_renormalized"]
        dropped = r.get("stale_dropped", {})
        n.append("PROVISIONAL (incomplete_row_rule: HY OAS or VIX missing or stale > "
                 f"{HY_VIX_PROVISIONAL_BD} business days; no fully confident official score): "
                 + "; ".join(why)
                 + ("; dropped as > 5 business days stale: "
                    + ", ".join(f"{k} ({v[0]})" for k, v in sorted(dropped.items()))
                    if dropped else "")
                 + ("; top-level weights renormalized over "
                    + ", ".join(k for k in TOP_WEIGHTS if k not in legs)
                    + " (" + ", ".join(legs) + " missing)" if legs else "")
                 + (f"; -8 confidence per missing core series ({len(r['missing_core'])})"
                    if r["missing_core"] else "")
                 + ("; -5 confidence for core daily staleness" if r["core_daily_stale"] else ""))
    elif r["core_daily_stale"]:
        n.append(f"Core daily staleness vs report date {r['report_date']}: "
                 + ", ".join(r["core_daily_stale"]) + "; -5 confidence")
    n.append(GOVERNANCE_NOTE)
    k = r["transition_update_n"]
    if k <= TRANSITION_UPDATES:
        n.append(f"Transition disclosure (methodology_history {FORMALIZATION_DATE}): update {k} of "
                 f"the first {TRANSITION_UPDATES} under the formalized implementation (listed "
                 f"updates {','.join(r['formalized_updates']) or 'none'}); a mechanically "
                 "reproduced score may differ by approximately 1-3 points from older manually "
                 "assigned component scores")
    n.append(
        f"Mixed dates: credit/VIX {r['asof']}, SOFR/IORB {fu['sofr_date']}, ON RRP/repo "
        f"{rs['rrp_date']}, H.4.1 {rs['wresbal']['date']}, NFCI {r['broad']['nfci_date']}, STLFSI4 "
        f"{r['broad']['stlfsi4_date']}; WALCL is a Wednesday level, WRESBAL/WTREGEN week averages, "
        "so TGA attribution is approximate, not an identity")
    n.append("Standing repo uses the FRED RPONTSYD proxy (direct NY Fed results not ingested); "
             f"daily totals <= {REPO_TEST_MAX_B:.3f}B treated as small-value tests; -5 confidence")
    n.append(f"Calendar: window {','.join(fu['calendar_window_days']) or 'none'} touched; discount "
             f"{'APPLIED' if fu['calendar_discount'] else 'NOT applied'} ({fu['calendar_reason']})")
    # policy schedule / implementation note, computed from the facts file
    sched_ok = bool(facts.get("schedule_confirmed_primary"))
    fomc = sorted((kk, vv) for kk, vv in facts.items()
                  if kk.startswith("fomc_") and isinstance(vv, dict))
    pol = (f"Policy schedule {facts.get('operating_period', 'N/A')}: "
           + ("primary-confirmed" if sched_ok else
              facts.get("schedule_status", "unconfirmed").rstrip(".")))
    for kk, vv in fomc:
        label = kk[len("fomc_"):].replace("_", "-")
        pol += (f"; FOMC {label}: " + ("confirmed" if vv.get("confirmed") else
                                       vv.get("status", "unconfirmed").rstrip(".")))
    if any(t.startswith("current purchase schedule") for t in ded_txt):
        pol += "; -5 confidence for unclear schedule/implementation note"
    n.append(pol)
    # implementation change: session calendars (disclosed for report dates on/after the change)
    if r["report_date"] >= IMPL_CHANGE_DATE:
        dropped = {kk: v for kk, v in r["dropped_non_session_prints"].items() if v}
        chg, same = [], []
        for kk in ("HY", "IG", "BBB", "BAA10Y"):
            x = c.get(kk)
            if not x or x["d20_own_print_bp"] is None:
                continue
            if x["d20_own_print_bp"] != x["d20_bp"]:
                chg.append(f"{kk} 20d {x['d20_own_print_bp']:+.0f}bp -> {x['d20_bp']:+.0f}bp "
                           f"(vs {x['d20_from']} {x['d20_from_value']:.2f})")
            else:
                same.append(kk)
        kept = r.get("vix_kept_bond_holidays", [])
        n.append(
            f"Implementation change {IMPL_CHANGE_DATE} (no anchor/weight change): 5/20/63-session "
            "changes for ICE OAS count SIFMA bond-market sessions and VIX persistence/5d use "
            "NYSE/Cboe completed closes, instead of each FRED series' own prints; non-session "
            "prints dropped in the last 90 observations: "
            + ("; ".join(f"{kk} {','.join(v)}" for kk, v in dropped.items()) or "none")
            + "; effect: " + ("; ".join(chg) if chg else "no 20d change moved")
            + (f" ({','.join(same)} unchanged)" if same else "")
            + f"; HY trend overlay {r['hy_trend_overlay']:+d}, cross-spread overlay "
            f"{r['cross_spread_overlay']:+d}"
            + ("; VIX closes kept on SIFMA-closed Cboe sessions: " + ",".join(kept)
               if kept else ""))
    # ICE truncation: convention applied, literal reading disclosed
    starts = ",".join(sorted(set(conv["history_start"].values()))) or "N/A"
    if conv["deduct"]:
        lit = ("would also deduct" if ice["deduct"] else "would not deduct")
        n.append(f"ICE truncation: FRED ICE OAS history starts {starts} (after "
                 f"{ICE_FULL_HISTORY_BEFORE}), so the agent-of-record convention deducts 2 "
                 f"(confidence {r['confidence']}); the literal reading (deduct only if truncation "
                 f"prevents the 3-year context from {ice['window_start']}, first SIFMA session "
                 f"{ice['first_session']}) {lit} and would give {r['confidence_literal_ice']}; the "
                 "literal outcome depends on the window date and FRED's rolling start, so it is "
                 "not applied; changing the convention needs Jason's approval")
    elif ice["deduct"]:
        n.append(f"ICE truncation: FRED ICE OAS history starts {starts} (full history), no "
                 f"convention deduction; literal 3-year check would deduct "
                 f"(confidence {r['confidence_literal_ice']})")
    if hy:
        pk = r["hy_peak_20d_change_recent"]
        n.append(f"HY 20-session window starts {hy['d20_from']} ({hy['d20_from_value']:.2f}); peak "
                 f"20-session change in the last 21 sessions "
                 + (f"{pk[1]:+.0f}bp on {pk[0]}" if pk else "N/A")
                 + f"; 63-session {f(hy['d63_bp'], 0, True)}bp; overlay applied exactly as of "
                 f"{r['credit_cutoff']}")
    if not r["live"] or r.get("credit_asof_override"):
        n.append("Replay: fredgraph.csv has no vintages, so recomputation for "
                 f"{r['asof']} uses current NFCI/STLFSI4 vintages (revised weekly)"
                 + (f"; credit series cut at {r['credit_asof_override']} to emulate the "
                    "publication lag" if r.get("credit_asof_override") else ""))
    if r.get("prior_score") is not None:
        alt = round_half_up(comp["unrounded_from_1dp_components"])
        n.append(f"Score change {r['score_delta']:+d} vs prior official {r['prior_score']} at "
                 f"{r['prior_asof']}; one-decimal component path gives "
                 f"{comp['unrounded_from_1dp_components']:.2f} -> "
                 + ("same integer" if alt == r["score"] else f"{alt}, a different integer"))
    else:
        n.append("No prior score supplied")
    n.extend(r["notes"])
    return n


CSV_COLS = [
    "report_asof", "score_0_100", "confidence_0_100", "credit_stress", "volatility_stress",
    "reserve_buffer_score", "funding_market_score", "facility_stress", "broad_stress",
    "liquidity_funding_stress", "hy_oas_pct", "hy_oas_20d_change_bp", "ig_oas_pct", "bbb_oas_pct",
    "baa10y_pct", "vix_close", "vix_5d_change_pct", "walcl_T", "wresbal_T", "wresbal_4w_change_B",
    "tga_T", "tga_4w_change_B", "tga_explained_fraction", "on_rrp_B", "standing_repo_accepted_B",
    "sofr_pct", "iorb_pct", "sofr_iorb_bp", "primary_credit_B", "central_bank_swaps_B", "nfci",
    "stlfsi4", "reserve_management_purchases_planned_B", "credit_confirmation_flag",
    "funding_confirmation_flag", "temporary_tga_flag", "genuine_scarcity_flag",
    "calendar_adjustment_flag", "notes",
]


def csv_block(r: dict, notes: str) -> str:
    c, comp, rs, fu, fa, br, fl = (r["credit"], r["components"], r["reserves"], r["funding"],
                                   r["facilities"], r["broad"], r["flags"])
    frac = rs["tga_explained_fraction"]
    rmp = r["facts"].get("reserve_management_purchases_planned_B")

    def cv(k, key, nd):
        return f(c[k][key], nd) if c.get(k) else "N/A"

    vals = [
        r["asof"], r["score"], r["confidence"], f(comp["CreditStress"], 3),
        f(comp["VolatilityStress"], 3), f"{comp['ReserveBufferScore']:.3f}",
        f"{comp['FundingMarketScore']:.3f}", f"{comp['FacilityStress']:.3f}",
        f"{comp['BroadStress']:.3f}", f"{comp['LiquidityFundingStress']:.3f}",
        cv("HY", "value", 2), cv("HY", "d20_bp", 0), cv("IG", "value", 2),
        cv("BBB", "value", 2), cv("BAA10Y", "value", 2),
        f(r["vix"]["value"] if r["vix"] else None, 2),
        f(r["vix"]["d5_pct"] if r["vix"] else None, 3), f"{rs['walcl']['value_m'] / 1e6:.6f}",
        f"{rs['wresbal_T']:.6f}", f"{rs['wresbal']['d4w_B']:.3f}",
        f"{rs['tga']['value_m'] / 1e6:.6f}", f"{rs['tga']['d4w_B']:.3f}",
        f"{frac:.4f}" if frac is not None else "N/A", f"{rs['rrp_B']:.3f}",
        f"{fu['repo_latest_B']:.3f}", f"{fu['sofr']:.2f}", f"{fu['iorb']:.2f}",
        f"{fu['spread_bp']:.0f}", f"{fa['primary_B']:.3f}", f"{fa['swaps_B']:.3f}",
        f"{br['nfci']:.3f}", f"{br['stlfsi4']:.4f}", f"{rmp:.1f}" if rmp is not None else "N/A",
        flagstr(fl["credit_confirmation"]), flagstr(fl["funding_confirmation"]),
        flagstr(fl["temporary_tga"]), flagstr(fl["genuine_scarcity"]),
        flagstr(fl["calendar_discount"]), notes,
    ]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLS)
    w.writerow(vals)
    return buf.getvalue().rstrip("\n")


def print_summary(r: dict) -> None:
    comp, c, v, fu, rs, fa, br, fl = (r["components"], r["credit"], r["vix"], r["funding"],
                                      r["reserves"], r["facilities"], r["broad"], r["flags"])
    print(f"{MODULE_NAME}  asof {r['asof']}  report {r['report_date']}")
    print(f"SCORE {r['score']}  (unrounded {comp['unrounded']:.4f})  CONF {r['confidence']}  "
          f"regime {r['regime']}")
    for k in ("HY", "IG", "BBB", "BAA10Y"):
        x = c[k]
        if x is None:
            print(f"  {k:6s} N/A (missing or > 5 business days stale)")
            continue
        print(f"  {k:6s} {x['value']:.2f} ({x['date']}) score {x['score']:.3f}  5d "
              f"{f(x['d5_bp'], 0, True)}bp  20d {f(x['d20_bp'], 0, True)}bp (vs {x['d20_from']} "
              f"{f(x['d20_from_value'])}; own-print count {f(x['d20_own_print_bp'], 0, True)}bp vs "
              f"{x['d20_own_print_from']})  63d {f(x['d63_bp'], 0, True)}bp  20d-low {x['low20']:.2f}")
    print(f"  credit level {r['credit_level_weighted']:.4f}  HY overlay {r['hy_trend_overlay']}  "
          f"cross {r['cross_spread_overlay']}  -> CreditStress {f(comp['CreditStress'], 4)}")
    print(f"  HY peak 20d change in last 21 sessions: {r['hy_peak_20d_change_recent']}")
    print(f"  non-session prints dropped (last 90 obs): {r['dropped_non_session_prints']}")
    if v:
        print(f"  VIX {v['value']:.2f} ({v['date']}) level {v['level_score']:.4f} 5d "
              f"{v['d5_pct']:+.3f}% o5 {v['o5']} pers {v['persistence']} -> uncapped "
              f"{comp['VolatilityStress_uncapped']:.4f} cap eligible {comp['vol_cap_eligible']} "
              f"-> {comp['VolatilityStress']:.4f}")
    else:
        print("  VIX N/A (missing or > 5 business days stale)")
    print(f"  ICE convention {r['ice_convention']}  literal 3-year check {r['ice_context']} "
          f"(literal confidence {r['confidence_literal_ice']})")
    if r.get("provisional"):
        print(f"  PROVISIONAL: missing core {r['missing_core']}; top-level renormalized "
              f"{r['top_weight_renormalized']}")
    print(f"  SOFR window {fu['window']} median {fu['median_pos_bp']} max {fu['max_pos_bp']} "
          f"score {fu['sofr_score']:.4f}")
    print(f"  repo window {fu['repo_window_raw']} -> clean max {fu['repo_max_B']} med "
          f"{fu['repo_median_B']} score {fu['repo_score']:.4f}")
    print(f"  calendar days {fu['calendar_window_days']} discount {fu['calendar_discount']} "
          f"({fu['calendar_reason']}) -> FundingMarket {comp['FundingMarketScore']:.4f}")
    wr, tg = rs["wresbal"], rs["tga"]
    print(f"  WRESBAL {rs['wresbal_T']:.6f}T ({wr['date']}) 1w {wr['d1w_B']:+.3f} 4w {wr['d4w_B']:+.3f}"
          f" 13w {f(wr['d13w_B'], 3, True)}  abs {rs['abs_score']:.4f} chg {rs['chg4w_score']:.4f}")
    print(f"  TGA {tg['value_m'] / 1e6:.6f}T 1w {tg['d1w_B']:+.3f} 4w {tg['d4w_B']:+.3f}  frac "
          f"{rs['tga_explained_fraction']}")
    print(f"  WALCL {rs['walcl']['value_m'] / 1e6:.6f}T 1w {rs['walcl']['d1w_B']:+.3f} 4w "
          f"{rs['walcl']['d4w_B']:+.3f}")
    print(f"  ON RRP {rs['rrp_B']} ({rs['rrp_date']}) score {rs['rrp_score']}  policy "
          f"{rs['policy_score']} (RMP {rs['rmp_B']}) -> ReserveBuffer {comp['ReserveBufferScore']:.4f}")
    print(f"  primary {fa['primary_B']:.3f}B 1w {f(fa['primary_1w_pct'], 2, True)}% swaps "
          f"{fa['swaps_B']:.3f}B -> Facility {comp['FacilityStress']:.4f}")
    print(f"  NFCI {br['nfci']} ({br['nfci_date']}) {br['nfci_score']:.4f}  STLFSI4 {br['stlfsi4']} "
          f"({br['stlfsi4_date']}) {br['stlfsi4_score']:.4f} -> Broad {comp['BroadStress']:.4f}")
    print(f"  scarcity overlay {comp['genuine_scarcity_overlay']} -> LFS "
          f"{comp['LiquidityFundingStress']:.4f}")
    print(f"  flags {json.dumps(fl)}")
    print(f"  confidence deductions {r['confidence_deductions']}")


def self_test(cache: str) -> int:
    fails = []

    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # arithmetic
    check(abs(interp(2.70, A_HY) - 20 * 0.20 / 0.75) < 1e-9, "HY anchor interpolation")
    check(interp(0.0, A_ONRRP_B) == 60 and interp(1000, A_ONRRP_B) == 0, "clamping")
    check(hy_trend_overlay(25) == 5 and hy_trend_overlay(24.999) == 0, "HY overlay edges")
    check(vix_5d_overlay(-5) == -5 and vix_5d_overlay(5) == 5, "VIX overlay edges")
    check(round_half_up(9.5) == 10 and round_half_up(9.4999) == 9, "round half up")
    check(bp(3.95 - 3.90) == 5.0, "bp de-noise")
    check(calendar_window_days(d("2026-10-29")) and calendar_window_days(d("2026-10-30"))
          and not calendar_window_days(d("2026-10-28")), "calendar window Oct-2026 month-end")
    # session calendars: ICE on SIFMA (Columbus Day closed), VIX on NYSE/Cboe (open)
    check(not is_session(d("2026-10-12"), SESSION_CALENDAR["BAMLH0A0HYM2"])
          and is_session(d("2026-10-12"), SESSION_CALENDAR["VIXCLS"])
          and not is_session(d("2026-09-07"), SESSION_CALENDAR["VIXCLS"])
          and not is_session(d("2026-09-07"), SESSION_CALENDAR["BAMLC0A4CBBB"]),
          "session calendars (2026-10-12 SIFMA closed / Cboe open; 2026-09-07 both closed)")
    check(next_month_end_turn("2026-10-30") == ("2026-10-30", "2026-11-03")
          and next_month_end_turn("2026-10-15") == ("2026-10-29", "2026-11-03")
          and next_month_end_turn("2026-11-02") == ("2026-11-02", "2026-11-03"),
          "computed month-end turn window")
    # ICE truncation: convention (history after 2000 -> -2) vs literal 3-year window check
    ice_ok = ice_context_check({"X": [("2023-10-30", 1.0)]}, "2026-10-28")
    ice_short = ice_context_check({"X": [("2023-10-30", 1.0)]}, "2026-09-16")
    check(not ice_ok["deduct"] and ice_ok["first_session"] == "2023-10-30"
          and ice_short["deduct"] and ice_short["first_session"] == "2023-09-18",
          "ICE literal check is window-date dependent (2026-10-28 none, 2026-09-16 deducts)")
    check(ice_convention_check({"X": [("2023-10-30", 1.0)]})["deduct"]
          and not ice_convention_check({"X": [("1996-12-31", 1.0)]})["deduct"],
          "ICE convention: truncated FRED window deducts, full history does not")
    # transition-disclosure count from the explicit formalized-update list
    check([transition_update_number(x, FORMALIZED_UPDATE_DATES)
           for x in ("2026-09-17", "2026-10-28", "2026-11-25")] == [1, 2, 3]
          and transition_update_number("2026-12-30",
                                       FORMALIZED_UPDATE_DATES + ["2026-11-25"]) == 4,
          "transition update count (09-17 -> 1, 10-28 -> 2, next -> 3, after three -> 4)")
    # policy subscore tiers: any planned purchase > 0 moves 60 -> 25 (>=10B 15, >=40B 0)
    check([policy_score(x, 2.9, False) for x in (0.0, 0.001, 9.99, 10, 39.9, 40)]
          == [60, 25, 25, 15, 15, 0], "policy subscore tiers")
    if not cache:
        print("no --cache: skipping 2026-09-17 validation")
        return 1 if fails else 0
    V = VALIDATION
    r = compute(cache, VALIDATION_FACTS, asof=V["asof"], credit_asof=V["credit_asof"])
    # 93 = 100 - 5 (RPONTSYD proxy) - 2 (ICE truncation). Under the literal rule the -2 applies
    # here because the cached FRED ICE history (2023-10-30) starts after the first session
    # (2023-09-18) of the 3-year window ending 2026-09-16; it is not applied unconditionally.
    comp, c, rs, fu, fa, br = (r["components"], r["credit"], r["reserves"], r["funding"],
                               r["facilities"], r["broad"])
    check(abs(r["score"] - V["score"]) <= 1, f"score {r['score']} vs {V['score']}")
    for k in ("CreditStress", "VolatilityStress", "LiquidityFundingStress"):
        check(abs(comp[k] - V[k]) <= 2, f"{k} {comp[k]:.3f} vs {V[k]}")
    check(r["confidence"] == V["confidence"], f"confidence {r['confidence']} vs {V['confidence']}")
    for k in ("HY", "IG", "BBB", "BAA10Y"):
        check(c[k]["value"] == V[k] and c[k]["date"] == V["credit_asof"], f"{k} {c[k]['value']}")
    check(r["vix"]["value"] == V["VIX"] and r["vix"]["date"] == V["asof"], "VIX 15.44 09-17")
    check(abs(rs["wresbal_T"] - V["WRESBAL_T"]) < 1e-9 and rs["wresbal"]["date"] == "2026-09-16",
          "WRESBAL")
    check(abs(rs["wresbal"]["d4w_B"] - V["WRESBAL_4w_B"]) < 1e-9, "WRESBAL 4w")
    check(abs(rs["tga"]["value_m"] / 1e6 - V["WTREGEN_T"]) < 1e-9, "WTREGEN")
    check(abs(rs["tga"]["d4w_B"] - V["WTREGEN_4w_B"]) < 1e-9, "WTREGEN 4w")
    check(abs(rs["walcl"]["value_m"] / 1e6 - V["WALCL_T"]) < 1e-9, "WALCL")
    check(rs["rrp_B"] == V["RRP_B"], "ON RRP")
    check(fu["sofr"] == V["SOFR"] and fu["iorb"] == V["IORB"] and fu["spread_bp"] == V["SOFR_IORB_bp"]
          and fu["sofr_date"] == V["asof"], "SOFR-IORB -5bp 09-17")
    check(abs(fa["primary_B"] - V["PRIMARY_B"]) < 1e-9 and abs(fa["swaps_B"] - V["SWAPS_B"]) < 1e-9,
          "facilities")
    check(br["nfci_date"] == "2026-09-11" and br["stlfsi4_date"] == "2026-09-11",
          "broad index week ending 09-11")
    # NFCI/STLFSI4 are revised weekly; current vintage may differ from the packet's values.
    check(abs(br["nfci"] - V["NFCI"]) < 0.05 and abs(br["stlfsi4"] - V["STLFSI4"]) < 0.01,
          f"NFCI {br['nfci']} / STLFSI4 {br['stlfsi4']} within revision tolerance")
    check(r["ice_convention"]["deduct"] and r["ice_context"]["deduct"]
          and r["confidence_literal_ice"] == V["confidence"],
          "2026-09-17 ICE -2 applies by convention (and literal reading agrees)")
    vt = packet(r)
    check(vt.startswith(f"MODULE: {MODULE_NAME}\n") and "Transition disclosure" in vt
          and "update 1 of the first 3" in vt and f"Implementation change {IMPL_CHANGE_DATE}"
          not in vt and "PROVISIONAL" not in vt,
          "2026-09-17 packet: contract module name, transition update 1, no 10-30 note")
    # live-cache checks (2026-10-30 report): SIFMA session counting and robustness
    lv = compute(cache, LAST_KNOWN_FACTS, report_date="2026-10-30")
    if lv["asof"] == "2026-10-28":
        hyl, bbbl = lv["credit"]["HY"], lv["credit"]["BBB"]
        check(hyl["d20_from"] == "2026-09-29" and hyl["d20_bp"] == 6.0 and bbbl["d20_bp"] == 5.0
              and lv["hy_trend_overlay"] == 0 and lv["cross_spread_overlay"] == 0,
              f"2026-10-28 HY 20d {hyl['d20_bp']:+.0f}bp vs {hyl['d20_from']}, BBB "
              f"{bbbl['d20_bp']:+.0f}bp, overlays 0")
        check(lv["ice_convention"]["deduct"] and not lv["ice_context"]["deduct"]
              and lv["confidence"] == 88 and lv["confidence_literal_ice"] == 90
              and lv["score"] == 20 and not lv["provisional"],
              f"2026-10-30 report: score {lv['score']}, confidence {lv['confidence']} by "
              f"convention, literal reading {lv['confidence_literal_ice']}")
        lt = packet(lv)
        check(lt.startswith("MODULE: Liquidity and Credit\n") and GOVERNANCE_NOTE in lt
              and "update 2 of the first 3" in lt
              and f"Implementation change {IMPL_CHANGE_DATE}" in lt
              and "Facilities: primary credit > 25B and accelerating (now 9.538B" in lt
              and "trigger met FALSE" in lt and "would give 90" in lt,
              "2026-10-30 packet: module name, governance, transition 2/3, impl note, "
              "facility trigger, ICE disclosure")
        pt = policy_trigger(lv)
        check("> 0B moves the policy subscore 60 -> 25" in pt and ">= 10B -> 15" in pt
              and ">= 40B -> 0" in pt,
              "policy trigger text computed")
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for fn in os.listdir(cache):
            if fn.endswith(".csv") and fn not in ("BAMLH0A0HYM2.csv", "VIXCLS.csv"):
                shutil.copy(os.path.join(cache, fn), tmp)
        try:
            m = compute(tmp, LAST_KNOWN_FACTS, report_date="2026-10-30")
            txt = packet(m)
            ded = [x for x, _ in m["confidence_deductions"]]
            check(m["provisional"] and "BAMLH0A0HYM2" in m["missing_core"]
                  and "VIXCLS" in m["missing_core"] and ded.count(8) == 2
                  and "PROVISIONAL" in txt,
                  f"missing HY+VIX: no crash, provisional, two -8 deductions (conf {m['confidence']})")
        except Exception as exc:  # noqa: BLE001
            check(False, f"missing HY+VIX crashed: {exc!r}")
        # staleness: a single 5-point deduction even when several core dailies are stale
        st = compute(cache, LAST_KNOWN_FACTS, report_date="2026-11-04")
        n5 = [t for x, t in st["confidence_deductions"] if "stale vs report date" in t]
        check(len(n5) == 1, f"staleness deduction not stacked ({n5})")
    # staleness is measured against --report-date, not the as-of found in the data
    if lv["asof"] == "2026-10-28":
        # 2026-11-03: HY/VIX 4 business days stale -> still scored, but PROVISIONAL
        s4 = compute(cache, LAST_KNOWN_FACTS, report_date="2026-11-03")
        check(s4["credit"]["HY"] is not None and s4["credit"]["HY"]["stale_bd"] == 4
              and s4["vix"] is not None and s4["vix"]["stale_bd"] == 4 and s4["provisional"]
              and set(s4["hy_vix_stale"]) == {"BAMLH0A0HYM2", "VIXCLS"}
              and "PROVISIONAL" in packet(s4) and s4["confidence"] < lv["confidence"],
              f"report 2026-11-03: HY/VIX 4bd stale, scored but PROVISIONAL "
              f"(conf {s4['confidence']})")
        # 2026-11-30 on the current cache: HY/VIX ~21bd stale -> dropped, PROVISIONAL, deducted
        s30 = compute(cache, LAST_KNOWN_FACTS, report_date="2026-11-30")
        t30 = packet(s30)
        ded30 = [x for x, _ in s30["confidence_deductions"]]
        check(s30["provisional"] and s30["asof"] == "2026-10-28"
              and s30["hy_vix_stale"].get("BAMLH0A0HYM2") == ("2026-10-28", 21)
              and s30["hy_vix_stale"].get("VIXCLS") == ("2026-10-28", 21)
              and "BAMLH0A0HYM2" in s30["missing_core"] and "VIXCLS" in s30["missing_core"]
              and ded30.count(8) == len(s30["missing_core"]) and 5 in ded30
              and s30["confidence"] < 88 and t30.startswith("MODULE: Liquidity and Credit\n")
              and "PROVISIONAL" in t30.split("NOTES:")[1]
              and "21 business days stale vs report date 2026-11-30" in t30,
              f"report 2026-11-30 on current cache: HY/VIX 21bd stale vs report date, "
              f"PROVISIONAL, conf {s30['confidence']}, score {s30['score']}")
    print(f"validation unrounded {comp['unrounded']:.4f}; components "
          f"{comp['CreditStress']:.3f}/{comp['VolatilityStress']:.3f}/{comp['LiquidityFundingStress']:.3f}")
    return 1 if fails else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", metavar="DIR")
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--asof")
    ap.add_argument("--report-date")
    ap.add_argument("--credit-asof", help="emulate a past FRED vintage lag for credit series")
    ap.add_argument("--facts", help="JSON file of web-sourced policy facts")
    ap.add_argument("--prior-score", type=int, default=10)
    ap.add_argument("--prior-asof", default="2026-09-17")
    ap.add_argument("--formalized-updates",
                    help="comma-separated as-of dates of official updates under the formalized "
                         "implementation (default: FORMALIZED_UPDATE_DATES)")
    ap.add_argument("--json", action="store_true", help="dump the full result as JSON")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.fetch:
        fetch(a.fetch)
        if not a.cache:
            return 0
    if a.self_test:
        return self_test(a.cache)
    if not a.cache:
        ap.error("--cache DIR required")
    if a.facts:
        with open(a.facts) as fh:
            facts = json.load(fh)
    else:
        facts = LAST_KNOWN_FACTS
    upd = ([x.strip() for x in a.formalized_updates.split(",") if x.strip()]
           if a.formalized_updates is not None else None)
    r = compute(a.cache, facts, asof=a.asof, report_date=a.report_date, credit_asof=a.credit_asof,
                prior_score=a.prior_score, prior_asof=a.prior_asof, formalized_updates=upd)
    if a.json:
        print(json.dumps(r, indent=1, default=str))
        return 0
    print_summary(r)
    print()
    print("SUPERVISOR PACKET (COPY/PASTE)")
    print("```")
    print(packet(r))
    print("```")
    print()
    note = (("PROVISIONAL; " if r["provisional"] else "") + "Supervisor self-run; RPONTSYD proxy; "
            f"schedule {'confirmed' if r['facts'].get('schedule_confirmed_primary') else 'secondary/unconfirmed'}; "
            f"calendar discount {'applied' if r['flags']['calendar_discount'] else 'not applied'}")
    print(csv_block(r, note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
