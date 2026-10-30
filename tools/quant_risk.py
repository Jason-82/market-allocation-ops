#!/usr/bin/env python3
"""QuantRisk module engine — fixed-band ETF continuation plus full-history VIX / modern NDX.

Implements supervisor/modules/quant_risk.yaml calculations.formulas.

ETF score inputs (SPY, QQQ; IWM diagnostic) use the method of the agent of record's 2026-09-17
packet ("ETF quantiles remain fixed at uploaded-history calibration"):

  * dev_low / dev_high are held FIXED at the uploaded-history 2025-12-26 calibration
    (files_or_code_needed_for_reproduction.deterministic_test_fixtures_from_uploaded_files).
  * CANONICAL (clean fixture) seed: the 504-observation log-price EMA trend is seeded at
    2025-12-26 by inverting the fixture price_at_r0.5:
        trend = ln(p@r0.5) - dev_low - 0.5*(dev_high - dev_low)
    and risk_smooth is seeded at the fixture risk_smooth. Every cached session strictly after
    2025-12-26 is rolled forward in date order.
  * LINEAGE (re-anchored) seed, sensitivity only: trend re-anchored on the 2026-08-14 published
    p@r0.5 and risk_smooth and rolled forward. This reproduces the 2026-09-17 published composite.
    The two seeds are separate engines and are never differenced against each other.

Known lineage gap (documented, not tuned away): the clean seed puts 2026-09-17 SPY/QQQ r-levels
~0.64% below the published packet and risk_raw ~+0.006 above it; the composite is +0.0019 and
the score is identical. --self-test gates on this gap staying inside LINEAGE_TOL / DOCUMENTED_GAP.

Context rows (0% weight, never blended into the score):
  * VIX: full-history recomputation per spec from FRED VIXCLS since 1990 (504-obs EMA trend,
    full as-of-sample linear 5/95 quantiles, 28-obs smoothing). Validated in --self-test
    against the published 2026-08-14 VIX state.
  * NDX_MODERN_1999 / SPX_MODERN_1999: full recomputation (same formulas) on the index close from
    the spec's modern-window start (NDX 1999-03-10, SPX 1999-01-04), computed only when a daily
    index history reaching back to that start is supplied (--fred-idx, FRED SP500/NASDAQ100);
    otherwise N/A with the reason. Full-history index readings are never mixed with ETF-era
    calibration; they are not computed here (Stooq full histories are not cached).

Inputs
  --cache DIR      DIR/{SPY,QQQ,IWM,SPX,NDX,VIX}.csv, header date,low,high,close
                   (Robinhood split-adjusted raw closes, NOT dividend-adjusted).
  --fred-vix CSV   FRED VIXCLS (observation_date,VIXCLS): VIX spot of record, VIX risk row,
                   bar cross-check.
  --fred-vix3m CSV FRED VXVCLS (observation_date,VXVCLS): VIX/VIX3M term structure.
  --fred-idx CSV   FRED observation_date,SP500,NASDAQ100[,...]: SPX/NDX close cross-check and
                   modern-window rows where the history is long enough.
  --now T          America/Chicago wall clock YYYY-MM-DDTHH:MM (default: system clock).

Row rules, applied in order to every cached series:
  1. Current-day intraday rule (spec incomplete_row_rule): a row dated today (America/Chicago)
     before the 15:00 CT close is excluded and disclosed; a row dated after today is refused.
  2. Incomplete-final rule: a FINAL row with a missing close, missing low/high, low==high==close
     (interpolated/synthesized shape) or close outside low/high is refused unless
     --drop-incomplete-final is given; then it is dropped, the NEW final row is re-checked, and
     dropping repeats until the final row is clean (refused if more than MAX_DROP_FINAL rows
     would go). Every dropped row is disclosed and costs confidence.
  3. Any remaining row with a missing / non-positive close is refused.
  4. Interior-gap guard: each ETF series (SPY/QQQ/IWM) is checked against the NYSE session
     calendar between its first and last row. A missing session or a non-session row is refused
     unless --allow-session-gaps is given; then it is disclosed and costs confidence.
     SPX/NDX/VIX cache gaps are disclosed only (they are not score inputs).

Continuity check: after 2026-09-17 the SPY/SPX and QQQ/NDX close ratios (Robinhood caches) may
not move more than RATIO_JUMP_TOL (0.5%) per session except on the assumed ex-dividend dates
(looser 1.0% bound, reported). A break puts a WARNING line in the packet header and costs confidence; it is
not a refusal. The ex-dividend dates and amounts in DIV_EVENTS are assumed, not verified.

Exit codes
  0  report written (self-test: all checks passed)
  1  self-test failed
  2  refused: a data guard tripped (intraday/future row, incomplete final row, missing session,
     mismatched ETF dates, as-of not a cached session, unreadable input); also argparse usage errors
  3  report written but the 2026-09-17 lineage validation FAILED and --allow-unvalidated was
     not given; the packet header carries a VALIDATION FAILED warning

Usage:
    python3 tools/quant_risk.py --cache DIR [--fred-vix F] [--fred-vix3m F] [--fred-idx F]
                                [--asof YYYY-MM-DD] [--now YYYY-MM-DDTHH:MM]
    python3 tools/quant_risk.py --cache DIR --fred-vix F [--fred-idx F] --self-test

Stdlib only. Decision support only; not financial advice.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import io
import math
import os
import shutil
import sys
import tempfile

SEED_DATE = "2025-12-26"
REANCHOR_DATE = "2026-08-14"
VALIDATION_DATE = "2026-09-17"
H_TREND = 504  # observation-count halflife
H_SMOOTH = 28  # observation-count halflife
DELTA_SESSIONS = 20
R_LEVELS = (0.2, 0.5, 0.8, 0.9)
WEIGHTS = {"SPY": 0.60, "QQQ": 0.40}  # official composite; IWM is diagnostic only
ETFS = ("SPY", "QQQ", "IWM")
SERIES = ("SPY", "QQQ", "IWM", "SPX", "NDX", "VIX")
MAX_DROP_FINAL = 3
RATIO_JUMP_TOL = 0.005  # SPY/SPX, QQQ/NDX per-session ratio jump tolerance
EXDIV_JUMP_TOL = 0.010  # looser bound on assumed ex-dividend dates (expected drop ~0.3%), never unbounded

# Spec history_windows.
MODERN_START = {"SPX": "1999-01-04", "NDX": "1999-03-10"}
VIX_FULL_START_BY = "1990-01-31"  # VIX_FULL_HISTORY: "1990 onward"

EXIT_OK, EXIT_SELFTEST_FAIL, EXIT_REFUSED, EXIT_UNVALIDATED = 0, 1, 2, 3


class Refuse(Exception):
    """A data guard refused the run (exit 2)."""


# Verbatim from quant_risk.yaml deterministic_test_fixtures_from_uploaded_files.
FIXTURES = {
    "SPY": {
        "dev_low": -0.21496076081706295,
        "dev_high": 0.44640374315131187,
        "risk_raw": 1.0,
        "risk_smooth": 0.9902766253271554,
        "price_at_r0_2": 401.33246349034664,
        "price_at_r0_5": 489.40947976853505,
        "price_at_r0_8": 596.8160083642703,
        "price_at_r0_9": 637.6218031795744,
    },
    "QQQ": {
        "dev_low": -0.4310183623750675,
        "dev_high": 0.6399819596397465,
        "risk_raw": 0.9256554977445975,
        "risk_smooth": 0.9208914491037786,
        "price_at_r0_2": 286.80335953065827,
        "price_at_r0_5": 395.4786957395883,
        "price_at_r0_8": 545.3332172950608,
        "price_at_r0_9": 606.9807322152335,
    },
    "IWM": {
        "dev_low": -0.14953250892067627,
        "dev_high": 0.40090574902138165,
        "risk_raw": 0.821953596392444,
        "risk_smooth": 0.7762091605826392,
        "price_at_r0_2": 178.533772692958,
        "price_at_r0_5": 210.58918886694363,
        "price_at_r0_8": 248.40009707242604,
        "price_at_r0_9": 262.4562933498076,
    },
}

# Seam check: u at 2025-12-26 from the cached close must reproduce these (6 dp).
SEAM_U = {"SPY": 1.020048, "QQQ": 0.925655, "IWM": 0.821954}

# Agent of record, published 2026-09-17 packet.
VALIDATION_0917 = {
    "SPY": {"spot": 762.60, "risk_raw": 0.9935, "risk_smooth": 0.9938,
            "r0.2": 451.22, "r0.5": 550.25, "r0.8": 671.00, "r0.9": 716.88},
    "QQQ": {"spot": 716.92, "risk_raw": 0.9203, "risk_smooth": 0.9340,
            "r0.2": 331.46, "r0.5": 457.06, "r0.8": 630.25, "r0.9": 701.50},
    "IWM": {"spot": 285.43, "risk_raw": 0.8906, "risk_smooth": 0.9545,
            "r0.2": 195.17, "r0.5": 230.21, "r0.8": 271.54, "r0.9": 286.91},
    "composite": 0.9698,
    "score": 97,
}

# Handoff latest_state (2026-08-14). Its p@r0.5 and smooth are the re-anchor point for the
# lineage seed. Comparison only; never forced.
REFERENCE_0814 = {
    "SPY": {"spot": 776.34, "risk_raw": 1.000, "risk_smooth": 0.992, "delta_4w": 0.001,
            "r0.5": 542.30, "r0.9": 707.20, "r0.8": 661.78},
    "QQQ": {"spot": 731.07, "risk_raw": 0.957, "risk_smooth": 0.942, "delta_4w": -0.008,
            "r0.5": 449.35, "r0.9": 687.82, "r0.8": 618.38},
    "IWM": {"spot": 305.09, "risk_raw": 1.000, "risk_smooth": 0.962, "delta_4w": 0.015,
            "r0.5": 228.17, "r0.9": 283.97, "r0.8": 268.86},
    "composite": 0.972,
}

# Handoff latest_state.key_metrics.VIX (2026-08-14), reported as a full-history recomputation.
VIX_REF_0814 = {"spot": 14.25, "risk_raw": 0.186, "risk_smooth": 0.385, "delta_4w": -0.035,
                "r0.2": 14.44, "r0.5": 19.17, "r0.8": 25.47, "r0.9": 27.99}
# Published to 3 dp (risk) / 2 dp (prices). Risk: +-0.0006 (rounding + float); prices: 0.05%
# relative (the published r0.8 25.47 vs recomputed 25.4627 is -0.029%).
VIX_TOL = {"risk": 0.0006, "price_rel": 0.0005, "spot_abs": 0.005}

# ---- Documented validation tolerances against the 2026-09-17 agent of record -------------
STRICT_TOL = {"risk": 0.003, "price_rel": 0.005}
LINEAGE_TOL = {
    "spot_rel": 0.00005,     # closes are the same data; must match to the cent
    "risk_raw": 0.010,       # observed SPY +0.0065, QQQ +0.0059
    "risk_smooth": 0.005,    # observed SPY +0.0011, QQQ +0.0030, IWM +0.0017
    "price_rel": 0.010,      # observed SPY -0.64%, QQQ -0.63%, IWM -0.05%
    "composite": 0.005,      # observed +0.0019 vs published 0.9698
    "score": 0,              # SCORE_0_100 must be identical
}
DOCUMENTED_GAP = {
    "price": {"SPY": (-0.0064, 0.0015), "QQQ": (-0.0063, 0.0015), "IWM": (-0.0005, 0.0015)},
    "risk_raw": {"SPY": (+0.0065, 0.0020), "QQQ": (+0.0059, 0.0020), "IWM": (+0.0009, 0.0020)},
    "risk_smooth": {"SPY": (+0.0011, 0.0010), "QQQ": (+0.0030, 0.0010),
                    "IWM": (+0.0017, 0.0010)},
    "composite": (+0.0019, 0.0010),
}
# Fields the known trend-state lineage gap is expected to push outside STRICT_TOL.
KNOWN_STRICT_GAP_FIELDS = {f"{s}_{k}" for s in ("SPY", "QQQ")
                           for k in ("risk_raw", "r0.2", "r0.5", "r0.8", "r0.9")}
REANCHOR_TOL = {"composite": 0.00005, "risk": 0.001, "price_rel": 0.001, "score": 0}

ETF_METHOD = ("Fixed-band continuation: uploaded-history bands fixed at 2025-12-26 fixtures; "
              "trend/smooth seeded 2025-12-26 (clean fixture seed); raw split-adjusted "
              "Robinhood closes after seed")

# ---- Dividend sensitivity inputs (APPROXIMATE, assumed, not verified; sensitivity only) --
DIV_EVENTS = {
    "SPY": [("2026-03-20", 1.75), ("2026-06-18", 1.85), ("2026-09-18", 1.90)],
    "QQQ": [("2026-03-23", 0.72), ("2026-06-22", 0.74), ("2026-09-21", 0.76)],
    "IWM": [("2026-03-18", 0.55), ("2026-06-09", 0.75), ("2026-09-16", 0.75)],
}
DIV_YIELD = {"SPY": 0.012, "QQQ": 0.005, "IWM": 0.010}  # continuous-accrual approximation

CLOSE_HOUR_CT = 15  # 15:00 America/Chicago regular close (early-close days treated as 15:00)

# Hard-coded published NYSE full-day closures used to pin the algorithmic calendar.
NYSE_HOLIDAYS_2025 = {"2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
                      "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
                      "2025-12-25"}
NYSE_HOLIDAYS_2026 = {"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
                      "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"}
NYSE_SPECIAL_CLOSURES = {"2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14", "2004-06-11",
                         "2007-01-02", "2012-10-29", "2012-10-30", "2018-12-05", "2025-01-09"}


def alpha(h: float) -> float:
    return 1.0 - math.exp(math.log(0.5) / h)


# ---------------------------------------------------------------------------------------
# NYSE session calendar (1999 onward is what this module needs)
# ---------------------------------------------------------------------------------------

def _easter(y: int) -> dt.date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return dt.date(y, month, day)


def _nth_weekday(y: int, m: int, wd: int, n: int) -> dt.date:
    d = dt.date(y, m, 1)
    d += dt.timedelta(days=(wd - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(y: int, m: int, wd: int) -> dt.date:
    d = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - wd) % 7)


def _observed(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=1) if d.weekday() == 5 else \
        d + dt.timedelta(days=1) if d.weekday() == 6 else d


_HOL_CACHE: dict[int, set[str]] = {}


def nyse_holidays(y: int) -> set[str]:
    """NYSE full-day closures for year y (weekday dates only)."""
    if y in _HOL_CACHE:
        return _HOL_CACHE[y]
    h = []
    ny = dt.date(y, 1, 1)
    if ny.weekday() == 6:
        h.append(dt.date(y, 1, 2))
    elif ny.weekday() < 5:
        h.append(ny)  # Saturday New Year: NYSE does not observe on the Friday before
    if y >= 1998:
        h.append(_nth_weekday(y, 1, 0, 3))  # MLK
    h.append(_nth_weekday(y, 2, 0, 3))      # Washington's Birthday
    h.append(_easter(y) - dt.timedelta(days=2))  # Good Friday
    h.append(_last_weekday(y, 5, 0))        # Memorial Day
    if y >= 2022:
        h.append(_observed(dt.date(y, 6, 19)))  # Juneteenth
    h.append(_observed(dt.date(y, 7, 4)))
    h.append(_nth_weekday(y, 9, 0, 1))      # Labor Day
    h.append(_nth_weekday(y, 11, 3, 4))     # Thanksgiving
    h.append(_observed(dt.date(y, 12, 25)))
    out = {d.isoformat() for d in h if d.year == y and d.weekday() < 5}
    out |= {d for d in NYSE_SPECIAL_CLOSURES if d.startswith(f"{y}-")}
    _HOL_CACHE[y] = out
    return out


def is_session(d: dt.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in nyse_holidays(d.year)


def sessions_between(a: str, b: str) -> list[str]:
    d, end, out = dt.date.fromisoformat(a), dt.date.fromisoformat(b), []
    while d <= end:
        if is_session(d):
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def calendar_gaps(dates: list[str]) -> dict:
    """Missing NYSE sessions and non-session rows between the first and last date."""
    if not dates:
        return {"missing": [], "extra": []}
    have = set(dates)
    exp = sessions_between(dates[0], dates[-1])
    exps = set(exp)
    return {"missing": [d for d in exp if d not in have],
            "extra": [d for d in dates if d not in exps]}


# ---------------------------------------------------------------------------------------
# Loading and row guards
# ---------------------------------------------------------------------------------------

def _num(x: str | None) -> float | None:
    x = (x or "").strip()
    if x in ("", ".", "NaN", "nan", "null", "None"):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def incomplete_reason(row: dict) -> str | None:
    """Why a bar looks incomplete or synthesized, or None if it looks like a real close."""
    lo, hi, c = row.get("low"), row.get("high"), row.get("close")
    if c is None:
        return "missing close"
    if lo is None or hi is None:
        return "missing low/high (close-only bar)"
    if lo == hi == c:
        return "low==high==close (looks interpolated/synthesized)"
    if not (lo <= c <= hi):
        return "close outside low/high range"
    return None


def load_rows(path: str) -> list[dict]:
    rows: dict[str, dict] = {}
    try:
        fh = open(path, newline="")
    except OSError as e:
        raise Refuse(f"cannot read {path}: {e}")
    with fh:
        for rec in csv.DictReader(fh):
            d = rec["date"].strip()
            if d in rows:
                raise Refuse(f"{path}: duplicate date {d}")
            rows[d] = {"date": d, "low": _num(rec.get("low")), "high": _num(rec.get("high")),
                       "close": _num(rec.get("close"))}
    return [rows[d] for d in sorted(rows)]


def apply_intraday(sym: str, rows: list[dict], now_ct: dt.datetime) -> tuple[list[dict], list]:
    """Spec incomplete_row_rule: exclude a current-day row before the America/Chicago close;
    refuse any row dated after the current America/Chicago date."""
    today = now_ct.date().isoformat()
    fut = [r["date"] for r in rows if r["date"] > today]
    if fut:
        raise Refuse(f"{sym} has row(s) dated after the America/Chicago date {today}: "
                     f"{', '.join(fut)}")
    if rows and rows[-1]["date"] == today and now_ct.hour < CLOSE_HOUR_CT:
        return rows[:-1], [{"sym": sym, "date": today,
                            "why": f"current-day row before {CLOSE_HOUR_CT}:00 CT close "
                                   f"(clock {now_ct:%Y-%m-%d %H:%M} CT)"}]
    return rows, []


def guard_final(sym: str, rows: list[dict], drop: bool) -> tuple[list[dict], list[dict]]:
    """Incomplete-final-row rule. With drop=True, drop and RE-CHECK the new final row until
    it is clean; refuse if more than MAX_DROP_FINAL rows would go or nothing is left."""
    issues: list[dict] = []
    while True:
        if not rows:
            raise Refuse(f"{sym}: no complete rows left after dropping "
                         f"{len(issues)} incomplete final row(s)")
        why = incomplete_reason(rows[-1])
        if why is None:
            return rows, issues
        if not drop:
            raise Refuse(f"{sym} final row {rows[-1]['date']} is incomplete ({why}). Refusing a "
                         "close-based report; fix the cache or rerun with "
                         "--drop-incomplete-final to exclude and flag it.")
        if len(issues) >= MAX_DROP_FINAL:
            raise Refuse(f"{sym}: more than {MAX_DROP_FINAL} consecutive incomplete final rows "
                         f"(latest {rows[-1]['date']}: {why}); refusing to keep dropping")
        issues.append({"sym": sym, "date": rows[-1]["date"], "why": why})
        rows = rows[:-1]


def interior_flags(sym: str, rows: list[dict]) -> list[dict]:
    return [{"sym": sym, "date": r["date"], "why": w} for r in rows[:-1]
            if (w := incomplete_reason(r)) is not None]


def load_fred(path: str | None, col: str | None = None) -> dict[str, float] | None:
    """FRED fredgraph CSV. col=None uses the second column; else the named column."""
    if not path:
        return None
    out: dict[str, float] = {}
    try:
        fh = open(path, newline="")
    except OSError as e:
        raise Refuse(f"cannot read {path}: {e}")
    with fh:
        rd = csv.reader(fh)
        hdr = next(rd)
        j = 1 if col is None else (hdr.index(col) if col in hdr else None)
        if j is None:
            return {}
        for rec in rd:
            if len(rec) > j and (v := _num(rec[j])) is not None and v > 0:
                out[rec[0].strip()] = v
    return out


# ---------------------------------------------------------------------------------------
# ETF fixed-band engine
# ---------------------------------------------------------------------------------------

def _state(date, price, trend, raw, smooth, u, dl, width):
    lv = {r: math.exp(trend + dl + r * width) for r in R_LEVELS}
    return {"date": date, "spot": price, "trend": trend, "u": u, "risk_raw": raw,
            "risk_smooth": smooth, "levels": lv,
            "vs": {r: price / lv[r] - 1.0 for r in R_LEVELS}}


def run_engine(sym: str, series: list[tuple[str, float]], seed: str = "clean",
               log_adj=None) -> list[dict]:
    """Per-session states starting with the seed row.

    seed='clean'    : 2025-12-26 fixture seed (canonical).
    seed='reanchor' : 2026-08-14 published p@r0.5/risk_smooth (lineage sensitivity).
    log_adj(date, n): optional additive log adjustment to each post-seed close (dividends).
    """
    fx = FIXTURES[sym]
    dl, dh = fx["dev_low"], fx["dev_high"]
    width = dh - dl
    closes = dict(series)
    if seed == "clean":
        sd, mid, smooth = SEED_DATE, fx["price_at_r0_5"], fx["risk_smooth"]
    elif seed == "reanchor":
        sd, mid, smooth = REANCHOR_DATE, REFERENCE_0814[sym]["r0.5"], \
            REFERENCE_0814[sym]["risk_smooth"]
    else:
        raise ValueError(seed)
    if sd not in closes:
        raise Refuse(f"{sym}: cache lacks seed date {sd}")
    trend = math.log(mid) - dl - 0.5 * width
    if log_adj is not None:
        trend += log_adj(sd, 0)
    a_t, a_s = alpha(H_TREND), alpha(H_SMOOTH)
    p0 = closes[sd]
    lp0 = math.log(p0) + (log_adj(sd, 0) if log_adj else 0.0)
    u0 = (lp0 - trend - dl) / width
    raw0 = fx["risk_raw"] if seed == "clean" else min(1.0, max(0.0, u0))
    hist = [_state(sd, p0, trend, raw0, smooth, u0, dl, width)]
    n = 0
    for d, p in series:
        if d <= sd:
            continue
        n += 1
        lp = math.log(p) + (log_adj(d, n) if log_adj else 0.0)
        trend = a_t * lp + (1.0 - a_t) * trend
        u = (lp - trend - dl) / width
        raw = min(1.0, max(0.0, u))
        smooth = a_s * raw + (1.0 - a_s) * smooth
        st = _state(d, p, trend, raw, smooth, u, dl, width)
        if log_adj is not None:  # express levels in nominal (unadjusted) price terms
            k = math.exp(-log_adj(d, n))
            st["levels"] = {r: v * k for r, v in st["levels"].items()}
            st["vs"] = {r: p / st["levels"][r] - 1.0 for r in R_LEVELS}
        hist.append(st)
    for i, s in enumerate(hist):
        s["delta_4w"] = (s["risk_smooth"] - hist[i - DELTA_SESSIONS]["risk_smooth"]
                         if i >= DELTA_SESSIONS else None)
    return hist


def at(hist: list[dict], date: str) -> dict:
    for s in hist:
        if s["date"] == date:
            return s
    raise Refuse(f"{date} is not a cached session")


def composite(states: dict[str, dict]) -> float:
    return sum(WEIGHTS[k] * states[k]["risk_smooth"] for k in WEIGHTS)


def score(comp: float) -> int:
    return int(math.floor(100.0 * comp + 0.5))


# ---------------------------------------------------------------------------------------
# Full-sample engine (spec formulas verbatim): VIX full history, SPX/NDX modern window
# ---------------------------------------------------------------------------------------

def quantile_linear(xs: list[float], p: float) -> float:
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def full_engine(series: list[tuple[str, float]], asof: str, start: str | None = None) -> dict:
    """Spec Risk Engine on the full as-of sample: EMA(logP, 504 obs, adjust=False, init x0),
    dev quantiles 5/95 (linear) over every as-of observation, risk_raw clamp, EMA(raw, 28 obs).
    Returns the state at the last observation <= asof."""
    ser = [(d, v) for d, v in series if d <= asof and (start is None or d >= start)]
    if len(ser) <= DELTA_SESSIONS:
        raise Refuse(f"full-sample engine: only {len(ser)} observations <= {asof}")
    a_t, a_s = alpha(H_TREND), alpha(H_SMOOTH)
    tr, trs, devs = None, [], []
    for _, v in ser:
        lp = math.log(v)
        tr = lp if tr is None else a_t * lp + (1 - a_t) * tr
        trs.append(tr)
        devs.append(lp - tr)
    lo, hi = quantile_linear(devs, 0.05), quantile_linear(devs, 0.95)
    w = hi - lo
    sm, sms, raw = None, [], 0.0
    for (_, v), t in zip(ser, trs):
        u = (math.log(v) - t - lo) / w
        raw = min(1.0, max(0.0, u))
        sm = raw if sm is None else a_s * raw + (1 - a_s) * sm
        sms.append(sm)
    d, p = ser[-1]
    lv = {r: math.exp(trs[-1] + lo + r * w) for r in R_LEVELS}
    return {"date": d, "spot": p, "risk_raw": raw, "risk_smooth": sms[-1],
            "delta_4w": sms[-1] - sms[-1 - DELTA_SESSIONS], "levels": lv,
            "vs": {r: p / lv[r] - 1.0 for r in R_LEVELS}, "dev_low": lo, "dev_high": hi,
            "n": len(ser), "first": ser[0][0]}


def vix_row(fred: dict[str, float] | None, asof: str) -> dict:
    if not fred:
        return {"available": False, "reason": "FRED VIXCLS not supplied (--fred-vix)"}
    first = min(fred)
    if first > VIX_FULL_START_BY:
        return {"available": False,
                "reason": f"FRED VIXCLS file starts {first}; spec VIX_FULL_HISTORY needs 1990 "
                          "onward, so a full-history recomputation is not possible"}
    ser = sorted(fred.items())
    cur = full_engine(ser, asof)
    prior = full_engine(ser, VALIDATION_DATE) if VALIDATION_DATE in fred else None
    ref = full_engine(ser, REANCHOR_DATE) if REANCHOR_DATE in fred else None
    return {"available": True, "st": cur, "prior": prior, "ref": ref,
            "val": vix_validate(ref) if ref else None,
            "method": f"Full-history recomputation, FRED VIXCLS {first}..{cur['date']} "
                      f"({cur['n']} obs): 504-obs EMA, full-sample linear 5/95 quantiles, "
                      "28-obs smoothing; context only, 0% weight"}


def vix_validate(st: dict) -> dict:
    rows, ok = [], True
    for k, pub in VIX_REF_0814.items():
        if k == "spot":
            comp, diff, lim = st["spot"], st["spot"] - pub, VIX_TOL["spot_abs"]
            txt = f"{diff:+.4f}"
        elif k.startswith("r0."):
            comp = st["levels"][float(k[1:])]
            diff, lim = comp / pub - 1.0, VIX_TOL["price_rel"]
            txt = f"{100 * diff:+.3f}%"
        else:
            comp, lim = st[k], VIX_TOL["risk"]
            diff = comp - pub
            txt = f"{diff:+.4f}"
        good = abs(diff) <= lim
        ok &= good
        rows.append({"field": f"VIX_{k}", "published": pub, "computed": comp, "diff": txt,
                     "tol": lim, "ok": good})
    return {"ok": ok, "rows": rows}


def modern_row(sym: str, fred_col: dict[str, float] | None, rh_first: str, asof: str,
               col: str) -> dict:
    start = MODERN_START[sym]
    win = win_name = f"{sym}_MODERN_1999"
    if fred_col is None:
        return {"available": False,
                "reason": f"{win} needs daily {sym} closes from {start}; --fred-idx (FRED "
                          f"{col}) not supplied, Robinhood {sym} cache starts {rh_first}, Stooq "
                          "history not cached (no network); not derived or invented"}
    if not fred_col:
        return {"available": False,
                "reason": f"{win} needs daily {sym} closes from {start}; --fred-idx has no "
                          f"{col} data; Robinhood {sym} cache starts {rh_first}; Stooq history "
                          "not cached (no network); not derived or invented"}
    first = min(fred_col)
    n = len(fred_col)
    if first > start:
        return {"available": False,
                "reason": f"{win} needs daily {sym} closes from {start}; supplied FRED {col} "
                          f"starts {first} ({n} obs), Robinhood {sym} cache starts {rh_first}, "
                          "Stooq history not cached (no network); not derived or invented"}
    win = {d: v for d, v in fred_col.items() if start <= d <= asof}
    gaps = calendar_gaps(sorted(win))
    # Spec: one observation per trading session. FRED carries stale values on some NYSE
    # holidays (e.g. NASDAQ100 2001-01-01, 2019-04-19); those non-session rows are excluded.
    ser = sorted((d, v) for d, v in win.items() if d not in set(gaps["extra"]))
    st = full_engine(ser, asof, start)
    return {"available": True, "st": st, "gaps": gaps,
            "method": f"{win_name}: full recomputation on FRED {col} {start}..{st['date']} "
                      f"({st['n']} session obs), price index (no dividends), fresh full-sample "
                      "bands; window-aligned to the ETF era for comparison only, NOT the "
                      "fixed-band ETF calibration; 0% weight"}


# ---------------------------------------------------------------------------------------
# Dividend sensitivity
# ---------------------------------------------------------------------------------------

def _div_discrete(sym: str, series: list[tuple[str, float]], asof: str):
    """Forward total-return adjustment from assumed discrete ex-dates (<= asof)."""
    dates = [d for d, _ in series]
    closes = dict(series)
    cum: list[tuple[str, float]] = []
    for ex, amt in DIV_EVENTS[sym]:
        if SEED_DATE < ex <= asof:
            prev = max(d for d in dates if d < ex)
            cum.append((ex, -math.log(1.0 - amt / closes[prev])))

    def adj(d: str, n: int) -> float:
        return sum(v for ex, v in cum if d >= ex)
    return adj


def _div_accrual(sym: str):
    y = DIV_YIELD[sym]
    return lambda d, n: y * n / 252.0


def dividend_sensitivity(data: dict, asof: str, base: dict) -> dict:
    out = {}
    for name, mk in (("discrete", lambda s: _div_discrete(s, data[s], asof)),
                     ("accrual", lambda s: _div_accrual(s))):
        st = {s: at(run_engine(s, data[s], "clean", mk(s)), asof) for s in ETFS}
        c = composite(st)
        out[name] = {"composite": c, "score": score(c),
                     "r09": {s: st[s]["levels"][0.9] / base[s]["levels"][0.9] - 1.0
                             for s in ETFS}}
    comps = [v["composite"] for v in out.values()]
    spy = [v["r09"]["SPY"] for v in out.values()]
    qqq = [v["r09"]["QQQ"] for v in out.values()]
    return {"variants": out, "comp_lo": min(comps), "comp_hi": max(comps),
            "scores": sorted({v["score"] for v in out.values()}),
            "spy_r09_lo": min(spy), "spy_r09_hi": max(spy),
            "qqq_r09_lo": min(qqq), "qqq_r09_hi": max(qqq),
            "max_shift": max(abs(c - composite(base)) for c in comps)}


def _rng(lo: float, hi: float) -> str:
    """Signed % range ordered smaller-magnitude first, e.g. -0.67%..-0.86%."""
    a, b = sorted((lo, hi), key=abs)
    return f"{100 * a:+.2f}%..{100 * b:+.2f}%"


# ---------------------------------------------------------------------------------------
# Cross-checks: VIX bars, index closes, ETF/index ratio continuity, term structure, staleness
# ---------------------------------------------------------------------------------------

def vix_crosscheck(rh: list[tuple[str, float]], fred: dict[str, float] | None,
                   asof: str, sessions: list[str]) -> dict:
    """Compare Robinhood VIX bars with FRED VIXCLS. A bar whose value equals FRED's NEXT
    date's close (and not its own date's) is misdated. 'missing' counts only equity sessions."""
    if fred is None:
        return {"available": False}
    rhd = dict(rh)
    fd = sorted(d for d in fred if rhd and min(rhd) <= d <= asof)
    agree, misdated, mismatched = 0, [], []
    for d, v in rh:
        if d > asof or d not in fred:
            continue
        if abs(v - fred[d]) < 0.005:
            agree += 1
            continue
        nxt = [x for x in fd if x > d]
        if nxt and abs(v - fred[nxt[0]]) < 0.005:
            misdated.append((d, v, nxt[0], fred[d]))
        else:
            mismatched.append((d, v, fred[d]))
    eq = set(sessions)
    missing = [d for d in fd if d not in rhd and d in eq]
    return {"available": True, "agree": agree, "misdated": misdated,
            "mismatched": mismatched, "missing": missing}


def index_crosscheck(rh: list[tuple[str, float]], fred_col: dict[str, float] | None,
                     asof: str) -> dict:
    if not fred_col:
        return {"available": False}
    common = [(d, v) for d, v in rh if d <= asof and d in fred_col]
    bad = [(d, v, fred_col[d]) for d, v in common if abs(v / fred_col[d] - 1) > 1e-5]
    return {"available": bool(common), "n": len(common), "bad": bad,
            "first": common[0][0] if common else None, "last": common[-1][0] if common else None}


def ratio_continuity(data: dict, asof: str, start: str = VALIDATION_DATE) -> dict:
    """Post-start ETF/index ratio continuity: |ratio_t/ratio_prev - 1| <= RATIO_JUMP_TOL on every
    session, and <= EXDIV_JUMP_TOL on the (assumed) ex-dividend dates, which are reported. A session
    with no index bar is listed as unchecked and the next session is compared across the gap, so a
    break cannot hide behind a missing bar."""
    out = {}
    for etf, ix in (("SPY", "SPX"), ("QQQ", "NDX")):
        e, i = dict(data[etf]), dict(data[ix])
        exd = {ex for ex, _ in DIV_EVENTS[etf]}
        jumps, exempt, unchecked, mx, prev, n = [], [], [], 0.0, None, 0
        for d in [d for d, _ in data[etf] if start <= d <= asof]:
            if d not in i:
                unchecked.append(d)
                continue
            r = e[d] / i[d]
            if prev is not None:
                ch = r / prev - 1.0
                n += 1
                if d in exd:
                    exempt.append((d, ch))
                    if abs(ch) > EXDIV_JUMP_TOL:
                        jumps.append((d, ch))
                else:
                    mx = max(mx, abs(ch))
                    if abs(ch) > RATIO_JUMP_TOL:
                        jumps.append((d, ch))
            prev = r
        out[etf] = {"index": ix, "n": n, "max": mx, "jumps": jumps, "exempt": exempt,
                    "unchecked": unchecked}
    out["ok"] = all(not out[k]["jumps"] and not out[k]["unchecked"] for k in ("SPY", "QQQ"))
    return out


def term_structure(vix: dict[str, float] | None, vix3m: dict[str, float] | None,
                   asof: str) -> dict:
    if not vix or not vix3m:
        return {"available": False, "reason": "FRED VIXCLS/VXVCLS not supplied"}
    common = [d for d in vix if d in vix3m and d <= asof]
    if not common:
        return {"available": False, "reason": "no common VIXCLS/VXVCLS date <= as-of"}
    d = max(common)
    prior = VALIDATION_DATE if VALIDATION_DATE in vix and VALIDATION_DATE in vix3m else None
    return {"available": True, "date": d, "vix": vix[d], "vix3m": vix3m[d],
            "ratio": vix[d] / vix3m[d],
            "prior_ratio": vix[prior] / vix3m[prior] if prior else None}


def chicago_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)
    except Exception:  # tzdata missing: fixed CDT offset approximation
        return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=5)


def missing_sessions(asof: str, now_ct: dt.datetime) -> list[str]:
    """Completed sessions after asof up to the last completed close (America/Chicago)."""
    last = now_ct.date()
    if not (is_session(last) and now_ct.hour >= CLOSE_HOUR_CT):
        last -= dt.timedelta(days=1)
        while not is_session(last):
            last -= dt.timedelta(days=1)
    d, out = dt.date.fromisoformat(asof) + dt.timedelta(days=1), []
    while d <= last:
        if is_session(d):
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------------------
# Build and validation
# ---------------------------------------------------------------------------------------

def build(cache: str, drop_incomplete: bool = False, fred_vix: str | None = None,
          fred_vix3m: str | None = None, now_ct: dt.datetime | None = None,
          allow_gaps: bool = False, fred_idx: str | None = None) -> dict:
    now_ct = now_ct or chicago_now()
    data, issues, flags, intraday, gaps = {}, [], [], [], {}
    for s in SERIES:
        rows = load_rows(os.path.join(cache, f"{s}.csv"))
        rows, intr = apply_intraday(s, rows, now_ct)
        intraday += intr
        rows, iss = guard_final(s, rows, drop_incomplete)
        issues += iss
        for r in rows:
            if r["close"] is None or r["close"] <= 0:
                raise Refuse(f"{s}: missing/non-positive close on row {r['date']}")
        flags += interior_flags(s, rows)
        data[s] = [(r["date"], r["close"]) for r in rows]
        gaps[s] = calendar_gaps([d for d, _ in data[s]])
    etf_gaps = {s: gaps[s] for s in ETFS if gaps[s]["missing"] or gaps[s]["extra"]}
    if etf_gaps and not allow_gaps:
        raise Refuse("interior session gap(s) vs the NYSE calendar: " + "; ".join(
            f"{s} missing {len(g['missing'])} ({', '.join(g['missing'][:5])}"
            f"{', ...' if len(g['missing']) > 5 else ''})"
            + (f", non-session rows {', '.join(g['extra'])}" if g["extra"] else "")
            for s, g in etf_gaps.items())
            + ". An EMA must not skip sessions; supply the complete bridge or rerun with "
              "--allow-session-gaps to proceed with disclosure and a confidence deduction.")
    dates = {s: [d for d, _ in data[s]] for s in ETFS}
    if not (dates["SPY"] == dates["QQQ"] == dates["IWM"]):
        raise Refuse("SPY/QQQ/IWM date sets differ")
    eng = {s: run_engine(s, data[s]) for s in ETFS}
    fi = {"path": fred_idx}
    if fred_idx:
        fi["SPX"] = load_fred(fred_idx, "SP500")
        fi["NDX"] = load_fred(fred_idx, "NASDAQ100")
    return {"data": data, "eng": eng, "asof": dates["SPY"][-1], "issues": issues,
            "flags": flags, "intraday": intraday, "gaps": gaps, "etf_gaps": etf_gaps,
            "allow_gaps": allow_gaps, "now_ct": now_ct, "fred_idx": fi,
            "fred_vix": load_fred(fred_vix), "fred_vix3m": load_fred(fred_vix3m)}


def compare(st: dict, ref: dict, tol: dict) -> tuple[list[dict], bool]:
    """Field-by-field comparison of engine states with a published reference."""
    rows, ok = [], True
    for sym in ETFS:
        for k, pub in ref[sym].items():
            s = st[sym]
            if k in ("risk_raw", "risk_smooth", "delta_4w"):
                comp, diff = s[k], s[k] - pub
                lim = tol.get(k, tol.get("risk"))
                dtxt, ctxt = f"{diff:+.4f}", f"{comp:.4f}"
            else:
                comp = s["spot"] if k == "spot" else s["levels"][float(k[1:])]
                diff = comp / pub - 1.0
                lim = tol.get("spot_rel", tol["price_rel"]) if k == "spot" else tol["price_rel"]
                dtxt, ctxt = f"{100 * diff:+.3f}%", f"{comp:.2f}"
            good = abs(diff) <= lim
            rows.append({"field": f"{sym}_{k}", "published": str(pub), "computed": ctxt,
                         "diff": dtxt, "within_tol": good, "tol": lim, "d": diff,
                         "sym": sym, "key": k})
            ok &= good
    c = composite(st)
    good = abs(c - ref["composite"]) <= tol["composite"]
    rows.append({"field": "ETF_composite", "published": str(ref["composite"]),
                 "computed": f"{c:.6f}", "diff": f"{c - ref['composite']:+.6f}",
                 "within_tol": good, "tol": tol["composite"], "d": c - ref["composite"],
                 "sym": None, "key": "composite"})
    ok &= good
    if "score" in ref:
        sc = score(c)
        good = abs(sc - ref["score"]) <= tol["score"]
        rows.append({"field": "SCORE_0_100", "published": str(ref["score"]),
                     "computed": str(sc), "diff": f"{sc - ref['score']:+d}",
                     "within_tol": good, "tol": tol["score"], "d": sc - ref["score"],
                     "sym": None, "key": "score"})
        ok &= good
    return rows, ok


def gap_rows(rows: list[dict]) -> tuple[list[dict], bool]:
    """Check clean-seed diffs against DOCUMENTED_GAP (centre +- band)."""
    out, ok = [], True
    for r in rows:
        k = r["key"]
        if k == "composite":
            c, h = DOCUMENTED_GAP["composite"]
        elif k in ("risk_raw", "risk_smooth"):
            c, h = DOCUMENTED_GAP[k][r["sym"]]
        elif k.startswith("r0."):
            c, h = DOCUMENTED_GAP["price"][r["sym"]]
        else:
            continue
        good = abs(r["d"] - c) <= h
        out.append({**r, "centre": c, "band": h, "within_gap": good})
        ok &= good
    return out, ok


def validation(b: dict) -> dict:
    clean = {s: at(b["eng"][s], VALIDATION_DATE) for s in ETFS}
    re = {s: at(run_engine(s, b["data"][s], "reanchor"), VALIDATION_DATE) for s in ETFS}
    ref_sq = {k: VALIDATION_0917[k] for k in ("SPY", "QQQ", "composite", "score")}
    ref_sq["IWM"] = {}
    lin_rows, lin_tol_ok = compare(clean, VALIDATION_0917, LINEAGE_TOL)
    g_rows, g_ok = gap_rows(lin_rows)
    strict_tol = {"risk": STRICT_TOL["risk"], "price_rel": STRICT_TOL["price_rel"],
                  "spot_rel": LINEAGE_TOL["spot_rel"], "composite": STRICT_TOL["risk"],
                  "score": 0}
    strict_rows, strict_ok = compare(clean, VALIDATION_0917, strict_tol)
    re_rows, re_ok = compare(re, ref_sq, {**REANCHOR_TOL, "spot_rel": LINEAGE_TOL["spot_rel"]})
    gaps = [r for r in strict_rows if not r["within_tol"]]
    failed = ([f"{r['field']} {r['diff']} (tol {r['tol']})" for r in lin_rows
               if not r["within_tol"]]
              + [f"{r['field']} gap {r['diff']} outside documented {r['centre']:+.4f}+-"
                 f"{r['band']:.4f}" for r in g_rows if not r["within_gap"]]
              + [f"re-anchored {r['field']} {r['diff']}" for r in re_rows
                 if not r["within_tol"]])
    lin_ok = lin_tol_ok and g_ok
    return {"clean": clean, "reanchor": re, "lin_rows": lin_rows, "lin_ok": lin_ok,
            "gap_rows": g_rows, "strict_ok": strict_ok, "strict_gaps": gaps,
            "unexpected_strict": [r["field"] for r in gaps
                                  if r["field"] not in KNOWN_STRICT_GAP_FIELDS],
            "re_rows": re_rows, "re_ok": re_ok, "ok": lin_ok and re_ok, "failed": failed,
            "c_clean": composite(clean), "c_re": composite(re)}


# ---------------------------------------------------------------------------------------
# Confidence (computed from run state; data quality and signal clarity, not prediction)
# ---------------------------------------------------------------------------------------

def confidence(rs: dict) -> tuple[int, list[tuple[int, str]]]:
    d: list[tuple[int, str]] = []
    # Structural to this engine: the uploaded full ETF histories are not an input.
    d.append((6, f"ETF dev bands fixed at {SEED_DATE} uploaded-history calibration, not "
                 "recomputed (uploaded histories not supplied)"))
    dv = rs["div"]
    adj = min(8, 2 + math.ceil(1000 * dv["max_shift"] - 1e-9))
    d.append((adj, f"raw (non-dividend-adjusted) closes on an adjusted base: composite "
                   f"{dv['comp_lo']:.4f}-{dv['comp_hi']:.4f} if adjusted (assumed dividends), "
                   f"SPY r0.9 {_rng(dv['spy_r09_lo'], dv['spy_r09_hi'])}"))
    ix, rc = rs["idxx"], rs["ratio"]
    idx_ok = all(ix[s]["available"] and not ix[s]["bad"] for s in ("SPX", "NDX"))
    if idx_ok and rc["ok"]:
        d.append((1, "ETF closes single-provider (Robinhood, no independent ETF close source); "
                     "SPX/NDX closes agree with FRED and post-"
                     f"{VALIDATION_DATE} SPY/SPX, QQQ/NDX ratios are continuous"))
    else:
        why = []
        for s in ("SPX", "NDX"):
            if not ix[s]["available"]:
                why.append(f"{s} closes not cross-checked (no FRED data)")
            elif ix[s]["bad"]:
                why.append(f"{s} {len(ix[s]['bad'])} close(s) disagree with FRED")
        if not rc["ok"]:
            why.append("ETF/index ratio continuity breaks")
        d.append((2, "ETF/index closes single-provider (Robinhood): " + "; ".join(why)))
    vx = rs["vixx"]
    if not vx["available"]:
        d.append((1, "VIX bars not cross-checked against FRED VIXCLS"))
    vr = rs["vix"]
    if not vr["available"]:
        d.append((1, f"VIX risk row N/A ({vr['reason']})"))
    elif vr["val"] is None:
        d.append((1, f"VIX full-history engine not checkable against the {REANCHOR_DATE} "
                     "published VIX state (date absent from FRED file)"))
    elif not vr["val"]["ok"]:
        d.append((2, f"VIX full-history engine does NOT reproduce the {REANCHOR_DATE} published "
                     "VIX state: " + ", ".join(r["field"] for r in vr["val"]["rows"]
                                               if not r["ok"])))
    for s in ("SPX", "NDX"):
        m = rs["modern"][s]
        if not m["available"]:
            d.append((1, f"{s}_MODERN_1999 N/A ({m['reason'].split(';')[1].strip()})"))
        elif m["gaps"]["missing"]:
            d.append((1, f"{s}_MODERN_1999 FRED history misses {len(m['gaps']['missing'])} "
                         "NYSE session(s)"))
    if not rs["ts"]["available"]:
        d.append((1, f"VIX term structure N/A ({rs['ts']['reason']})"))
    ms = rs["missing_sessions"]
    if ms:
        d.append((min(6, 2 * len(ms)), f"cache stale by {len(ms)} completed session(s): "
                                        f"{', '.join(ms)}"))
    if rs["issues"]:
        d.append((len(rs["issues"]), "incomplete final row(s) excluded: " + ", ".join(
            f"{i['sym']} {i['date']}" for i in rs["issues"])))
    eg = rs["etf_gaps"]
    if eg:
        n = len({x for g in eg.values() for x in g["missing"] + g["extra"]})
        d.append((min(20, 2 + n), f"ETF interior session gaps ALLOWED by --allow-session-gaps: "
                                  f"{n} date(s); EMA continued across missing sessions"))
    js = [c for k in ("SPY", "QQQ") for _, c in rc[k]["jumps"]]
    if js:
        mx = max(abs(c) for c in js)
        d.append((min(15, 3 * len(js) + int(100 * mx)),
                  f"{len(js)} ETF/index ratio jump(s) >{100 * RATIO_JUMP_TOL:.1f}% on non-ex-div "
                  f"sessions after {VALIDATION_DATE} (max {100 * mx:.2f}%)"))
    unc = [d_ for k in ("SPY", "QQQ") for d_ in rc[k]["unchecked"]]
    if unc:
        d.append((1, f"ratio continuity unchecked on {len(unc)} session(s) (index bar missing)"))
    if not rs["seam_ok"]:
        d.append((5, f"seam mismatch: cached {SEED_DATE} closes give u " + ", ".join(
            f"{s}={rs['seam'][s]:.6f}" for s in ETFS if round(rs['seam'][s], 6) != SEAM_U[s])
            + " vs fixture " + ", ".join(f"{s}={SEAM_U[s]}" for s in ETFS
                                        if round(rs['seam'][s], 6) != SEAM_U[s])))
    v = rs["val"]
    if not v["ok"]:
        d.append((10, f"{VALIDATION_DATE} lineage validation FAILED: "
                      + "; ".join(v["failed"][:4])
                      + (f"; +{len(v['failed']) - 4} more" if len(v["failed"]) > 4 else "")))
    elif not v["strict_ok"]:
        if v["unexpected_strict"]:
            why = "includes field(s) outside the known gap: " + ", ".join(v["unexpected_strict"])
        else:
            why = "all within the documented, pinned trend-state lineage gap"
        d.append((3, f"{VALIDATION_DATE} agent-of-record match within lineage tolerance only: "
                     f"{len(v['strict_gaps'])} field(s) outside strict tolerance ({why})"))
    return 100 - sum(x for x, _ in d), d


# ---------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------

def _f(x, nd=4):
    return "N/A" if x is None else f"{x:.{nd}f}"


def _pct(x):
    return "N/A" if x is None else f"{100 * x:+.2f}%"


def validation_banner(rs: dict) -> str | None:
    v = rs["val"]
    if v["ok"]:
        return None
    rel = (" Released with --allow-unvalidated." if rs.get("allow_unvalidated")
           else " Not releasable (exit 3).")
    return (f"WARNING: VALIDATION FAILED - {VALIDATION_DATE} lineage check failed ("
            + "; ".join(v["failed"][:3]) + (", ..." if len(v["failed"]) > 3 else "")
            + "). Do not treat this score as validated." + rel)


def continuity_banner(rs: dict) -> str | None:
    rc = rs["ratio"]
    js = [(k, d, c) for k in ("SPY", "QQQ") for d, c in rc[k]["jumps"]]
    if not js:
        return None
    return ("WARNING: ETF/INDEX RATIO CONTINUITY FAILED - " + ", ".join(
        f"{k}/{rc[k]['index']} {d} {100 * c:+.2f}%" for k, d, c in js[:4])
        + (", ..." if len(js) > 4 else "") + "; check the ETF closes before using this score.")


def packet(rs: dict) -> str:
    st, idx, asof, comp, sc = rs["st"], rs["idx"], rs["asof"], rs["comp"], rs["score"]
    A = f"({asof} close)"
    S, Q, I = st["SPY"], st["QQQ"], st["IWM"]
    conf, _ = confidence(rs)
    dv, ts = rs["div"], rs["ts"]

    def dist(x, r):
        return 100 * (x["levels"][r] / x["spot"] - 1)

    def zone(x):
        return ("in the extreme zone (above r0.9)" if x["vs"][0.9] > 0 else
                "between r0.8 and r0.9" if x["vs"][0.8] > 0 else "below r0.8")

    nearest = min(("SPY", "QQQ"), key=lambda s: st[s]["vs"][0.9])
    iwm_dir = ("small-cap extension narrowing" if (I["delta_4w"] or 0) < -0.01 else
               "small-cap extension broadening" if (I["delta_4w"] or 0) > 0.01 else
               "small-cap extension steady")
    L = ["SUPERVISOR PACKET (COPY/PASTE)"]
    ban = validation_banner(rs)
    if ban:
        L.append(ban)
    cban = continuity_banner(rs)
    if cban:
        L.append(cban)
    L += ["MODULE: QuantRisk", f"ASOF: {asof} close", f"SCORE_0_100: {sc}",
          f"CONFIDENCE_0_100: {conf}", "TOP_3_DRIVERS:"]
    L.append(f"- SPY risk_smooth {S['risk_smooth']:.4f} (d4w {S['delta_4w']:+.4f}), risk_raw "
             f"{S['risk_raw']:.4f}; spot {S['spot']:.2f} is {100 * S['vs'][0.9]:+.2f}% vs r0.9 "
             f"{S['levels'][0.9]:.2f}, {zone(S)}; 60% of the score.")
    L.append(f"- QQQ risk_smooth {Q['risk_smooth']:.4f} (d4w {Q['delta_4w']:+.4f}), risk_raw "
             f"{Q['risk_raw']:.4f}; spot {Q['spot']:.2f} {100 * Q['vs'][0.9]:+.2f}% vs r0.9 "
             f"{Q['levels'][0.9]:.2f}, {zone(Q)}"
             + ("; nearest canonical asset to initial de-extension." if nearest == "QQQ"
                else "."))
    L.append(f"- IWM (diagnostic, 0% weight) risk_smooth {I['risk_smooth']:.4f} (d4w "
             f"{I['delta_4w']:+.4f}); spot {I['spot']:.2f} {100 * I['vs'][0.9]:+.2f}% vs r0.9 "
             f"{I['levels'][0.9]:.2f}, {100 * I['vs'][0.8]:+.2f}% vs r0.8 "
             f"{I['levels'][0.8]:.2f}; {iwm_dir}.")
    L.append("CHANGE_MY_MIND (TRIGGERS):")
    L.append(f"- Initial de-extension only (not cheap): SPY close below r0.9 {S['levels'][0.9]:.2f} "
             f"({dist(S, 0.9):+.2f}%; assumed dividend-adjustment offset "
             f"{_rng(dv['spy_r09_lo'], dv['spy_r09_hi'])} on that level) with SPY risk_smooth "
             f"<0.95, and QQQ below r0.9 {Q['levels'][0.9]:.2f} ({dist(Q, 0.9):+.2f}%) with QQQ "
             "risk_smooth <0.90.")
    L.append(f"- Toward 0.75x review: SPY near/below r0.8 {S['levels'][0.8]:.2f} ({dist(S, 0.8):+.2f}%) "
             f"or QQQ near/below r0.8 {Q['levels'][0.8]:.2f} ({dist(Q, 0.8):+.2f}%) with that "
             f"asset's risk_raw <=0.80, risk_smooth falling, composite below 0.95; credit orderly.")
    lead = (f"composite {comp:.4f} is already >=0.95" if comp >= 0.95
            else f"composite {comp:.4f} back at/above 0.95")
    L.append(f"- Toward 0.50x or below: {lead}; escalates if SPY and QQQ both hold above r0.9 "
             f"({S['levels'][0.9]:.2f}/{Q['levels'][0.9]:.2f}) with flat/rising risk_smooth, or "
             f"a VIX shock coincides with credit widening (Liquidity module).")
    L.append("KEY_METRICS:")
    L.append(f"- ETF_composite_risk={comp:.4f} {A}")
    for sym, x in (("SPY", S), ("QQQ", Q), ("IWM", I)):
        L.append(f"- {sym}_spot={x['spot']:.2f} {A}")
        L.append(f"- {sym}_risk_raw={x['risk_raw']:.4f} {A}")
        L.append(f"- {sym}_risk_smooth={x['risk_smooth']:.4f} {A}")
        for r in R_LEVELS:
            L.append(f"- {sym}_price_at_r{r}={x['levels'][r]:.2f} {A}")
    for sym in ("SPX", "NDX"):
        i, m = idx[sym], rs["modern"][sym]
        a = f"({i['date']} close)"
        L.append(f"- {sym}_spot={i['spot']:.2f} ({i['src']}) {a}")
        if m["available"]:
            x = m["st"]
            b = f"({x['date']} close)"
            L.append(f"- {sym}_risk_smooth_modern_window={x['risk_smooth']:.4f} (raw "
                     f"{x['risk_raw']:.4f}, d4w {x['delta_4w']:+.4f}; FRED from "
                     f"{MODERN_START[sym]}, fresh bands, not ETF calibration) {b}")
            for r in R_LEVELS:
                L.append(f"- {sym}_price_at_r{r}={x['levels'][r]:.2f} {b}")
        else:
            short = m["reason"].split(";")[1].strip()
            L.append(f"- {sym}_risk_smooth_modern_window=N/A (needs daily closes from "
                     f"{MODERN_START[sym]}; {short}) {a}")
            for r in R_LEVELS:
                L.append(f"- {sym}_price_at_r{r}=N/A {a}")
    i, vr = idx["VIX"], rs["vix"]
    a = f"({i['date']} close)"
    L.append(f"- VIX_spot={i['spot']:.2f} ({i['src']}) {a}")
    if vr["available"]:
        x = vr["st"]
        b = f"({x['date']} close)"
        L.append(f"- VIX_risk_raw={x['risk_raw']:.4f} {b}")
        L.append(f"- VIX_risk_smooth={x['risk_smooth']:.4f} (d4w {x['delta_4w']:+.4f}; full "
                 f"history FRED VIXCLS from {x['first']}; context only, 0% weight) {b}")
        for r in (0.2, 0.5, 0.8, 0.9):
            L.append(f"- VIX_price_at_r{r}={x['levels'][r]:.2f} {b}")
    else:
        L.append(f"- VIX_risk_smooth=N/A ({vr['reason']}) {a}")
        L.append(f"- VIX_price_at_r0.8=N/A {a}")
        L.append(f"- VIX_price_at_r0.9=N/A {a}")
    if ts["available"]:
        b = f"({ts['date']} close)"
        L.append(f"- VIX3M_spot={ts['vix3m']:.2f} (FRED VXVCLS) {b}")
        pr = (f"; {VALIDATION_DATE} {ts['prior_ratio']:.3f}" if ts["prior_ratio"] else "")
        L.append(f"- VIX_term_structure_VIX_over_VIX3M={ts['ratio']:.3f} (FRED VIXCLS/VXVCLS; "
                 f"{'<1 contango' if ts['ratio'] < 1 else '>=1 backwardation'}{pr}) {b}")
    else:
        L.append(f"- VIX_term_structure_VIX_over_VIX3M=N/A ({ts['reason']}) {A}")
    L.append("NOTES:")
    L += [f"- {n}" for n in notes(rs)]
    return "\n".join(L)


GOVERNANCE_NOTE = (
    "Supervisor self-run at Jason's request (2026-10-30: 'it would be much easier going forward if you could simply run them all yourself'; recorded in CLAUDE.md), replacing the external agent; spec supervisor/modules/"
    "quant_risk.yaml via tools/quant_risk.py, methodology unchanged.")


def notes(rs: dict) -> list[str]:
    asof, v, dv, ts, vx = rs["asof"], rs["val"], rs["div"], rs["ts"], rs["vixx"]
    out = [GOVERNANCE_NOTE]
    if not v["ok"]:
        out.append(f"VALIDATION FAILED vs {VALIDATION_DATE} lineage: " + "; ".join(v["failed"])
                   + (". Released only because --allow-unvalidated was given."
                      if rs.get("allow_unvalidated") else "."))
    out.append(f"Fixed bands: ETF dev_low/dev_high held at the uploaded-history {SEED_DATE} "
               "fixtures; not recomputed (uploaded histories not supplied to this run). Not a "
               "fresh full-history ETF recomputation.")
    out.append(f"Seed (canonical): clean {SEED_DATE} fixture seed; seam u " + ", ".join(
        f"{s}={rs['seam'][s]:.6f}" for s in ETFS) + " ("
        + ("matches fixtures to 6dp)." if rs["seam_ok"] else
           "DOES NOT match fixtures " + ", ".join(f"{s}={SEAM_U[s]}" for s in ETFS)
           + " to 6dp; cached seed-date close differs from the uploaded history; confidence "
             "deducted)."))
    sg = {r["field"]: r["diff"] for r in v["lin_rows"]}
    if v["lin_ok"]:
        gap = (f"SPY r-levels {sg['SPY_r0.9']}, QQQ {sg['QQQ_r0.9']}, SPY risk_raw "
               f"{sg['SPY_risk_raw']}, within the pinned trend-state lineage gap")
        verdict = "PASS"
    else:
        gap = "failing: " + "; ".join(f for f in v["failed"] if not f.startswith("re-anch"))
        verdict = "FAIL"
    out.append(
        f"Lineage vs {VALIDATION_DATE} agent of record (clean seed): composite "
        f"{v['c_clean']:.6f} vs published {VALIDATION_0917['composite']} "
        f"({v['c_clean'] - VALIDATION_0917['composite']:+.4f}), SCORE "
        f"{score(v['c_clean'])} vs {VALIDATION_0917['score']}; {gap} -> {verdict} "
        f"(composite +-{LINEAGE_TOL['composite']}, r-levels +-{100 * LINEAGE_TOL['price_rel']:.1f}%, "
        f"gap pinned +-{100 * DOCUMENTED_GAP['price']['SPY'][1]:.2f}%).")
    out.append(
        f"Re-anchored lineage engine ({REANCHOR_DATE} published state): {VALIDATION_DATE} "
        f"composite {v['c_re']:.6f} ({'reproduces' if v['re_ok'] else 'does NOT reproduce'} "
        f"published {VALIDATION_0917['composite']}); sensitivity only, not official.")
    cr = rs["chg"]
    out.append(
        f"Change since {VALIDATION_DATE} (same engine each, never across engines): clean seed "
        f"composite {cr['clean'][0]:.6f} -> {cr['clean'][1]:.6f} "
        f"({cr['clean'][1] - cr['clean'][0]:+.6f}), SCORE {score(cr['clean'][0])}->"
        f"{score(cr['clean'][1])}; re-anchored {cr['re'][0]:.6f} -> {cr['re'][1]:.6f} "
        f"({cr['re'][1] - cr['re'][0]:+.6f}), SCORE {score(cr['re'][0])}->{score(cr['re'][1])}"
        + (f"; VIX risk_smooth {cr['vix'][0]:.4f} -> {cr['vix'][1]:.4f} "
           f"({cr['vix'][1] - cr['vix'][0]:+.4f})" if cr["vix"] else "") + ".")
    out.append(
        f"Prices: raw split-adjusted, NOT dividend-adjusted ETF closes after {SEED_DATE} on an "
        "adjusted base (disclosed spec failure mode). Approx. dividend adjustment (assumed "
        f"amounts/yields) lifts composite to {dv['comp_lo']:.4f}-{dv['comp_hi']:.4f} "
        f"(+{dv['comp_lo'] - rs['comp']:.4f}..+{dv['comp_hi'] - rs['comp']:.4f}), SCORE "
        f"{'/'.join(map(str, dv['scores']))}.")
    out.append(
        f"Trigger-price offset from adjustedness (assumed dividends): SPY r0.9 "
        f"{rs['st']['SPY']['levels'][0.9]:.2f} {_rng(dv['spy_r09_lo'], dv['spy_r09_hi'])}, QQQ "
        f"r0.9 {rs['st']['QQQ']['levels'][0.9]:.2f} "
        f"{_rng(dv['qqq_r09_lo'], dv['qqq_r09_hi'])} if dividend-adjusted.")
    ms = rs["missing_sessions"]
    stale = (f"{len(ms)} completed session(s) not in cache ({', '.join(ms)}) per America/"
             f"Chicago clock {rs['now_ct']:%Y-%m-%d %H:%M}" if ms else
             f"cache current per America/Chicago clock {rs['now_ct']:%Y-%m-%d %H:%M}")
    guard = ("incomplete final row(s) EXCLUDED: " + "; ".join(
        f"{i['sym']} {i['date']} {i['why']}" for i in rs["issues"]) if rs["issues"]
        else "incomplete-row guard: final rows clean")
    if rs["intraday"]:
        guard += "; intraday rule excluded " + ", ".join(
            f"{i['sym']} {i['date']}" for i in rs["intraday"]) + \
            f" (before {CLOSE_HOUR_CT}:00 CT)"
    if rs["flags"]:
        guard += f"; {len(rs['flags'])} interior bar(s) flagged ({rs['flags'][0]['sym']} " \
                 f"{rs['flags'][0]['date']} {rs['flags'][0]['why']})"
    out.append(f"As-of {asof}: {stale}; {guard}; delta_4w = {DELTA_SESSIONS} sessions; "
               "halflives are observation counts.")
    g = rs["gaps"]
    eg = rs["etf_gaps"]
    if eg:
        out.append("ETF session gaps vs NYSE calendar ALLOWED (--allow-session-gaps): " + "; ".join(
            f"{s} missing {len(x['missing'])} ({', '.join(x['missing'][:4])}"
            f"{', ...' if len(x['missing']) > 4 else ''})"
            + (f", non-session {', '.join(x['extra'])}" if x["extra"] else "")
            for s, x in eg.items()) + "; EMA trend/smooth continued across them.")
    else:
        other = [f"{s} missing {', '.join(g[s]['missing'])}" for s in ("SPX", "NDX", "VIX")
                 if g[s]["missing"]]
        out.append(f"Session-calendar guard: SPY/QQQ/IWM complete vs NYSE calendar "
                   f"{rs['data_first']}..{asof}"
                   + (f"; non-score caches: {'; '.join(other)}" if other else "") + ".")
    if vx["available"]:
        parts = [f"Robinhood VIX vs FRED VIXCLS: {vx['agree']} bars agree"]
        for d, val, nd, fv in vx["misdated"]:
            parts.append(f"Robinhood bar dated {d} ({val:.2f}) is misdated, it is the {nd} close "
                         f"(FRED {d} = {fv:.2f})")
        if vx["missing"]:
            parts.append("Robinhood missing equity session(s) " + ", ".join(vx["missing"]))
        for d, val, fv in vx["mismatched"]:
            parts.append(f"{d} mismatch RH {val:.2f} vs FRED {fv:.2f}")
        out.append("; ".join(parts) + "; VIX spot and risk row from FRED VIXCLS.")
    else:
        out.append("VIX bars not cross-checked (FRED VIXCLS not supplied); spot from Robinhood.")
    vr = rs["vix"]
    if vr["available"]:
        vv = vr["val"]
        if vv is None:
            vtxt = f"{REANCHOR_DATE} published VIX state not checkable (date absent)"
        else:
            rr = vr["ref"]
            vtxt = (f"reproduces {REANCHOR_DATE} published VIX state (raw {rr['risk_raw']:.3f}, "
                    f"smooth {rr['risk_smooth']:.3f}, d4w {rr['delta_4w']:+.3f}, r0.8 "
                    f"{rr['levels'][0.8]:.2f} vs published {VIX_REF_0814['r0.8']}, r0.9 "
                    f"{rr['levels'][0.9]:.2f})"
                    if vv["ok"] else
                    f"does NOT reproduce {REANCHOR_DATE} published VIX state: " + ", ".join(
                        f"{r['field']} {r['diff']}" for r in vv["rows"] if not r["ok"]))
        out.append(f"VIX row: {vr['method']}; {vtxt}. Low VIX risk = subdued volatility, not "
                   "low equity risk.")
    else:
        out.append(f"VIX row N/A: {vr['reason']}.")
    ix = rs["idxx"]
    cx = []
    for s, col in (("SPX", "SP500"), ("NDX", "NASDAQ100")):
        x = ix[s]
        if not x["available"]:
            cx.append(f"{s} not cross-checked (no FRED {col} in --fred-idx)"
                      if rs["fred_idx_path"] else f"{s} not cross-checked (--fred-idx not supplied)")
        elif x["bad"]:
            cx.append(f"{s} {len(x['bad'])}/{x['n']} Robinhood closes DISAGREE with FRED {col} "
                      f"(first {x['bad'][0][0]} RH {x['bad'][0][1]:.2f} vs {x['bad'][0][2]:.2f})")
        else:
            cx.append(f"{s} Robinhood closes match FRED {col} on all {x['n']} common dates "
                      f"{x['first']}..{x['last']}")
    out.append("Index cross-check: " + "; ".join(cx) + "; no FRED ETF series exists, so ETF "
               "closes rely on the ratio check below.")
    rc = rs["ratio"]
    rparts = []
    for etf in ("SPY", "QQQ"):
        x = rc[etf]
        t = (f"{etf}/{x['index']} {x['n']} sessions, max non-ex-div jump {100 * x['max']:.3f}%")
        if x["exempt"]:
            t += " (ex-div, 1.0% bound, assumed dates: " + ", ".join(
                f"{d} {100 * c:+.3f}%" for d, c in x["exempt"]) + ")"
        if x["jumps"]:
            t += "; JUMPS " + ", ".join(f"{d} {100 * c:+.3f}%" for d, c in x["jumps"])
        if x["unchecked"]:
            t += "; unchecked (index bar missing) " + ", ".join(x["unchecked"])
        rparts.append(t)
    out.append(f"Ratio continuity after {VALIDATION_DATE} (tol {100 * RATIO_JUMP_TOL:.1f}%/session, "
               f"Robinhood SPX/NDX caches): " + "; ".join(rparts)
               + (" -> PASS." if rc["ok"] else " -> FAIL (confidence deducted)."))
    if ts["available"]:
        out.append(f"VIX/VIX3M {ts['ratio']:.3f} ({ts['vix']:.2f}/{ts['vix3m']:.2f}, {ts['date']}) "
                   "is context only, not in the score; contango with low VIX is complacency, "
                   "not low risk.")
    md = []
    for s in ("SPX", "NDX"):
        m = rs["modern"][s]
        if m["available"]:
            gp = m["gaps"]["missing"]
            ex = m["gaps"]["extra"]
            md.append(m["method"] + (f"; FRED misses {len(gp)} NYSE session(s) ({', '.join(gp[:3])})"
                                     if gp else "; no missing NYSE sessions")
                      + (f"; {len(ex)} FRED non-session row(s) excluded ({', '.join(ex[:3])})"
                         if ex else ""))
        else:
            md.append(m["reason"])
    out.append("; ".join(md) + ".")
    out.append("SPX/NDX FULL_HISTORY context N/A: Stooq full histories not cached (no network); "
               "never mixed with ETF-era SPY/QQQ calibration; IWM/SPX/NDX/VIX carry 0% weight.")
    idx = rs["idx"]
    chg = {s: (idx[s]["spot"] / idx[s]["prior"] - 1 if idx[s]["prior"] else None)
           for s in ("SPX", "NDX", "VIX")}
    out.append(f"Spot moves since {VALIDATION_DATE} close: SPX {_pct(chg['SPX'])}, NDX "
               f"{_pct(chg['NDX'])}, VIX {_pct(chg['VIX'])}; r-levels drift up as trend rises; "
               "not support/forecasts.")
    return out


def run_state(b: dict, asof: str | None, allow_unvalidated: bool = False) -> dict:
    asof = asof or b["asof"]
    st = {s: at(b["eng"][s], asof) for s in ETFS}
    comp = composite(st)
    idx = {}
    for s in ("SPX", "NDX", "VIX"):
        dd, src = dict(b["data"][s]), "Robinhood"
        if s == "VIX" and b["fred_vix"]:
            dd, src = b["fred_vix"], "FRED VIXCLS"
        last = max(d for d in dd if d <= asof)
        idx[s] = {"date": last, "spot": dd[last], "prior": dd.get(VALIDATION_DATE), "src": src}
    seam = {s: b["eng"][s][0]["u"] for s in ETFS}
    seam_ok = all(round(seam[s], 6) == SEAM_U[s] for s in ETFS)
    re_hist = {s: run_engine(s, b["data"][s], "reanchor") for s in ETFS}
    re_now = {s: at(re_hist[s], asof) for s in ETFS}
    val = validation(b)
    vix = vix_row(b["fred_vix"], asof)
    fi = b["fred_idx"]
    modern = {s: modern_row(s, fi.get(s) if fi["path"] else None, b["data"][s][0][0], asof,
                            col)
              for s, col in (("SPX", "SP500"), ("NDX", "NASDAQ100"))}
    chg = {"clean": (val["c_clean"], comp), "re": (val["c_re"], composite(re_now)),
           "vix": ((vix["prior"]["risk_smooth"], vix["st"]["risk_smooth"])
                   if vix["available"] and vix["prior"] else None),
           "etf": {s: (val["clean"][s]["risk_smooth"], st[s]["risk_smooth"]) for s in ETFS}}
    return {"asof": asof, "st": st, "comp": comp, "score": score(comp), "idx": idx,
            "seam": seam, "seam_ok": seam_ok, "val": val,
            "div": dividend_sensitivity(b["data"], asof, st), "c_re_now": composite(re_now),
            "chg": chg, "vix": vix, "modern": modern,
            "idxx": {s: index_crosscheck(b["data"][s], fi.get(s), asof) for s in ("SPX", "NDX")},
            "fred_idx_path": fi["path"],
            "ratio": ratio_continuity(b["data"], asof),
            "ts": term_structure(b["fred_vix"], b["fred_vix3m"], asof),
            "vixx": vix_crosscheck(b["data"]["VIX"], b["fred_vix"], asof,
                                   [d for d, _ in b["data"]["SPY"]]),
            "missing_sessions": missing_sessions(asof, b["now_ct"]), "now_ct": b["now_ct"],
            "issues": b["issues"], "flags": b["flags"], "intraday": b["intraday"],
            "gaps": b["gaps"], "etf_gaps": b["etf_gaps"], "data_first": b["data"]["SPY"][0][0],
            "allow_unvalidated": allow_unvalidated}


def _row_txt(name, x, method):
    lv = x["levels"]
    return (f"| {name} | {x['date']} | {x['spot']:.2f} | {x['risk_raw']:.4f} | "
            f"{x['risk_smooth']:.4f} | {_f(x['delta_4w'])} | "
            + " | ".join(f"{lv[r]:.2f}" for r in R_LEVELS) + " | "
            + " | ".join(_pct(x['vs'][r]) for r in R_LEVELS) + f" | {method} |")


def report(b: dict, asof_override: str | None = None, extra: tuple[str, dict] | None = None,
           allow_unvalidated: bool = False) -> tuple[str, dict]:
    rs = run_state(b, asof_override, allow_unvalidated)
    st, idx, asof, comp, sc, v = rs["st"], rs["idx"], rs["asof"], rs["comp"], rs["score"], rs["val"]
    out = []
    for ban in (validation_banner(rs), continuity_banner(rs)):
        if ban:
            out += [ban, ""]
    out += [f"QuantRisk - as of {asof} close (last completed session in cache)",
            f"Method: {ETF_METHOD}", "",
            "| Asset | As-of | Spot | risk_raw | risk_smooth | delta_4w | p@r0.2 | p@r0.5 | "
            "p@r0.8 | p@r0.9 | Spot vs r0.2 | Spot vs r0.5 | Spot vs r0.8 | Spot vs r0.9 | "
            "History/Method |"]
    for s in ("SPX", "NDX"):
        m = rs["modern"][s]
        if m["available"]:
            out.append(_row_txt(f"{s} (modern 1999)", m["st"], m["method"]))
        else:
            i = idx[s]
            out.append(f"| {s} | {i['date']} | {i['spot']:.2f} | " + " | ".join(["N/A"] * 11)
                       + f" | {m['reason']} |")
    for s in ETFS:
        out.append(_row_txt(s, st[s], "fixed-band continuation (official inputs SPY/QQQ)"))
    vr = rs["vix"]
    if vr["available"]:
        out.append(_row_txt("VIX", vr["st"], vr["method"]))
    else:
        i = idx["VIX"]
        out.append(f"| VIX | {i['date']} | {i['spot']:.2f} | " + " | ".join(["N/A"] * 11)
                   + f" | {vr['reason']} |")
    out.append("")
    out.append(f"ETF_composite_risk = 0.60*{st['SPY']['risk_smooth']:.6f} + "
               f"0.40*{st['QQQ']['risk_smooth']:.6f} = {comp:.6f}  -> SCORE {sc}")
    out.append("")
    cr = rs["chg"]
    out.append(f"Current vs prior ({VALIDATION_DATE}), same engine per row:")
    out.append("| Item | Engine | Prior | Current | Change |")
    out.append(f"| Quant score | clean seed | {score(cr['clean'][0])} | {score(cr['clean'][1])} | "
               f"{score(cr['clean'][1]) - score(cr['clean'][0]):+d} |")
    for s in ETFS:
        p, c = cr["etf"][s]
        out.append(f"| {s} risk_smooth | clean seed | {p:.4f} | {c:.4f} | {c - p:+.4f} |")
    out.append(f"| ETF composite | clean seed | {cr['clean'][0]:.6f} | {cr['clean'][1]:.6f} | "
               f"{cr['clean'][1] - cr['clean'][0]:+.6f} |")
    out.append(f"| ETF composite | re-anchored ({REANCHOR_DATE}) | {cr['re'][0]:.6f} | "
               f"{cr['re'][1]:.6f} | {cr['re'][1] - cr['re'][0]:+.6f} |")
    if cr["vix"]:
        out.append(f"| VIX risk_smooth | full-history | {cr['vix'][0]:.4f} | {cr['vix'][1]:.4f} | "
                   f"{cr['vix'][1] - cr['vix'][0]:+.4f} |")
    else:
        out.append("| VIX risk_smooth | full-history | N/A | N/A | N/A |")
    out.append("Engines are never differenced against each other or against the published "
               "0.9698; the published packet appears only in the validation block.")
    out.append("")
    ts = rs["ts"]
    out.append("VIX/VIX3M: " + (f"{ts['ratio']:.4f} ({ts['date']})" if ts["available"]
                                else f"N/A ({ts['reason']})"))
    dvv = rs["div"]["variants"]
    out.append("Dividend sensitivity (assumed): " + "; ".join(
        f"{k} composite {x['composite']:.6f} SCORE {x['score']} SPY r0.9 {100 * x['r09']['SPY']:+.2f}%"
        for k, x in dvv.items()))
    out.append("")
    out.append(f"Validation vs {VALIDATION_DATE} agent of record, clean seed, documented "
               f"lineage tolerance: {'PASS' if v['lin_ok'] else 'FAIL'}")
    for r in v["lin_rows"]:
        out.append(f"  {r['field']}: published {r['published']} computed {r['computed']} diff "
                   f"{r['diff']} tol {r['tol']} {'ok' if r['within_tol'] else 'FAIL'}")
    out.append(f"Re-anchored lineage seed (SPY/QQQ): {'PASS' if v['re_ok'] else 'FAIL'}")
    for r in v["re_rows"]:
        out.append(f"  {r['field']}: published {r['published']} computed {r['computed']} diff "
                   f"{r['diff']} {'ok' if r['within_tol'] else 'FAIL'}")
    out.append(f"Strict-tolerance gaps (clean seed): {len(v['strict_gaps'])} field(s)")
    if vr["available"] and vr["val"]:
        out.append(f"VIX full-history engine vs {REANCHOR_DATE} published VIX state: "
                   f"{'PASS' if vr['val']['ok'] else 'FAIL'}")
        for r in vr["val"]["rows"]:
            out.append(f"  {r['field']}: published {r['published']} computed {r['computed']:.4f} "
                       f"diff {r['diff']} {'ok' if r['ok'] else 'FAIL'}")
    out.append("")
    if extra:
        ed, ev = extra
        xs = {s: run_engine(s, b["data"][s] + [(ed, ev[s])])[-1] for s in ETFS}
        c_x = composite(xs)
        out.append(f"Close-only sensitivity with {ed} closes (SPY {ev['SPY']}, QQQ {ev['QQQ']}): "
                   f"composite {c_x:.4f}, SCORE {score(c_x)}; not official.")
        out.append("")
    conf, ded = confidence(rs)
    out.append(f"Confidence {conf} = 100 - " + " - ".join(f"{d} ({why})" for d, why in ded))
    out.append("Risk-band prices are model levels, not forecasts, supports or guarantees; "
               "r0.8/r0.9 are de-extension levels, never 'cheap'.")
    out.append("")
    out.append("```")
    out.append(packet(rs))
    out.append("```")
    out.append("")
    out.append("```csv")
    out.append(csv_block(rs))
    out.append("```")
    out.append("")
    out.append("QUANT_SNAPSHOT")
    out.append(snapshot(rs))
    return "\n".join(out), rs


def _csv_row(name, x, method):
    return (f"{x['date']},{name},{x['spot']:.2f},{x['risk_raw']:.4f},{x['risk_smooth']:.4f},"
            f"{_f(x['delta_4w'])}," + ",".join(f"{x['levels'][r]:.2f}" for r in R_LEVELS) + ","
            + ",".join(f"{x['vs'][r]:.4f}" for r in R_LEVELS) + f",\"{method}\"")


def csv_block(rs: dict) -> str:
    st, idx = rs["st"], rs["idx"]
    lines = ["ASOF,ASSET,SPOT,RISK_RAW,RISK_SMOOTH,DELTA_4W,PRICE_R0.2,PRICE_R0.5,PRICE_R0.8,"
             "PRICE_R0.9,SPOT_VS_R0.2,SPOT_VS_R0.5,SPOT_VS_R0.8,SPOT_VS_R0.9,HISTORY_METHOD"]
    for s in ("SPX", "NDX"):
        m = rs["modern"][s]
        if m["available"]:
            lines.append(_csv_row(s, m["st"], m["method"]))
        else:
            i = idx[s]
            lines.append(f"{i['date']},{s},{i['spot']:.2f}," + ",".join(["N/A"] * 11)
                         + f",\"Spot from {i['src']} cache; {m['reason']}\"")
    for s in ETFS:
        lines.append(_csv_row(s, st[s], ETF_METHOD))
    vr, vx, ts = rs["vix"], rs["vixx"], rs["ts"]
    extra = ""
    if vx["available"] and (vx["misdated"] or vx["missing"]):
        extra = " (Robinhood VIX bars: " + "; ".join(
            [f"{d} misdated = {nd} close" for d, _, nd, _ in vx["misdated"]]
            + [f"{d} missing" for d in vx["missing"]]) + ")"
    tstxt = f"; VIX/VIX3M {ts['ratio']:.3f}" if ts["available"] else ""
    if vr["available"]:
        lines.append(_csv_row("VIX", vr["st"], vr["method"] + extra + tstxt))
    else:
        i = idx["VIX"]
        lines.append(f"{i['date']},VIX,{i['spot']:.2f}," + ",".join(["N/A"] * 11)
                     + f",\"Spot from {i['src']}{extra}{tstxt}; {vr['reason']}\"")
    return "\n".join(lines)


def snapshot(rs: dict) -> str:
    """Compact rows: as-of, spot, raw, smooth, d4w, p@r0.8, p@r0.9, spot distance to r0.9."""
    hdr = "ASSET | ASOF | SPOT | RISK_RAW | RISK_SMOOTH | D4W | P@R0.8 | P@R0.9 | R0.9_VS_SPOT"
    L = [hdr]

    def row(name, x):
        return (f"{name} | {x['date']} | {x['spot']:.2f} | {x['risk_raw']:.4f} | "
                f"{x['risk_smooth']:.4f} | {_f(x['delta_4w'])} | {x['levels'][0.8]:.2f} | "
                f"{x['levels'][0.9]:.2f} | {100 * (x['levels'][0.9] / x['spot'] - 1):+.2f}%")
    for s in ("SPX", "NDX"):
        m, i = rs["modern"][s], rs["idx"][s]
        if m["available"]:
            L.append(row(f"{s} (modern 1999, FRED)", m["st"]))
        else:
            L.append(f"{s} | {i['date']} | {i['spot']:.2f} | N/A | N/A | N/A | N/A | N/A | N/A "
                     f"({m['reason']})")
    for s in ETFS:
        L.append(row(s, rs["st"][s]))
    vr, i = rs["vix"], rs["idx"]["VIX"]
    if vr["available"]:
        L.append(row("VIX (full history, FRED)", vr["st"]))
    else:
        L.append(f"VIX | {i['date']} | {i['spot']:.2f} | N/A | N/A | N/A | N/A | N/A | N/A "
                 f"({vr['reason']})")
    return "\n".join(L)


# ---------------------------------------------------------------------------------------
# Self-test gate
# ---------------------------------------------------------------------------------------

def _copy_cache(src: str, dst: str):
    for s in SERIES:
        shutil.copy(os.path.join(src, f"{s}.csv"), os.path.join(dst, f"{s}.csv"))


def _edit_csv(path: str, fn):
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    rows = fn(rows)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "low", "high", "close"])
        w.writeheader()
        w.writerows(rows)


def _run_main(argv: list[str]) -> tuple[int, str]:
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, buf.getvalue() + err.getvalue()


def self_test(cache: str, fred_vix: str | None, fred_idx: str | None,
              fred_vix3m: str | None) -> int:
    fails = 0

    def check(ok: bool, msg: str):
        nonlocal fails
        print(("ok   " if ok else "FAIL ") + msg)
        fails += 0 if ok else 1

    now = dt.datetime(2026, 10, 30, 10, 0)

    # 1. Fixture internal consistency: p@r levels follow from (trend, dev bands).
    for sym, fx in FIXTURES.items():
        w = fx["dev_high"] - fx["dev_low"]
        t = math.log(fx["price_at_r0_5"]) - fx["dev_low"] - 0.5 * w
        worst = max(abs(math.exp(t + fx["dev_low"] + r * w) /
                        fx[f"price_at_r0_{int(r * 10)}"] - 1) for r in (0.2, 0.8, 0.9))
        check(worst < 1e-9, f"fixture {sym} p@r0.2/0.8/0.9 reproduce from p@r0.5 + bands "
                            f"(max rel err {worst:.1e})")

    # 2. Arithmetic: composite weights and round-half-up score; context rows carry no weight.
    check(abs(sum(WEIGHTS.values()) - 1.0) < 1e-12 and set(WEIGHTS) == {"SPY", "QQQ"},
          "composite weights SPY 0.60 + QQQ 0.40 = 1.0; IWM/SPX/NDX/VIX not weighted")
    check(score(0.965) == 97 and score(0.96499) == 96 and score(0.971087) == 97,
          "score() rounds half up: 0.965->97, 0.96499->96, 0.971087->97")

    # 3. NYSE calendar pinned to published closures.
    check(nyse_holidays(2026) == NYSE_HOLIDAYS_2026 and nyse_holidays(2025) == NYSE_HOLIDAYS_2025,
          "NYSE calendar reproduces published 2025 and 2026 full-day closures")
    check(nyse_holidays(2022) >= {"2022-06-20", "2022-12-26"} and "2021-12-31" not in
          nyse_holidays(2021) and "2012-10-29" in nyse_holidays(2012),
          "NYSE calendar: Juneteenth 2022 observed Mon, Sat New Year not observed, Sandy closure")

    # 4. Incomplete-row guard and the drop loop.
    good = {"date": "x", "low": 1.0, "high": 2.0, "close": 1.5}
    cases = [({**good}, None), ({**good, "close": None}, "missing close"),
             ({**good, "low": None, "high": None}, "missing low/high (close-only bar)"),
             ({**good, "low": 1.5, "high": 1.5}, "low==high==close (looks interpolated/synthesized)")]
    check(all(incomplete_reason(r) == want for r, want in cases),
          "guard classifies complete / missing close / close-only / low==high==close bars")
    try:
        guard_final("T", [good, {**good, "date": "y", "low": None, "high": None}], drop=False)
        check(False, "guard refuses an incomplete final row by default")
    except Refuse:
        check(True, "guard refuses an incomplete final row by default")
    kept, iss = guard_final("T", [good, {**good, "date": "y", "close": None}], drop=True)
    check(len(kept) == 1 and len(iss) == 1, "guard drops and flags with --drop-incomplete-final")
    bad1 = {**good, "date": "y", "low": None, "high": None}
    bad2 = {**good, "date": "z", "low": 1.5, "high": 1.5}
    kept, iss = guard_final("T", [good, bad1, bad2], drop=True)
    check(len(kept) == 1 and [i["date"] for i in iss] == ["z", "y"],
          "drop loop re-checks the new final row and drops again until clean")
    try:
        guard_final("T", [good] + [{**bad1, "date": f"d{k}"} for k in range(MAX_DROP_FINAL + 1)],
                    drop=True)
        check(False, f"drop loop refuses beyond {MAX_DROP_FINAL} consecutive incomplete rows")
    except Refuse:
        check(True, f"drop loop refuses beyond {MAX_DROP_FINAL} consecutive incomplete rows")
    try:
        guard_final("T", [bad1, bad2], drop=True)
        check(False, "drop loop refuses when no clean row is left")
    except Refuse:
        check(True, "drop loop refuses when no clean row is left")

    # 5. Intraday rule.
    rows = [{**good, "date": "2026-10-28"}, {**good, "date": "2026-10-30"}]
    k1, x1 = apply_intraday("T", rows, now)
    k2, x2 = apply_intraday("T", rows, dt.datetime(2026, 10, 30, 15, 5))
    check(len(k1) == 1 and len(x1) == 1 and len(k2) == 2 and not x2,
          "intraday rule: today's row excluded at 10:00 CT, kept after the 15:00 CT close")
    try:
        apply_intraday("T", rows + [{**good, "date": "2026-10-31"}], now)
        check(False, "intraday rule refuses a row dated after today (America/Chicago)")
    except Refuse:
        check(True, "intraday rule refuses a row dated after today (America/Chicago)")

    # 6. Seam: cached 2025-12-26 closes reproduce fixture u / risk_raw to 6 dp.
    b = build(cache, now_ct=now, fred_vix=fred_vix, fred_idx=fred_idx, fred_vix3m=fred_vix3m)
    check(not b["etf_gaps"], "interior-gap guard: SPY/QQQ/IWM cache complete vs NYSE calendar")
    for sym in ETFS:
        s0 = b["eng"][sym][0]
        u = round(s0["u"], 6)
        raw_cached = min(1.0, max(0.0, s0["u"]))
        check(u == SEAM_U[sym] and abs(raw_cached - FIXTURES[sym]["risk_raw"]) < 1e-6,
              f"seam {sym} {SEED_DATE} u={s0['u']:.6f} (want {SEAM_U[sym]}) "
              f"risk_raw={raw_cached:.6f} (fixture {FIXTURES[sym]['risk_raw']:.6f})")

    # 7. Lineage gate vs the published 2026-09-17 packet.
    v = validation(b)
    print(f"-- clean fixture seed vs {VALIDATION_DATE} published, documented lineage tolerance "
          f"{LINEAGE_TOL}")
    for r in v["lin_rows"]:
        check(r["within_tol"], f"{r['field']}: published {r['published']} computed "
                               f"{r['computed']} diff {r['diff']} (tol {r['tol']})")
    print("-- clean-seed diffs vs DOCUMENTED_GAP (known lineage gap pinned: centre +- band)")
    for r in v["gap_rows"]:
        unit = "%" if r["key"].startswith("r0.") else ""
        sc = 100 if unit else 1
        check(r["within_gap"], f"{r['field']}: diff {sc * r['d']:+.4f}{unit} vs documented "
                               f"{sc * r['centre']:+.4f}{unit} +- {sc * r['band']:.4f}{unit}")
    print(f"-- re-anchored seed ({REANCHOR_DATE} published state) vs {VALIDATION_DATE}, "
          f"tolerance {REANCHOR_TOL}")
    for r in v["re_rows"]:
        check(r["within_tol"], f"{r['field']}: published {r['published']} computed "
                               f"{r['computed']} diff {r['diff']} (tol {r['tol']})")
    check(not v["unexpected_strict"], "strict-tolerance gaps are exactly the known lineage-gap "
                                      "fields: " + ", ".join(f"{r['field']} {r['diff']}"
                                                             for r in v["strict_gaps"]))

    # 8. VIX full-history engine vs the published 2026-08-14 VIX state.
    vx = vix_row(b["fred_vix"], REANCHOR_DATE)
    if not vx["available"]:
        check(False, f"VIX {REANCHOR_DATE} validation needs --fred-vix full history "
                     f"({vx['reason']})")
    else:
        print(f"-- VIX full-history engine ({vx['st']['first']}..{REANCHOR_DATE}, "
              f"{vx['st']['n']} obs) vs published {REANCHOR_DATE} VIX state, tol {VIX_TOL}")
        for r in vix_validate(vx["st"])["rows"]:
            check(r["ok"], f"{r['field']}: published {r['published']} computed "
                           f"{r['computed']:.4f} diff {r['diff']}")
        check(not any(k in WEIGHTS for k in ("VIX", "SPX", "NDX")),
              "VIX is context only: not in the composite")

    # 9. One-engine change since 2026-09-17 (pinned on this cache at 2026-10-28).
    if "2026-10-28" in dict(b["data"]["SPY"]):
        c28 = composite({s: at(b["eng"][s], "2026-10-28") for s in ETFS})
        r28 = composite({s: at(run_engine(s, b["data"][s], "reanchor"), "2026-10-28")
                         for s in ETFS})
        check(abs(v["c_clean"] - 0.971737) < 5e-7 and abs(c28 - 0.971087) < 5e-7,
              f"clean-seed engine {VALIDATION_DATE} {v['c_clean']:.6f} -> 2026-10-28 {c28:.6f} "
              f"({c28 - v['c_clean']:+.6f})")
        check(abs(v["c_re"] - 0.969782) < 5e-7 and abs(r28 - 0.9672) < 5e-5,
              f"re-anchored engine {VALIDATION_DATE} {v['c_re']:.6f} -> 2026-10-28 {r28:.6f} "
              f"({r28 - v['c_re']:+.6f})")

    # 10. Ratio continuity on the real cache, and a planted jump.
    rc = ratio_continuity(b["data"], b["asof"])
    check(rc["ok"], f"SPY/SPX, QQQ/NDX ratio continuity after {VALIDATION_DATE}: max jump "
                    f"{100 * rc['SPY']['max']:.3f}% / {100 * rc['QQQ']['max']:.3f}%, ex-div "
                    "exempt " + ", ".join(f"{d} {100 * c:+.3f}%" for k in ("SPY", "QQQ")
                                          for d, c in rc[k]["exempt"]))
    dd = dict(b["data"]["SPY"])
    jd = [d for d in dd if d > "2026-10-01"][3]
    planted = {**b["data"], "SPY": [(d, c * (1.01 if d == jd else 1.0))
                                    for d, c in b["data"]["SPY"]]}
    rp = ratio_continuity(planted, b["asof"])
    check(not rp["ok"] and rp["SPY"]["jumps"] and rp["SPY"]["jumps"][0][0] == jd,
          f"ratio continuity flags a planted 1% SPY jump on {jd}")

    # 11. Modern-window index rows (only when --fred-idx is supplied).
    if fred_idx:
        rs = run_state(b, b["asof"])
        nd, sp = rs["modern"]["NDX"], rs["modern"]["SPX"]
        check(nd["available"] and nd["st"]["first"] == MODERN_START["NDX"]
              and nd["st"]["n"] == len(sessions_between(MODERN_START["NDX"], nd["st"]["date"]))
              - len(nd["gaps"]["missing"]),
              "NDX_MODERN_1999 computed from FRED NASDAQ100, one obs per NYSE session"
              + (f" ({nd['st']['first']}..{nd['st']['date']}, {nd['st']['n']} obs, smooth "
                 f"{nd['st']['risk_smooth']:.4f}); FRED non-session rows excluded "
                 f"{nd['gaps']['extra']}, missing sessions {nd['gaps']['missing']} (disclosed)"
                 if nd["available"] else f": {nd.get('reason')}"))
        print(f"info: SPX_MODERN_1999 "
              f"{'computed' if sp['available'] else 'N/A: ' + sp['reason']}")
        check(all(rs["idxx"][s]["available"] and not rs["idxx"][s]["bad"] for s in ("SPX", "NDX")),
              "Robinhood SPX/NDX closes match FRED on every common date")
    else:
        print("info: --fred-idx not supplied; modern-window and index cross-check tests skipped")

    # 12. End-to-end gates on planted caches (exit codes, banners, confidence).
    with tempfile.TemporaryDirectory() as tmp:
        base_args = ["--now", "2026-10-30T10:00", "--asof", "2026-10-28"]
        if fred_vix:
            base_args += ["--fred-vix", fred_vix]

        def mk(name, sym_fn):
            d = os.path.join(tmp, name)
            os.mkdir(d)
            _copy_cache(cache, d)
            for s, fn in sym_fn.items():
                _edit_csv(os.path.join(d, f"{s}.csv"), fn)
            return d

        rc0, out0 = _run_main(["--cache", cache] + base_args)
        check(rc0 == EXIT_OK and "VALIDATION FAILED" not in out0 and "SCORE_0_100: 97" in out0,
              f"clean live run exits 0 with SCORE 97 and no validation banner (exit {rc0})")
        gap_day = "2026-10-14"
        gd = mk("gap", {s: (lambda rows: [r for r in rows if r["date"] != gap_day])
                        for s in ("SPY", "QQQ", "IWM")})
        rc1, out1 = _run_main(["--cache", gd] + base_args)
        check(rc1 == EXIT_REFUSED and "interior session gap" in out1,
              f"interior-gap guard refuses a missing {gap_day} session (exit {rc1})")
        rc2, out2 = _run_main(["--cache", gd, "--allow-session-gaps"] + base_args)
        check(rc2 == EXIT_OK and "ALLOWED by --allow-session-gaps" in out2
              and "ETF session gaps vs NYSE calendar ALLOWED" in out2,
              f"--allow-session-gaps proceeds, discloses and deducts confidence (exit {rc2})")
        intr = mk("intraday", {s: (lambda rows: rows + [{**rows[-1], "date": "2026-10-30"}])
                               for s in SERIES})
        rc3, out3 = _run_main(["--cache", intr, "--now", "2026-10-30T10:00"]
                              + (["--fred-vix", fred_vix] if fred_vix else []))
        check(rc3 == EXIT_OK and "ASOF: 2026-10-28 close" in out3
              and "intraday rule excluded" in out3,
              f"current-day row dated 2026-10-30 excluded at 10:00 CT; as-of 2026-10-28 (exit {rc3})")
        sd = mk("seam", {"SPY": lambda rows: [{**r, "close": f"{float(r['close']) * 1.0001:.6f}"}
                                              if r["date"] == SEED_DATE else r for r in rows]})
        rc4, out4 = _run_main(["--cache", sd] + base_args)
        check(rc4 == EXIT_OK and "seam mismatch" in out4 and "DOES NOT match" in out4,
              f"seam mismatch alone costs confidence and is disclosed (exit {rc4})")
        vd = mk("val", {"SPY": lambda rows: [{**r, "close": f"{float(r['close']) * 1.01:.6f}",
                                              "high": f"{float(r['high']) * 1.01:.6f}"}
                                             if r["date"] == VALIDATION_DATE else r
                                             for r in rows]})
        rc5, out5 = _run_main(["--cache", vd] + base_args)
        pk = out5.split("SUPERVISOR PACKET (COPY/PASTE)\n", 1)[-1].splitlines()
        check(rc5 == EXIT_UNVALIDATED and pk and pk[0].startswith("WARNING: VALIDATION FAILED"),
              f"failed {VALIDATION_DATE} lineage: packet header warns and run exits 3 (exit {rc5})")
        rc6, out6 = _run_main(["--cache", vd, "--allow-unvalidated"] + base_args)
        check(rc6 == EXIT_OK and "WARNING: VALIDATION FAILED" in out6
              and "--allow-unvalidated" in out6,
              f"--allow-unvalidated releases with the warning kept (exit {rc6})")
        jd2 = "2026-10-07"
        jp = mk("jump", {"QQQ": lambda rows: [{**r, "close": f"{float(r['close']) * 0.99:.6f}",
                                               "low": f"{float(r['low']) * 0.99:.6f}"}
                                              if r["date"] == jd2 else r for r in rows]})
        rc9, out9 = _run_main(["--cache", jp] + base_args)
        pk9 = out9.split("SUPERVISOR PACKET (COPY/PASTE)\n", 1)[-1].splitlines()
        check(rc9 == EXIT_OK and pk9[0].startswith("WARNING: ETF/INDEX RATIO CONTINUITY FAILED")
              and "ratio jump(s)" in out9,
              f"planted 1% QQQ/NDX break on {jd2}: packet header warns, confidence deducted")
        dr = mk("drop", {s: (lambda rows: rows + [
            {"date": "2026-10-29", "low": rows[-1]["close"], "high": rows[-1]["close"],
             "close": rows[-1]["close"]}]) for s in SERIES})
        rc7, _ = _run_main(["--cache", dr, "--now", "2026-10-30T10:00"])
        rc8, out8 = _run_main(["--cache", dr, "--now", "2026-10-30T10:00",
                               "--drop-incomplete-final"]
                              + (["--fred-vix", fred_vix] if fred_vix else []))
        check(rc7 == EXIT_REFUSED and rc8 == EXIT_OK and "EXCLUDED" in out8,
              f"synthesized final row refused by default (exit {rc7}), dropped with flag "
              f"(exit {rc8})")

    print("SELF-TEST PASSED" if fails == 0 else f"SELF-TEST FAILED ({fails})")
    return EXIT_OK if fails == 0 else EXIT_SELFTEST_FAIL


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", required=True, help="directory with SPY/QQQ/IWM/SPX/NDX/VIX.csv")
    ap.add_argument("--asof", help="override as-of date (must be a cached session)")
    ap.add_argument("--fred-vix", help="FRED VIXCLS csv (observation_date,VIXCLS)")
    ap.add_argument("--fred-vix3m", help="FRED VXVCLS csv (observation_date,VXVCLS)")
    ap.add_argument("--fred-idx", help="FRED csv with observation_date,SP500,NASDAQ100 columns")
    ap.add_argument("--drop-incomplete-final", action="store_true",
                    help="drop (and flag) incomplete final rows until the final row is clean")
    ap.add_argument("--allow-session-gaps", action="store_true",
                    help="proceed despite ETF interior NYSE-session gaps (disclosed, deducted)")
    ap.add_argument("--allow-unvalidated", action="store_true",
                    help="exit 0 even if the 2026-09-17 lineage validation fails (banner kept)")
    ap.add_argument("--now", help="America/Chicago wall clock YYYY-MM-DDTHH:MM")
    ap.add_argument("--extra-close", help="sensitivity only: DATE:SPY=x,QQQ=y,IWM=z close-only row")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.self_test:
            return self_test(a.cache, a.fred_vix, a.fred_idx, a.fred_vix3m)
        extra = None
        if a.extra_close:
            d, kv = a.extra_close.split(":", 1)
            extra = (d, {k: float(v) for k, v in (p.split("=") for p in kv.split(","))})
        now = dt.datetime.fromisoformat(a.now) if a.now else None
        b = build(a.cache, a.drop_incomplete_final, a.fred_vix, a.fred_vix3m, now,
                  a.allow_session_gaps, a.fred_idx)
        text, rs = report(b, a.asof, extra, a.allow_unvalidated)
    except Refuse as e:
        print(f"STOP: {e}", file=sys.stderr)
        return EXIT_REFUSED
    print(text)
    if not rs["val"]["ok"] and not a.allow_unvalidated:
        print(f"STOP: {VALIDATION_DATE} lineage validation FAILED; packet is not releasable. "
              "Rerun with --allow-unvalidated to release it with the warning.", file=sys.stderr)
        return EXIT_UNVALIDATED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
