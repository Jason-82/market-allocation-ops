#!/usr/bin/env python3
"""Sentiment & Technicals module calculator (stdlib only).

Implements supervisor/modules/sentiment_technicals.yaml:
  current_scoring_methodology (40/30/30 framework, subweights, anchor mappings, SPHB/SPLV
  overheating adjustment, missing_input_policy), calculations.formulas, asof_selection_rule,
  separate_condition_labels, change_my_mind_triggers and supervisor_integration.confidence_formula.

Data sources
  * ETFs (SPY RSP IWM XLY XLP SPHB SPLV): cache CSVs with header date,low,high,close
    (Robinhood daily bars). Raw (split-adjusted, NOT dividend-adjusted) closes; dividends are
    omitted from every relative return and from the SPY moving averages (disclosed in NOTES).
  * VIX: FRED VIXCLS via --vix-csv (preferred). The cache VIX.csv (Robinhood) is then used ONLY
    as a cross-check; every date where the two disagree by more than 0.05 is reported. Without
    --vix-csv the cache VIX.csv is used for scoring and a warning is printed.
    Known defect (audit 2026-10-30): Robinhood's VIX bar labelled 2026-10-21 (19.03) is the
    2026-10-22 close; FRED has 2026-10-21=16.68, 2026-10-22=19.03.
  * VIX3M: FRED VXVCLS via --vix3m-csv (spec: "VXVCLS may stand in for VIX3M"). The ratio is
    computed on the latest date COMMON to VIX and VIX3M on or before the as-of (asof_selection
    _rule). If that date is older than the as-of, the mismatch is dated and 7 confidence points
    are deducted. --vix3m LEVEL remains as a manual override dated at the as-of.
  FRED CSVs: header observation_date,<SERIES>; '.' or empty means missing.

VIX session-calendar rule (explicit; vix_on_equity_sessions): every VIX and VIX3M session count
(5D/20D/252D) uses US equity sessions only, i.e. the SPY trading calendar. FRED prints VIXCLS on
some equity holidays (e.g. 2025-11-27, 2026-09-07); those prints are DROPPED before any lookback is
counted and are listed in NOTES. This is the rule that reproduces the 2026-09-17 packet: on the
equity calendar the 20D base is 2026-08-19 (+3.69%, published +3.69%); counting on the FRED
calendar would put the base at 2026-08-20 (-3.56%). The self-test checks both.

Price convention: raw closes (continuity with the 2026-09-17 calibration, which reproduces on raw
closes). Dividends are omitted (spec adjusted_or_unadjusted_prices). A dividend sensitivity for
the SPY trend inputs is computed and disclosed (spy_dividend_sensitivity): SPY ex-dates follow the
SPDR schedule (third Friday of Mar/Jun/Sep/Dec, previous session if that is not a session) and each
dividend is estimated from the drop in the SPY/SPX close ratio on the ex-date, using the cached SPX
series (--spy-dividends DATE:AMOUNT overrides with confirmed amounts). It is diagnostic only; the
official score stays on raw closes. Pairs are NOT dividend-adjusted in the sensitivity because the
numerator ETFs' dividends are not in the cache (pair_rule: both legs on one convention).

Rows after the as-of: the cache builder drops Robinhood daily bars flagged `interpolated`
(placeholder bars that carry the prior close forward). A date present in some series but missing
from others after the as-of is reported with the series that lack it and the FRED last dates.

Usage
    python3 tools/sentiment_technicals.py --cache DIR --vix-csv VIXCLS.csv --vix3m-csv VXVCLS.csv
        [--asof YYYY-MM-DD] [--prior-score 63 --prior-asof 2026-09-17
         --prior-components 69.3,52.5,64.8] [--spy-dividends DATE:AMT,...] [--csv-out PATH]
    python3 tools/sentiment_technicals.py --self-test --cache DIR [--vix-csv ...] [--vix3m-csv ...]

Self-test residual (known, documented): recomputing 2026-09-17 from the raw cache gives raw
62.889 (components 69.28 / 52.49 / 64.77) versus 62.910 from the agent of record's published
components (69.3 / 52.5 / 64.8). Both round to the published 63. The ~0.02 gap comes from the
agent's two-decimal published inputs (e.g. SPY vs 50DMA 0.41 published vs 0.404 recomputed).
Confidence method (new with this tool, 2026-10-30): confidence is COMPUTED from inputs per
supervisor_integration.confidence_formula. Applied to the 2026-09-17 data it gives 87 versus the
external agent's published 82; the agent did not publish its DataCompleteness/SourceQuality/
SignalCoherence inputs, so that +5 residual is reported, not tuned away. A run's confidence is
therefore comparable like-for-like with the computed 2026-09-17 value, not with 82; every live
run recomputes 2026-09-17 and prints that comparator in NOTES.
SourceQuality feed-defect penalty (-5) applies only to a group whose SCORED series has a defect
inside its active lookback. A defect found in a cross-check-only series (the Robinhood VIX bars
when VIX is scored from FRED) is disclosed but does not mark down any scored series.

Decision support only; not financial advice.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import sys
from decimal import ROUND_HALF_UP, Decimal

# --------------------------------------------------------------------------------------
# Spec constants (supervisor/modules/sentiment_technicals.yaml). Keep in sync with the spec.
# --------------------------------------------------------------------------------------
TOP_WEIGHTS = {"volatility": 0.40, "trend": 0.30, "breadth": 0.30}

VOL_SUBWEIGHTS = {
    "vix_level": 0.30,
    "vix_20d_change": 0.20,
    "vix_5d_change": 0.10,
    "vix_term_structure": 0.25,
    "vix_52w_position": 0.15,
}
TREND_SUBWEIGHTS = {"spy_vs_50dma": 0.30, "spy_vs_200dma": 0.45, "spy_drawdown": 0.25}
BREADTH_SUBWEIGHTS = {"rsp_spy": 0.30, "iwm_spy": 0.30, "xly_xlp": 0.20, "sphb_splv": 0.20}

# Blend of 63-session and 20-session relative returns per pair: (w63, w20).
PAIR_BLEND = {
    "rsp_spy": (0.70, 0.30),
    "iwm_spy": (0.70, 0.30),
    "xly_xlp": (0.50, 0.50),
    "sphb_splv": (0.60, 0.40),
}
PAIRS = {
    "rsp_spy": ("RSP", "SPY"),
    "iwm_spy": ("IWM", "SPY"),
    "xly_xlp": ("XLY", "XLP"),
    "sphb_splv": ("SPHB", "SPLV"),
}
PAIR_LABEL = {"rsp_spy": "RSP/SPY", "iwm_spy": "IWM/SPY", "xly_xlp": "XLY/XLP",
              "sphb_splv": "SPHB/SPLV"}

ANCHORS = {
    "vix_level": [(10.0, 100), (12.5, 100), (13.5, 90), (15.0, 75), (17.0, 55), (19.0, 35),
                  (21.0, 30), (23.0, 40), (25.0, 55), (30.0, 80), (40.0, 100)],
    "vix_20d_change": [(-40.0, 100), (-25.0, 95), (-15.0, 80), (-5.0, 60), (0.0, 50),
                       (20.0, 30), (40.0, 50), (75.0, 80), (150.0, 100)],
    "vix_5d_change": [(-25.0, 95), (-15.0, 80), (-5.0, 60), (0.0, 50), (15.0, 30),
                      (35.0, 55), (60.0, 80), (100.0, 100)],
    "vix_term_structure": [(0.70, 100), (0.78, 90), (0.85, 70), (0.93, 45), (0.98, 30),
                           (1.00, 45), (1.05, 75), (1.15, 100)],
    "vix_52w_position": [(0.00, 100), (0.05, 95), (0.15, 75), (0.30, 45), (0.45, 30),
                         (0.60, 45), (0.80, 75), (1.00, 100)],
    "spy_vs_50dma": [(-15.0, 100), (-10.0, 85), (-5.0, 65), (-2.0, 40), (0.0, 35), (2.0, 55),
                     (5.0, 75), (8.0, 90), (12.0, 100)],
    "spy_vs_200dma": [(-25.0, 100), (-15.0, 95), (-8.0, 80), (-3.0, 55), (0.0, 35), (4.0, 50),
                      (8.0, 70), (12.0, 85), (15.0, 95), (20.0, 100)],
    "spy_drawdown": [(0.0, 70), (1.0, 60), (3.0, 40), (5.0, 25), (8.0, 35), (10.0, 50),
                     (15.0, 75), (20.0, 90), (30.0, 100)],
    "rsp_spy": [(-8.0, 100), (-5.0, 85), (-2.0, 65), (0.0, 45), (1.0, 35), (3.0, 20),
                (5.0, 10), (8.0, 5)],
    "iwm_spy": [(-8.0, 100), (-5.0, 85), (-2.0, 65), (0.0, 45), (1.0, 35), (3.0, 20),
                (5.0, 10), (8.0, 5)],
    "xly_xlp": [(-12.0, 100), (-7.0, 85), (-3.0, 65), (0.0, 45), (2.0, 25), (5.0, 15),
                (10.0, 10)],
    "sphb_splv": [(-10.0, 100), (-5.0, 75), (-2.0, 55), (0.0, 35), (3.0, 20), (8.0, 10)],
}

# Input at which each non-monotonic mapping reaches its minimum risk. Used only for the
# driver-type diagnostic. (pivot, side_if_input_below_pivot, side_if_input_above_pivot)
SIDE_RULES = {
    "vix_level": (21.0, "complacency", "stress"),
    "vix_20d_change": (20.0, "complacency", "stress"),
    "vix_5d_change": (15.0, "complacency", "stress"),
    "vix_term_structure": (0.98, "complacency", "stress"),
    "vix_52w_position": (0.45, "complacency", "stress"),
    "spy_vs_50dma": (0.0, "stress", "complacency"),
    "spy_vs_200dma": (0.0, "stress", "complacency"),
    "spy_drawdown": (5.0, "complacency", "stress"),
}

# separate_condition_labels. "Materially below 0.90" is operationalized as <= 0.85, the
# term-structure anchor at which the subscore reaches 70 (the spec's "very steep contango").
# A ratio in (0.85, 0.93) is reported as a contango-steepness lean on a Neutral label, not as
# Complacent. "Large negative 20-session change" is operationalized as <= -15% (the anchor at
# which the 20D subscore reaches 80).
COND = {
    "complacent_vix_max": 15.5,          # "at or below approximately 15.5"
    "complacent_ratio_max": 0.85,        # "materially below 0.90"
    "normal_contango_min": 0.93,         # interpretation.low_risk_regime: "near 0.93-0.98"
    "complacent_pos_max": 0.15,          # "near the bottom 15 percent of its 52-week range"
    "complacent_ch20_max": -15.0,        # "a large negative 20-session VIX change"
    "fearful_vix_min": 20.0,             # "VIX above 20"
    "fearful_ch20_min": 25.0,            # "20-session VIX increase above approximately 25%"
    "panic_vix_min": 30.0,               # "VIX above 30"
    "panic_ratio_min": 1.05,             # "above approximately 1.05 for at least two closes"
    "backwardation": 1.00,
}

# change_my_mind_triggers / escalation_thresholds (spec wording and guidance ranges).
TRIG = {
    "melt_up_vix_max": 13.0,             # "VIX closes at or below 13"
    "melt_up_spy200_min": 12.0,          # "SPY reaches at least 12 percent above its 200DMA"
    "melt_up_sphb20_min": 8.0,           # "SPHB/SPLV exceeds approximately 8% over 20D"
    "melt_up_sphb63_min": 25.0,          # "... or 25% over 63D"
    "melt_up_guidance": "70-76",
    "risk_off_ratio_min": 1.00,          # "VIX/VIX3M exceeds 1.00 for at least two closes"
    "risk_off_guidance": "70-85",
    "reset_dd_lo": 3.0, "reset_dd_hi": 7.0,       # "orderly 3-7 percent pullback"
    "reset_vix_lo": 17.0, "reset_vix_hi": 22.0,   # "VIX into approximately 17-22"
    "reset_guidance": "45-55",
    "risk_on_vix_max": 17.0,             # "VIX falls below approximately 17"
}

ETFS = ["SPY", "RSP", "IWM", "XLY", "XLP", "SPHB", "SPLV"]

SCORE_BANDS = [(0, 34, "Low module risk"), (35, 49, "Neutral-to-supportive"),
               (50, 59, "Mild caution"), (60, 69, "Moderate caution"), (70, 84, "High caution"),
               (85, 100, "Extreme caution")]

# Confidence (supervisor_integration.confidence_formula). SourceQuality per spec: 100 for
# official/exchange/issuer, about 85 for FRED or stable public historical feeds, about 70 for
# performance-table fallbacks. A group is marked down FEED_DEFECT_PENALTY only when a series it
# actually SCORES has a known or detected defect inside its active lookback. Defects in a
# cross-check-only series are disclosed but never penalize another group.
SOURCE_QUALITY = {"FRED": 85.0, "Robinhood daily bars": 85.0, "manual entry": 70.0}
FEED_DEFECT_PENALTY = 5.0
# Known defects per (source, series): {date: description}. Applied only when that source is the
# scoring source for that series and the date lies inside the series' active lookback.
KNOWN_FEED_DEFECTS = {
    ("Robinhood daily bars", "VIX"): {
        "2026-10-21": "Robinhood VIX bar labelled 2026-10-21 (19.03) is the 2026-10-22 close "
                      "(FRED 2026-10-21=16.68, 2026-10-22=19.03); audit 2026-10-30",
    },
}
# Rounding-edge disclosure (CLAUDE.md: a score within about 0.5 of a rounding boundary is stated).
ROUNDING_EDGE_BAND = 0.5
# Cache-build convention for rows missing after the as-of (see module docstring).
CACHE_DROP_REASON = ("their Robinhood daily bars for that date were flagged interpolated "
                     "placeholders (prior close carried forward) and dropped at cache build")
GOVERNANCE_NOTE = ("Supervisor self-run at Jason's request (2026-10-30: 'it would be much easier going forward if you could simply run them all yourself'; recorded in CLAUDE.md), "
                   "replacing the external agent; spec, anchors and weights unchanged.")
# Share of the final score carried by each input group (for weighting SourceQuality).
SQ_WEIGHTS = {"vix": TOP_WEIGHTS["volatility"] * (1 - VOL_SUBWEIGHTS["vix_term_structure"]),
              "vix3m": TOP_WEIGHTS["volatility"] * VOL_SUBWEIGHTS["vix_term_structure"],
              "etf": TOP_WEIGHTS["trend"] + TOP_WEIGHTS["breadth"]}
# SignalCoherence: spec "90-100 when volatility, trend, and breadth agree; 60-80 when mixed;
# below 60 when signals strongly conflict or are stale". Agree = all three components on the
# same side of 50 AND the driver type is not mixed. Strong conflict = component spread >= 35.
COHERENCE = {"agree": 95.0, "mixed": 70.0, "conflict": 55.0, "stale_cap": 59.0,
             "conflict_spread": 35.0, "side_line": 50.0}

XCHECK_TOL = 0.05
XCHECK_EPS = 1e-9  # float guard so a difference of exactly 0.05 is not flagged

# Agent of record's 2026-09-17 packet (validation reference).
VALIDATION_0917 = {
    "asof": "2026-09-17",
    "vix3m": 18.55,
    "metrics": {
        "vix": 15.44, "vix_5d": -13.45, "vix_20d": 3.69, "vix_52w_position": 0.112,
        "vix_term_ratio": 0.832, "spy_close": 762.60, "spy_vs_50dma": 0.41,
        "spy_vs_200dma": 6.53, "spy_drawdown_mag": 1.96,
        "rsp_spy_20d": -3.13, "rsp_spy_63d": -0.83, "iwm_spy_20d": -4.60, "iwm_spy_63d": -4.33,
        "xly_xlp_20d": -2.64, "xly_xlp_63d": -3.33, "sphb_splv_20d": 1.84, "sphb_splv_63d": -3.24,
    },
    "components": {"volatility": 69.3, "trend": 52.5, "breadth": 64.8},
    "score": 63,
    "confidence": 82,
}
COMPONENT_TOL = 2.0
SCORE_TOL = 1

DEFAULT_CACHE = os.environ.get("SENTIMENT_TECH_CACHE", "")


# --------------------------------------------------------------------------------------
# Arithmetic primitives and display rounding (half-up everywhere)
# --------------------------------------------------------------------------------------
def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def interp(x: float, anchors: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation with nearest-endpoint clamping outside the range."""
    if x <= anchors[0][0]:
        return clamp(float(anchors[0][1]))
    if x >= anchors[-1][0]:
        return clamp(float(anchors[-1][1]))
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return clamp(float(y1))
            return clamp(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    raise AssertionError("unreachable")


def round_half_up(x: float, places: int = 0) -> Decimal:
    q = Decimal(1).scaleb(-places)
    return Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP)


def fmt(x: float, places: int = 2, sign: bool = False) -> str:
    """Display formatting with half-up rounding (spec: percentages 2dp, components 1dp)."""
    d = round_half_up(x, places)
    if d == 0:
        d = abs(d)  # never print -0.00
    s = f"{d:.{places}f}"
    return ("+" + s) if (sign and d > 0) else s


def pct_change(new: float, old: float) -> float:
    return 100.0 * (new / old - 1.0)


def rel_return(num_t: float, num_0: float, den_t: float, den_0: float) -> float:
    """Exact ratio relative return, percent."""
    return 100.0 * ((num_t / num_0) / (den_t / den_0) - 1.0)


def sphb_overheat_adjustment(r20: float, r63: float) -> int:
    """Single highest applicable adjustment, not cumulative."""
    if r20 > 12.0 or r63 > 35.0:
        return 25
    if r20 > 8.0 or r63 > 25.0:
        return 20
    if r20 > 6.0 or r63 > 20.0:
        return 10
    if r20 > 4.0 or r63 > 15.0:
        return 5
    return 0


# --------------------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------------------
def load_closes(path: str) -> dict[str, float]:
    """Cache CSV with header date,low,high,close."""
    out: dict[str, float] = {}
    prev = ""
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            d = row["date"].strip()
            c = (row.get("close") or "").strip()
            if not c:
                continue  # incomplete_row_rule: exclude a row with a missing close
            if d <= prev:
                raise ValueError(f"{path}: dates not strictly increasing at {d}")
            prev = d
            out[d] = float(c)
    return out


def load_fred(path: str) -> dict[str, float]:
    """FRED CSV: header observation_date (or DATE),<SERIES>; '.' or empty = missing."""
    out: dict[str, float] = {}
    prev = ""
    with open(path, newline="") as fh:
        rdr = csv.reader(fh)
        header = next(rdr)
        if len(header) < 2 or header[0].strip().lower() not in ("observation_date", "date"):
            raise ValueError(f"{path}: not a FRED CSV (header {header})")
        for row in rdr:
            if not row:
                continue
            d, v = row[0].strip(), (row[1].strip() if len(row) > 1 else "")
            if d <= prev:
                raise ValueError(f"{path}: dates not strictly increasing at {d}")
            prev = d
            if v in ("", "."):
                continue
            out[d] = float(v)
    return out


def load_inputs(cache: str, vix_csv: str | None = None, vix3m_csv: str | None = None,
                vix3m_level: float | None = None) -> dict:
    """Returns {'data': {sym: {date: close}}, 'sources': {...}, 'rh_vix': dict|None,
    'vix3m': dict|None}."""
    data: dict[str, dict[str, float]] = {}
    for sym in ETFS:
        p = os.path.join(cache, f"{sym}.csv")
        if not os.path.exists(p):
            raise FileNotFoundError(f"missing required series {p}")
        data[sym] = load_closes(p)
    rh_path = os.path.join(cache, "VIX.csv")
    rh_vix = load_closes(rh_path) if os.path.exists(rh_path) else None
    spx_path = os.path.join(cache, "SPX.csv")
    spx = load_closes(spx_path) if os.path.exists(spx_path) else None  # dividend sensitivity only
    sources = {"etf": "Robinhood daily bars"}
    if vix_csv:
        data["VIX"] = load_fred(vix_csv)
        sources["vix"] = "FRED"
        sources["vix_label"] = "FRED VIXCLS"
    else:
        if rh_vix is None:
            raise FileNotFoundError("no VIX source: pass --vix-csv or provide cache VIX.csv")
        data["VIX"] = rh_vix
        sources["vix"] = "Robinhood daily bars"
        sources["vix_label"] = "Robinhood VIX bars (not cross-checked)"
        rh_vix = None  # nothing independent to cross-check against
    vix3m = None
    if vix3m_csv:
        vix3m = load_fred(vix3m_csv)
        sources["vix3m"] = "FRED"
        sources["vix3m_label"] = "FRED VXVCLS, VIX3M stand-in"
    elif vix3m_level is not None:
        sources["vix3m"] = "manual entry"
        sources["vix3m_label"] = "manual --vix3m entry"
    else:
        sources["vix3m"] = None
        sources["vix3m_label"] = "unavailable"
    return {"data": data, "sources": sources, "rh_vix": rh_vix, "vix3m": vix3m,
            "vix3m_level": vix3m_level, "spx": spx}


# --------------------------------------------------------------------------------------
# Audits
# --------------------------------------------------------------------------------------
def audit_dates(data: dict[str, dict[str, float]]) -> dict:
    """Date-alignment audit. Returns findings; never silently drops data."""
    spy = set(data["SPY"])
    etf_mismatch = {}
    for s in ETFS:
        extra = sorted(set(data[s]) - spy)
        missing = sorted(spy - set(data[s]))
        if extra or missing:
            etf_mismatch[s] = {"extra_vs_spy": extra, "missing_vs_spy": missing}
    first = min(spy)
    vix_missing = sorted(spy - set(data["VIX"]))
    vix_extra = sorted(d for d in set(data["VIX"]) - spy if d >= first)
    last = {s: max(data[s]) for s in data}
    common = set.intersection(*(set(data[s]) for s in data))
    return {
        "etf_mismatch": etf_mismatch,
        "vix_missing_vs_spy": vix_missing,
        "vix_off_calendar": vix_extra,
        "last_dates": last,
        "latest_common_asof": max(common),
    }


def cross_check_vix(primary: dict[str, float], secondary: dict[str, float],
                    calendar: list[str], tol: float = XCHECK_TOL) -> dict:
    """Compare a primary VIX series (FRED) against a secondary (Robinhood).

    Reports every date where both have a value and |diff| > tol, every SPY session present in
    one series and missing in the other, and flags a secondary bar whose value matches the
    primary's NEXT or PREVIOUS session (misdated bar)."""
    lo = max(min(primary), min(secondary))
    hi = min(max(primary), max(secondary))
    cal = [d for d in calendar if lo <= d <= hi]
    idx = {d: k for k, d in enumerate(cal)}
    disagree, missing_secondary, missing_primary = [], [], []
    for d in cal:
        a, b = primary.get(d), secondary.get(d)
        if a is None and b is None:
            continue
        if b is None:
            missing_secondary.append(d)
            continue
        if a is None:
            missing_primary.append(d)
            continue
        if abs(a - b) > tol + XCHECK_EPS:
            k = idx[d]
            hint = ""
            for off, word in ((1, "next"), (-1, "previous")):
                j = k + off
                if 0 <= j < len(cal) and cal[j] in primary and abs(primary[cal[j]] - b) <= tol + XCHECK_EPS:
                    hint = f"secondary value equals primary {word} session {cal[j]} (misdated bar)"
                    break
            disagree.append({"date": d, "primary": a, "secondary": b, "diff": b - a,
                             "hint": hint})
    off_cal_secondary = sorted(d for d in secondary if lo <= d <= hi and d not in idx)
    return {"range": (lo, hi), "disagree": disagree, "missing_secondary": missing_secondary,
            "missing_primary": missing_primary, "off_calendar_secondary": off_cal_secondary}


def vix_on_equity_sessions(series: dict[str, float], equity_cal: list[str]) -> tuple[dict, list]:
    """VIX session-calendar rule: keep only US equity sessions (the SPY calendar).

    FRED prints VIXCLS/VXVCLS on some equity holidays; those prints are dropped BEFORE any 5D/20D/
    252D lookback is counted, so a holiday print can never become a lookback base. This is the
    rule that reproduces the 2026-09-17 packet. Returns (filtered series, dropped dates within the
    equity calendar's span)."""
    sessions = set(equity_cal)
    first, last = equity_cal[0], equity_cal[-1]
    kept = {d: v for d, v in series.items() if d in sessions}
    dropped = sorted(d for d in series if d not in sessions and first <= d <= last)
    return kept, dropped


def rows_after_asof(data: dict[str, dict[str, float]], vix3m: dict | None, asof: str,
                    sources: dict) -> list[dict]:
    """Dates after the as-of present in at least one scored series, with the series lacking them."""
    series = dict(data)
    if vix3m is not None:
        series["VIX3M"] = vix3m
    later = sorted({d for v in series.values() for d in v if d > asof})
    out = []
    for d in later:
        have = [s for s in series if d in series[s]]
        lack = [s for s in series if d not in series[s]]
        out.append({"date": d, "have": have, "lack": lack,
                    "lack_etf": [s for s in lack if s in ETFS],
                    "fred_last": {s: max(series[s]) for s in ("VIX", "VIX3M")
                                  if s in series and sources.get(s.lower()) == "FRED"}})
    return out


def rounding_edge(raw: float, prior_score: int) -> dict:
    """Distance from the raw score to the nearer half-up rounding boundary, and what the
    neighbouring integer would mean for the change classification."""
    official = int(round_half_up(clamp(raw)))
    up_b, dn_b = official + 0.5, official - 0.5
    d_up, d_dn = up_b - raw, raw - dn_b
    if d_up <= d_dn:
        boundary, dist, alt = up_b, d_up, official + 1
    else:
        boundary, dist, alt = dn_b, d_dn, official - 1
    return {"raw": raw, "official": official, "boundary": boundary, "distance": dist,
            "alt_score": alt, "within_band": dist <= ROUNDING_EDGE_BAND + 1e-12,
            "delta": official - prior_score, "alt_delta": alt - prior_score,
            "class": change_class(official - prior_score),
            "alt_class": change_class(alt - prior_score)}


def third_friday(year: int, month: int) -> str:
    d = _dt.date(year, month, 1)
    first_fri = 1 + (4 - d.weekday()) % 7
    return _dt.date(year, month, first_fri + 14).isoformat()


def spy_ex_dates(cal: list[str]) -> list[str]:
    """SPDR SPY ex-dividend schedule: third Friday of Mar/Jun/Sep/Dec; if that date is not an
    equity session (e.g. 2026-06-19 Juneteenth), the preceding session."""
    out = []
    years = range(int(cal[0][:4]), int(cal[-1][:4]) + 1)
    for y in years:
        for mo in (3, 6, 9, 12):
            tf = third_friday(y, mo)
            if tf > cal[-1]:
                continue  # scheduled ex-date not yet reached inside this calendar
            prior = [d for d in cal if d <= tf]
            if prior and cal[0] < prior[-1] and prior[-1] not in out:
                out.append(prior[-1])
    return out


def spy_dividend_sensitivity(data: dict, spx: dict | None, m: dict, ratio: float | None,
                             prior_score: int, override: dict | None = None) -> dict | None:
    """Diagnostic only. Back-adjust SPY for dividends inside the 252-session window and recompute
    the TREND inputs (50DMA, 200DMA, 252D drawdown); pairs and volatility unchanged. Each dividend
    is the confirmed amount from `override` or, failing that, estimated from the SPY/SPX close
    ratio drop on the ex-date (SPX is a price index, so the ratio falls by about the dividend
    yield on the ex-date). Returns None when SPX is not cached and no override is given."""
    cal = m["cal"]
    spy = data["SPY"]
    win = cal[-252:]
    exs = [e for e in spy_ex_dates(cal) if win[0] < e <= cal[-1]]
    if not exs or (spx is None and not override):
        return None
    drops, method = {}, {}
    for e in exs:
        p = cal[cal.index(e) - 1]
        if override and e in override:
            drops[e], method[e] = override[e] / spy[p], "confirmed"
        elif spx is not None and e in spx and p in spx:
            drops[e] = 1.0 - (spy[e] / spx[e]) / (spy[p] / spx[p])
            method[e] = "SPY/SPX ratio"
        else:
            return None
    noise = None
    if spx is not None:
        ch = [100.0 * ((spy[b] / spx[b]) / (spy[a] / spx[a]) - 1.0)
              for a, b in zip(win, win[1:]) if b not in drops and a in spx and b in spx]
        if len(ch) > 2:
            mu = sum(ch) / len(ch)
            noise = (sum((x - mu) ** 2 for x in ch) / len(ch)) ** 0.5
    adj = {}
    for d in cal:
        f = 1.0
        for e, dr in drops.items():
            if d < e:
                f *= (1.0 - dr)
        adj[d] = spy[d] * f
    d2 = dict(data)
    d2["SPY"] = adj
    m_adj = compute_metrics(d2, m["asof"])
    mt = dict(m)
    for k in ("spy_vs_50dma", "spy_vs_200dma", "spy_drawdown_mag", "spy_50dma", "spy_200dma"):
        mt[k] = m_adj[k]
    s_adj = score(mt, ratio)
    return {"ex_dates": exs, "drops": drops, "method": method, "noise_sd_pct": noise,
            "amounts": {e: drops[e] * spy[cal[cal.index(e) - 1]] for e in exs},
            "metrics": {k: mt[k] for k in ("spy_vs_50dma", "spy_vs_200dma", "spy_drawdown_mag")},
            "trend": s_adj["trend"], "raw": s_adj["raw"], "score": s_adj["score"],
            "delta": s_adj["score"] - prior_score,
            "class": change_class(s_adj["score"] - prior_score)}


# --------------------------------------------------------------------------------------
# Metric computation
# --------------------------------------------------------------------------------------
def compute_metrics(data: dict[str, dict[str, float]], asof: str) -> dict:
    cal = sorted(d for d in data["SPY"] if d <= asof)  # trading-session calendar (SPY)
    if not cal or cal[-1] != asof:
        raise ValueError(f"as-of {asof} is not an SPY session")
    for s in ["VIX"] + ETFS:
        if asof not in data[s]:
            raise ValueError(f"{s} has no valid close on {asof}")
    i = len(cal) - 1
    m: dict = {"asof": asof, "cal": cal, "lookback_gaps": []}

    # --- VIX (sessions counted on the SPY trading calendar) ---
    vix = data["VIX"]

    def vix_at(k: int) -> tuple[str, float]:
        d = cal[i - k]
        if d not in vix:
            raise ValueError(f"VIX missing at t-{k} session {d}; do not substitute a lookback")
        return d, vix[d]

    m["vix"] = vix[asof]
    d5, v5 = vix_at(5)
    d20, v20 = vix_at(20)
    m["vix_5d_base_date"], m["vix_5d_base"] = d5, v5
    m["vix_20d_base_date"], m["vix_20d_base"] = d20, v20
    m["vix_5d"] = pct_change(m["vix"], v5)
    m["vix_20d"] = pct_change(m["vix"], v20)
    if i < 251:
        raise ValueError("fewer than 252 sessions for the VIX 252-session window")
    win = cal[i - 251: i + 1]
    vals = [(d, vix[d]) for d in win if d in vix]
    missing = [d for d in win if d not in vix]
    if missing:
        m["lookback_gaps"].append(f"VIX missing {len(missing)} of 252 sessions: {missing}")
    lo_d, lo = min(vals, key=lambda t: (t[1], t[0]))
    hi_d, hi = max(vals, key=lambda t: (t[1], t[0]))
    m.update(vix_252_low=lo, vix_252_low_date=lo_d, vix_252_high=hi, vix_252_high_date=hi_d,
             vix_252_window_start=win[0], vix_252_obs=len(vals), vix_252_missing=missing)
    m["vix_52w_position"] = (m["vix"] - lo) / (hi - lo)

    # --- SPY trend ---
    spy_rows = [data["SPY"][d] for d in cal]
    if len(spy_rows) < 252:
        raise ValueError("fewer than 252 SPY sessions")
    m["spy_close"] = spy_rows[-1]
    m["spy_50dma"] = sum(spy_rows[-50:]) / 50.0
    m["spy_200dma"] = sum(spy_rows[-200:]) / 200.0
    m["spy_vs_50dma"] = pct_change(m["spy_close"], m["spy_50dma"])
    m["spy_vs_200dma"] = pct_change(m["spy_close"], m["spy_200dma"])
    w252 = list(zip(cal[-252:], spy_rows[-252:]))
    hd, hv = max(w252, key=lambda t: (t[1], t[0]))
    m["spy_252_high"], m["spy_252_high_date"] = hv, hd
    m["spy_drawdown_signed"] = pct_change(m["spy_close"], hv)
    m["spy_drawdown_mag"] = 100.0 * (1.0 - m["spy_close"] / hv)

    # --- Relative pairs on common dates of both legs ---
    for key, (num, den) in PAIRS.items():
        common = sorted(d for d in set(data[num]) & set(data[den]) if d <= asof)
        if common[-1] != asof:
            raise ValueError(f"{key}: as-of not a common date")
        j = len(common) - 1
        for L in (20, 63):
            if j < L:
                raise ValueError(f"{key}: incomplete {L}-session lookback")
            d0 = common[j - L]
            if d0 != cal[i - L]:
                m["lookback_gaps"].append(
                    f"{key} {L}D base {d0} != SPY-calendar base {cal[i - L]} (missing sessions)")
            m[f"{key}_{L}d"] = rel_return(data[num][asof], data[num][d0],
                                          data[den][asof], data[den][d0])
            m[f"{key}_{L}d_base_date"] = d0
    return m


def term_structure(vix: dict[str, float], vix3m: dict[str, float] | None,
                   vix3m_level: float | None, cal: list[str], asof: str) -> dict | None:
    """VIX/VIX3M on the latest COMMON date on or before the as-of (SPY sessions only)."""
    if vix3m is not None:
        common = [d for d in cal if d <= asof and d in vix and d in vix3m]
        if not common:
            return None
        d = common[-1]
        stale = len([x for x in cal if d < x <= asof])
        out = {"date": d, "vix": vix[d], "vix3m": vix3m[d], "ratio": vix[d] / vix3m[d],
               "stale_sessions": stale, "manual": False}
        if len(common) >= 2:
            p = common[-2]
            out.update(prev_date=p, prev_ratio=vix[p] / vix3m[p])
        return out
    if vix3m_level is not None:
        return {"date": asof, "vix": vix[asof], "vix3m": vix3m_level,
                "ratio": vix[asof] / vix3m_level, "stale_sessions": 0, "manual": True}
    return None


def score(m: dict, ratio: float | None) -> dict:
    """Apply the anchor mappings and weights. ratio=None applies missing_input_policy."""
    sub: dict[str, float] = {}
    sub["vix_level"] = interp(m["vix"], ANCHORS["vix_level"])
    sub["vix_20d_change"] = interp(m["vix_20d"], ANCHORS["vix_20d_change"])
    sub["vix_5d_change"] = interp(m["vix_5d"], ANCHORS["vix_5d_change"])
    sub["vix_52w_position"] = interp(m["vix_52w_position"], ANCHORS["vix_52w_position"])
    weights = dict(VOL_SUBWEIGHTS)
    if ratio is not None:
        sub["vix_term_structure"] = interp(ratio, ANCHORS["vix_term_structure"])
    else:
        # missing_input_policy: remove the 0.25 subweight and renormalize the rest.
        del weights["vix_term_structure"]
    wsum = sum(weights.values())
    vol = clamp(sum(sub[k] * w for k, w in weights.items()) / wsum)
    vol_weights_eff = {k: w / wsum for k, w in weights.items()}

    sub["spy_vs_50dma"] = interp(m["spy_vs_50dma"], ANCHORS["spy_vs_50dma"])
    sub["spy_vs_200dma"] = interp(m["spy_vs_200dma"], ANCHORS["spy_vs_200dma"])
    sub["spy_drawdown"] = interp(m["spy_drawdown_mag"], ANCHORS["spy_drawdown"])
    trend = clamp(sum(sub[k] * w for k, w in TREND_SUBWEIGHTS.items()))

    blended = {}
    for key, (w63, w20) in PAIR_BLEND.items():
        blended[key] = w63 * m[f"{key}_63d"] + w20 * m[f"{key}_20d"]
    for key in ("rsp_spy", "iwm_spy", "xly_xlp"):
        sub[key] = interp(blended[key], ANCHORS[key])
    sphb_base = interp(blended["sphb_splv"], ANCHORS["sphb_splv"])
    adj = sphb_overheat_adjustment(m["sphb_splv_20d"], m["sphb_splv_63d"])
    sub["sphb_splv"] = clamp(sphb_base + adj)
    breadth = clamp(sum(sub[k] * w for k, w in BREADTH_SUBWEIGHTS.items()))

    raw = (TOP_WEIGHTS["volatility"] * vol + TOP_WEIGHTS["trend"] * trend
           + TOP_WEIGHTS["breadth"] * breadth)
    final = int(round_half_up(clamp(raw)))

    # --- Driver-type diagnostic (not part of the score) ---
    # Excess risk above each mapping's floor, attributed to the side of the pivot the input is
    # on, weighted by its effective weight in the final score. The SPHB/SPLV overheating
    # adjustment is attributed to complacency/extension.
    side_inputs = {
        "vix_level": m["vix"], "vix_20d_change": m["vix_20d"], "vix_5d_change": m["vix_5d"],
        "vix_52w_position": m["vix_52w_position"], "spy_vs_50dma": m["spy_vs_50dma"],
        "spy_vs_200dma": m["spy_vs_200dma"], "spy_drawdown": m["spy_drawdown_mag"],
    }
    if ratio is not None:
        side_inputs["vix_term_structure"] = ratio
    eff_w = {k: TOP_WEIGHTS["volatility"] * w for k, w in vol_weights_eff.items()}
    eff_w.update({k: TOP_WEIGHTS["trend"] * w for k, w in TREND_SUBWEIGHTS.items()})
    eff_w.update({k: TOP_WEIGHTS["breadth"] * w for k, w in BREADTH_SUBWEIGHTS.items()})
    sides = {"complacency": 0.0, "stress": 0.0}
    for k, x in side_inputs.items():
        floor = min(a[1] for a in ANCHORS[k])
        pivot, below, above = SIDE_RULES[k]
        sides[below if x < pivot else above] += eff_w[k] * (sub[k] - floor)
    for key in ("rsp_spy", "iwm_spy", "xly_xlp"):
        if blended[key] < 0:
            sides["stress"] += eff_w[key] * (sub[key] - min(a[1] for a in ANCHORS[key]))
    if blended["sphb_splv"] < 0:
        sides["stress"] += eff_w["sphb_splv"] * (sphb_base - min(a[1] for a in ANCHORS["sphb_splv"]))
    sides["complacency"] += eff_w["sphb_splv"] * adj

    return {
        "sub": sub, "vol_weights_eff": vol_weights_eff, "vix_term_ratio": ratio,
        "volatility": vol, "trend": trend, "breadth": breadth, "blended": blended,
        "sphb_base": sphb_base, "sphb_adjustment": adj, "raw": raw, "score": final,
        "sides": sides,
    }


# --------------------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------------------
def band_label(s: int) -> str:
    for lo, hi, lab in SCORE_BANDS:
        if lo <= s <= hi:
            return lab
    raise ValueError(s)


def change_class(delta: int) -> str:
    a = abs(delta)
    if a <= 2:
        return "No material change"
    if a <= 5:
        return "Modest change"
    if a <= 9:
        return "Meaningful change"
    return "Regime-level change"


def _breadth_collapse(m: dict) -> bool:
    return all(m[f"{k}_63d"] < 0 and m[f"{k}_20d"] < 0 for k in ("rsp_spy", "iwm_spy"))


def condition_label(m: dict, term: dict | None) -> tuple[str, str]:
    """separate_condition_labels. Returns (label, qualifier). Checked from most severe."""
    v, ch20, pos = m["vix"], m["vix_20d"], m["vix_52w_position"]
    ratio = term["ratio"] if term else None
    prev = term.get("prev_ratio") if term else None
    two_above_105 = (ratio is not None and prev is not None
                     and ratio > COND["panic_ratio_min"] and prev > COND["panic_ratio_min"])
    trend_break = m["spy_vs_200dma"] < 0
    why = []
    if v > COND["panic_vix_min"]:
        why.append(f"VIX {fmt(v)} > 30")
    if two_above_105:
        why.append("VIX/VIX3M > 1.05 for two closes")
    if trend_break and _breadth_collapse(m):
        why.append("SPY below 200DMA with RSP/SPY and IWM/SPY negative on 20D and 63D")
    if why:
        return "Panic/systemic stress", "; ".join(why)
    if v > COND["fearful_vix_min"] or ch20 > COND["fearful_ch20_min"]:
        q = []
        if m["spy_vs_200dma"] > 0 and (ratio is None or ratio < COND["backwardation"]):
            q.append("fear/event risk, not panic (SPY above 200DMA, term structure in contango)")
        return "Fearful", "; ".join(q)
    comp = []
    if v <= COND["complacent_vix_max"]:
        comp.append(f"VIX {fmt(v)} <= 15.5")
    if ratio is not None and ratio <= COND["complacent_ratio_max"]:
        comp.append(f"VIX/VIX3M {fmt(ratio, 3)} materially below 0.90")
    if pos <= COND["complacent_pos_max"]:
        comp.append(f"52w position {fmt(pos, 3)} in bottom 15%")
    if ch20 <= COND["complacent_ch20_max"]:
        comp.append(f"VIX 20D {fmt(ch20, 2, True)}%")
    if comp:
        return "Complacent", "; ".join(comp)
    lean = []
    if ratio is not None and ratio < COND["normal_contango_min"]:
        lean.append(f"contango steeper than the 0.93-0.98 normal band (VIX/VIX3M {fmt(ratio, 3)})")
    return "Neutral", "; ".join(lean)


def technical_posture(m: dict) -> str:
    a50, a200 = m["spy_vs_50dma"] > 0, m["spy_vs_200dma"] > 0
    neg_core = sum(1 for k in ("rsp_spy", "iwm_spy") if m[f"{k}_63d"] < 0 and m[f"{k}_20d"] < 0)
    neg_all = sum(1 for k in PAIRS if m[f"{k}_63d"] < 0 and m[f"{k}_20d"] < 0)
    if not a200 and neg_core == 2:
        return "Risk-off/broken"
    if not a50 and a200:
        return "Corrective"
    if a50 and a200 and neg_all == 0:
        return "Risk-on"
    return "Mixed"


def driver_type(sides: dict) -> str:
    tot = sides["complacency"] + sides["stress"]
    if tot <= 0:
        return "mixed"
    c = sides["complacency"] / tot
    if c >= 0.65:
        return "complacency/extension"
    if c <= 0.35:
        return "breakdown"
    return "mixed"


# --------------------------------------------------------------------------------------
# Confidence (supervisor_integration.confidence_formula), computed from inputs
# --------------------------------------------------------------------------------------
def data_completeness(data: dict, m: dict, term: dict | None) -> tuple[float, dict]:
    """100 x required observations present / required, on the SPY session calendar."""
    cal, i = m["cal"], len(m["cal"]) - 1
    need, have, detail = 0, 0, {}
    windows = {"VIX": 252, "SPY": 252}
    windows.update({s: 64 for s in ETFS if s != "SPY"})
    for s, n in windows.items():
        req = cal[i - n + 1: i + 1]
        h = sum(1 for d in req if d in data[s])
        need += len(req)
        have += h
        detail[s] = (h, len(req))
    need += 1
    if term is not None:
        have += 1
    detail["VIX3M"] = (1 if term is not None else 0, 1)
    return 100.0 * have / need, detail


def source_quality(sources: dict, defect_groups: set[str] | frozenset = frozenset()) -> tuple[float, dict]:
    """defect_groups: groups ('vix', 'vix3m', 'etf') whose SCORED series has a defect inside its
    active lookback. Only those groups are marked down; a cross-check-only defect is not passed."""
    q = {}
    for grp in ("vix", "vix3m", "etf"):
        src = sources.get(grp)
        if src is None:
            q[grp] = None
            continue
        v = SOURCE_QUALITY[src]
        if grp in defect_groups:
            v -= FEED_DEFECT_PENALTY
        q[grp] = v
    ws = {g: w for g, w in SQ_WEIGHTS.items() if q[g] is not None}
    tot = sum(ws.values())
    return sum(q[g] * w for g, w in ws.items()) / tot, q


def signal_coherence(s: dict, stale: bool) -> tuple[float, str]:
    comps = [s["volatility"], s["trend"], s["breadth"]]
    spread = max(comps) - min(comps)
    same_side = all(c >= COHERENCE["side_line"] for c in comps) or all(
        c < COHERENCE["side_line"] for c in comps)
    dt = driver_type(s["sides"])
    if spread >= COHERENCE["conflict_spread"]:
        val, why = COHERENCE["conflict"], f"strong conflict (component spread {fmt(spread, 1)})"
    elif same_side and dt != "mixed":
        val, why = COHERENCE["agree"], f"agree (all components same side of 50; driver {dt})"
    else:
        val, why = COHERENCE["mixed"], (f"mixed (component spread {fmt(spread, 1)}; "
                                        f"{'same' if same_side else 'split'} side of 50; driver {dt})")
    if stale:
        val, why = min(val, COHERENCE["stale_cap"]), why + "; capped for stale input"
    return val, why


def confidence(dc: float, sq: float, sc: float, vix3m_missing_or_stale: bool,
               approx_pairs: int = 0, lookback_incomplete: bool = False,
               missing_pairs: int = 0) -> tuple[int, float, list[str]]:
    base = 0.40 * dc + 0.30 * sq + 0.30 * sc
    c, ded = base, []
    if vix3m_missing_or_stale:
        c -= 7
        ded.append("-7 VIX3M missing or stale vs VIX")
    if approx_pairs:
        c -= 5 * approx_pairs
        ded.append(f"-{5 * approx_pairs} approximate performance-table pairs")
    if lookback_incomplete:
        c -= 10
        ded.append("-10 required lookback incomplete")
    if missing_pairs >= 2:
        c = min(c, 60)
        ded.append("cap 60 (two or more breadth pairs unavailable)")
    return int(round_half_up(clamp(c))), base, ded


# --------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------
def scored_series_defects(sources: dict, m: dict) -> list[dict]:
    """Known defects in series that are actually SCORED, inside their active lookback."""
    out = []
    win = {"VIX": (m["vix_252_window_start"], m["asof"])}
    group_of = {"VIX": "vix"}
    for (src, ser), defects in KNOWN_FEED_DEFECTS.items():
        grp = group_of.get(ser)
        if grp is None or sources.get(grp) != src:
            continue  # this source is not the scoring source for the series
        lo, hi = win[ser]
        for d, why in sorted(defects.items()):
            if lo <= d <= hi:
                out.append({"group": grp, "series": ser, "source": src, "date": d, "why": why})
    return out


def run(inp: dict, asof: str | None, prior_score: int, prior_asof: str,
        prior_components: dict | None, comparators: bool = True,
        spy_dividends: dict | None = None) -> dict:
    raw_data, sources = inp["data"], inp["sources"]
    audit = audit_dates(raw_data)
    asof = asof or audit["latest_common_asof"]
    # VIX session-calendar rule: equity sessions only; FRED holiday prints dropped before counting.
    equity_cal = sorted(raw_data["SPY"])
    data = dict(raw_data)
    data["VIX"], vix_dropped = vix_on_equity_sessions(raw_data["VIX"], equity_cal)
    vix3m = inp["vix3m"]
    vix3m_dropped: list[str] = []
    if vix3m is not None:
        vix3m, vix3m_dropped = vix_on_equity_sessions(vix3m, equity_cal)
    m = compute_metrics(data, asof)
    term = term_structure(data["VIX"], vix3m, inp["vix3m_level"], m["cal"], asof)
    s = score(m, term["ratio"] if term else None)

    xc = None
    xc_defect = False  # defect in the cross-check-only series (never penalizes scored series)
    if inp["rh_vix"] is not None:
        xc = cross_check_vix(data["VIX"], inp["rh_vix"], equity_cal)
        win_start = m["vix_252_window_start"]
        xc_defect = any(win_start <= e["date"] <= asof for e in xc["disagree"]) or any(
            win_start <= d <= asof for d in xc["missing_secondary"])

    defects = scored_series_defects(sources, m)
    defect_groups = {d["group"] for d in defects}
    stale = term is not None and term["stale_sessions"] > 0
    dc, dc_detail = data_completeness(data, m, term)
    sq, sq_detail = source_quality(sources, defect_groups)
    sc, sc_why = signal_coherence(s, stale)
    conf, base, ded = confidence(dc, sq, sc, vix3m_missing_or_stale=(term is None or stale),
                                 lookback_incomplete=bool(m["lookback_gaps"]))
    r = {"audit": audit, "m": m, "s": s, "term": term, "xc": xc, "sources": sources,
         "xc_defect": xc_defect, "scored_defects": defects,
         "vix_dropped": vix_dropped, "vix3m_dropped": vix3m_dropped,
         "conf": {"value": conf, "base": base, "dc": dc, "dc_detail": dc_detail, "sq": sq,
                  "sq_detail": sq_detail, "sc": sc, "sc_why": sc_why, "deductions": ded},
         "prior_score": prior_score, "prior_asof": prior_asof,
         "prior_components": prior_components, "delta": s["score"] - prior_score,
         "edge": rounding_edge(s["raw"], prior_score),
         "later_rows": rows_after_asof(raw_data, inp["vix3m"], asof, sources)}
    r["div"] = spy_dividend_sensitivity(data, inp.get("spx"), m, term["ratio"] if term else None,
                                        prior_score, spy_dividends)
    r["conf_0917"] = None
    if comparators and asof != VALIDATION_0917["asof"]:
        try:
            r0 = run(inp, VALIDATION_0917["asof"], VALIDATION_0917["score"], VALIDATION_0917["asof"],
                     None, comparators=False, spy_dividends=spy_dividends)
            r["conf_0917"] = r0["conf"]
            r["div_0917"] = r0["div"]
        except (ValueError, KeyError) as exc:  # e.g. cache too short to recompute 09-17
            r["conf_0917_error"] = str(exc)
    return r


# --------------------------------------------------------------------------------------
# Packet text
# --------------------------------------------------------------------------------------
def _move(new: float, old: float) -> str:
    d = round_half_up(new, 1) - round_half_up(old, 1)
    return "rose" if d > 0 else ("fell" if d < 0 else "unchanged at")


def drivers(r: dict) -> list[str]:
    m, s, t, pc = r["m"], r["s"], r["term"], r["prior_components"] or {}
    pa = r["prior_asof"]

    def chg(key: str, label: str) -> tuple[str, float]:
        new = s[key]
        if key in pc:
            mv = _move(new, pc[key])
            if mv == "unchanged at":
                return f"{label} unchanged at {fmt(new, 1)} vs {pa}", 0.0
            return (f"{label} {mv} {fmt(pc[key], 1)} -> {fmt(new, 1)} vs {pa}",
                    TOP_WEIGHTS[key] * (new - pc[key]))
        return f"{label} {fmt(new, 1)}", 0.0

    vt, vw = chg("volatility", "Volatility/complacency")
    ts = (f"VIX/VIX3M {fmt(t['ratio'], 3)} ({t['date']})" if t else "VIX3M unavailable")
    vol_line = (f"{vt}: VIX {fmt(m['vix'])} ({m['asof']}), 5D {fmt(m['vix_5d'], 2, True)}%, "
                f"20D {fmt(m['vix_20d'], 2, True)}%, {fmt(100 * m['vix_52w_position'])}% of 252-session "
                f"range, {ts}")

    bt, bw = chg("breadth", "Breadth fragility")
    worst = sorted(PAIRS, key=lambda k: -s["sub"][k])[:2]
    parts = [f"{PAIR_LABEL[k]} 63D {fmt(m[f'{k}_63d'], 2, True)}% / 20D {fmt(m[f'{k}_20d'], 2, True)}% "
             f"(risk {fmt(s['sub'][k], 1)})" for k in worst]
    highest = max(("volatility", "trend", "breadth"), key=lambda k: s[k]) == "breadth"
    br_line = (f"{bt}{' and is the highest component' if highest else ''}: " + "; ".join(parts))

    tt, tw = chg("trend", "Trend/extension")
    tr_line = (f"{tt}: SPY {fmt(m['spy_close'])} is {fmt(m['spy_vs_50dma'], 2, True)}% vs 50DMA "
               f"{fmt(m['spy_50dma'])}, {fmt(m['spy_vs_200dma'], 2, True)}% vs 200DMA "
               f"{fmt(m['spy_200dma'])}, {fmt(m['spy_drawdown_mag'])}% below 252D high "
               f"{fmt(m['spy_252_high'])} ({m['spy_252_high_date']})")

    lines = [(abs(vw), vol_line), (abs(bw), br_line), (abs(tw), tr_line)]
    if pc:
        lines.sort(key=lambda t_: -t_[0])
    return [ln for _, ln in lines]


def triggers(r: dict) -> list[str]:
    """Exactly three triggers with spec thresholds and guidance ranges."""
    m, t = r["m"], r["term"]
    ratio = f"{fmt(t['ratio'], 3)} on {t['date']}" if t else "N/A"
    up_break = (f"Raise toward {TRIG['risk_off_guidance']} (confirmed risk-off) if VIX/VIX3M exceeds "
                f"1.00 for at least two closes (now {ratio}) while SPY closes below its 50DMA "
                f"(now {fmt(m['spy_vs_50dma'], 2, True)}% vs {fmt(m['spy_50dma'])}) and RSP/SPY and "
                f"IWM/SPY deteriorate together")
    up_melt = (f"Raise toward {TRIG['melt_up_guidance']} (complacent melt-up) if VIX closes at or below "
               f"13 (now {fmt(m['vix'])}) with SPY at least 12% above its 200DMA (now "
               f"{fmt(m['spy_vs_200dma'], 2, True)}%) and SPHB/SPLV above +8% over 20D or +25% over "
               f"63D (now {fmt(m['sphb_splv_20d'], 2, True)}% / {fmt(m['sphb_splv_63d'], 2, True)}%)")
    if m["spy_vs_50dma"] < 0:
        down = (f"Lower on risk-on confirmation if SPY reclaims its 50DMA {fmt(m['spy_50dma'])} "
                f"(now {fmt(m['spy_vs_50dma'], 2, True)}%), VIX falls below about 17 without "
                f"reaching 15.5 or below (now {fmt(m['vix'])}), and RSP/SPY, IWM/SPY and XLY/XLP "
                f"turn positive")
    else:
        down = (f"Lower toward {TRIG['reset_guidance']} (healthy reset) after an orderly 3-7% pullback "
                f"from the 252D high (now {fmt(m['spy_drawdown_mag'])}% below) with VIX about 17-22 "
                f"and no backwardation, SPY above its 200DMA, and RSP/SPY and IWM/SPY 63D intact "
                f"(now {fmt(m['rsp_spy_63d'], 2, True)}% / {fmt(m['iwm_spy_63d'], 2, True)}%)")
    # The breakdown trigger leads when stress is the larger side, otherwise the melt-up one.
    first, second = ((up_break, up_melt) if r["s"]["sides"]["stress"] >= r["s"]["sides"]["complacency"]
                     else (up_melt, up_break))
    return [first, down, second]


def key_metrics(r: dict) -> list[str]:
    m, s, t = r["m"], r["s"], r["term"]
    a = m["asof"]
    out = [
        f"VIX={fmt(m['vix'])} ({a} close; {r['sources']['vix_label']})",
        f"VIX_5D_CHANGE={fmt(m['vix_5d'], 2, True)}pct ({m['vix_5d_base_date']} {fmt(m['vix_5d_base'])} -> {a} close)",
        f"VIX_20D_CHANGE={fmt(m['vix_20d'], 2, True)}pct ({m['vix_20d_base_date']} {fmt(m['vix_20d_base'])} -> {a} close)",
        f"VIX_52W_POSITION={fmt(m['vix_52w_position'], 3)} (low {fmt(m['vix_252_low'])} {m['vix_252_low_date']}, "
        f"high {fmt(m['vix_252_high'])} {m['vix_252_high_date']}; 252 sessions {m['vix_252_window_start']} to {a} close)",
    ]
    if t:
        out += [f"VIX3M={fmt(t['vix3m'])} ({t['date']} close; {r['sources']['vix3m_label']})",
                f"VIX3M_MINUS_VIX={fmt(t['vix3m'] - t['vix'], 2, True)} ({t['date']} close)",
                f"VIX_VIX3M_RATIO={fmt(t['ratio'], 3)} ({t['date']} close; latest common VIX/VIX3M date)"]
    else:
        out += ["VIX3M=N/A (unavailable)", "VIX_VIX3M_RATIO=N/A (unavailable)"]
    out += [
        f"SPY_CLOSE={fmt(m['spy_close'])} ({a} close)",
        f"SPY_50DMA={fmt(m['spy_50dma'])}; SPY_VS_50DMA={fmt(m['spy_vs_50dma'], 2, True)}pct ({a} close)",
        f"SPY_200DMA={fmt(m['spy_200dma'])}; SPY_VS_200DMA={fmt(m['spy_vs_200dma'], 2, True)}pct ({a} close)",
        f"SPY_DRAWDOWN_252D={fmt(m['spy_drawdown_signed'], 2, True)}pct (high {fmt(m['spy_252_high'])} "
        f"{m['spy_252_high_date']}; {a} close)",
    ]
    for k in PAIRS:
        out.append(f"{PAIR_LABEL[k].replace('/', '_')}_20D={fmt(m[f'{k}_20d'], 2, True)}pct "
                   f"({m[f'{k}_20d_base_date']} to {a} close); "
                   f"63D={fmt(m[f'{k}_63d'], 2, True)}pct ({m[f'{k}_63d_base_date']} to {a} close)")
    out += [
        f"VOLATILITY_COMPLACENCY_SCORE={fmt(s['volatility'], 1)} ({a} close)",
        f"TREND_EXTENSION_SCORE={fmt(s['trend'], 1)} ({a} close)",
        f"BREADTH_FRAGILITY_SCORE={fmt(s['breadth'], 1)} (SPHB/SPLV overheating adj +{s['sphb_adjustment']}; {a} close)",
        f"RAW_SCORE={fmt(s['raw'], 3)} -> {s['score']} {band_label(s['score'])} ({a} close)",
        f"CHANGE_FROM_PRIOR={r['delta']:+d} from {r['prior_score']} ({r['prior_asof']} close; "
        f"{change_class(r['delta'])})",
    ]
    return out


def notes(r: dict) -> list[str]:
    m, s, t, c, a = r["m"], r["s"], r["term"], r["conf"], r["audit"]
    cond, qual = condition_label(m, t)
    out = [
        GOVERNANCE_NOTE,
        f"Condition {cond}{(' - ' + qual) if qual else ''}; technical posture {technical_posture(m)}; "
        f"driver type {driver_type(s['sides'])} (weighted excess risk complacency "
        f"{fmt(s['sides']['complacency'], 1)} vs stress {fmt(s['sides']['stress'], 1)}).",
    ]
    e = r["edge"]
    if e["within_band"]:
        ln = (f"Rounding edge: raw {fmt(e['raw'], 3)} is {fmt(e['distance'], 3)} "
              f"{'below' if e['boundary'] > e['raw'] else 'above'} the {fmt(e['boundary'], 1)} boundary "
              f"(official {e['official']}; {e['alt_score']} would make the change {e['alt_delta']:+d} "
              f"'{e['alt_class']}' instead of {e['delta']:+d} '{e['class']}').")
        out.append(ln)
    dv = r.get("div")
    if dv is not None:
        src = ("confirmed amounts" if all(v == "confirmed" for v in dv["method"].values())
               else "estimated from the SPY/SPX close-ratio drop on each ex-date"
               + (f", daily ratio noise sd {fmt(dv['noise_sd_pct'], 2)}pp" if dv["noise_sd_pct"] else ""))
        divs = ", ".join(f"{d} {fmt(dv['amounts'][d])}" for d in dv["ex_dates"])
        ln = (f"Price convention: raw closes, dividends omitted from SPY close/MAs/drawdown and from "
              f"all relative returns (spec adjusted_or_unadjusted_prices; kept for continuity with the "
              f"2026-09-17 calibration, which reproduces on raw closes). Dividend sensitivity "
              f"(diagnostic, trend inputs only; pairs not adjustable from cache): SPY dividends {divs} "
              f"({src}) give SPY vs 50DMA {fmt(dv['metrics']['spy_vs_50dma'], 2, True)}%, vs 200DMA "
              f"{fmt(dv['metrics']['spy_vs_200dma'], 2, True)}%, trend {fmt(dv['trend'], 1)}, raw "
              f"{fmt(dv['raw'], 3)} -> {dv['score']}, change {dv['delta']:+d} '{dv['class']}'")
        d0 = r.get("div_0917")
        if d0 is not None:
            ln += (f"; the same adjustment on {VALIDATION_0917['asof']} gives raw {fmt(d0['raw'], 3)} -> "
                   f"{d0['score']} (published {VALIDATION_0917['score']})")
        out.append(ln + ".")
    else:
        out.append("Price convention: raw closes, dividends omitted from SPY close/MAs/drawdown and from "
                   "all relative returns (continuity with the 2026-09-17 calibration); dividend "
                   "sensitivity not computable (no SPX series or confirmed SPY dividends supplied).")
    out.append("Relative returns are exact ratio returns on common dates of both legs; ETF prices are "
               "Robinhood daily bars.")
    if r["sources"]["vix"] == "FRED":
        out.append("VIX source is FRED VIXCLS for the whole 252-session lookback (provider change from "
                   "Robinhood bars; whole active lookback recalculated per splice rule).")
    else:
        out.append("WARNING: VIX taken from Robinhood bars without a FRED cross-check.")
    dropped = sorted(set(r["vix_dropped"]) | set(r["vix3m_dropped"]))
    out.append("VIX session-calendar rule: VIX and VIX3M sessions counted on US equity sessions only "
               "(SPY calendar); FRED holiday prints dropped before any 5D/20D/252D count"
               + (f" ({', '.join(dropped)})" if dropped else " (none in range)")
               + f"; this is the rule that reproduces {VALIDATION_0917['asof']} (20D base 2026-08-19, +3.69%).")
    xc = r["xc"]
    if xc is not None:
        if xc["disagree"]:
            items = ", ".join(f"{e['date']} RH {fmt(e['secondary'])} vs FRED {fmt(e['primary'])}"
                              + (" [misdated]" if e["hint"] else "") for e in xc["disagree"])
            out.append(f"VIX cross-check FRED vs Robinhood {xc['range'][0]} to {xc['range'][1]}: "
                       f"{len(xc['disagree'])} date(s) differ by >{XCHECK_TOL}: {items}.")
        else:
            out.append(f"VIX cross-check FRED vs Robinhood {xc['range'][0]} to {xc['range'][1]}: "
                       f"no dates differ by >{XCHECK_TOL}.")
        if xc["missing_secondary"] or xc["missing_primary"]:
            out.append(f"VIX cross-check SPY sessions missing: Robinhood "
                       f"{', '.join(xc['missing_secondary']) or 'none'}; FRED "
                       f"{', '.join(xc['missing_primary']) or 'none'}.")
    if t is None:
        out.append("VIX3M unavailable: term-structure subweight removed, volatility renormalized "
                   "(/0.75), -7 confidence.")
    elif t["stale_sessions"]:
        out.append(f"VIX3M stale: ratio dated {t['date']} vs VIX {m['asof']} ({t['stale_sessions']} "
                   f"session(s)); -7 confidence.")
    elif t["manual"]:
        out.append("VIX3M entered manually (not file-sourced); full five-subweight formula applied.")
    else:
        out.append(f"VIX3M = FRED VXVCLS stand-in on the same date as VIX ({t['date']}); full "
                   f"five-subweight volatility formula applied, no VIX3M deduction.")
    ln = (f"As-of {m['asof']} is the latest completed close common to SPY, VIX and all breadth ETFs; "
          f"no intraday rows used.")
    for lr in r["later_rows"]:
        ends: dict[str, list[str]] = {}
        for k, v in lr["fred_last"].items():
            if v < lr["date"]:
                ends.setdefault(v, []).append("VIXCLS" if k == "VIX" else "VXVCLS")
        fl = "; ".join(f"FRED {' and '.join(ns)} end{'s' if len(ns) == 1 else ''} {v}"
                       for v, ns in sorted(ends.items()))
        ln += (f" {lr['date']} excluded although that close exists ({', '.join(lr['have'])} bar present): "
               f"{', '.join(lr['lack_etf']) or 'no ETF'} lack it because {CACHE_DROP_REASON}"
               + (f", and {fl}" if fl else "") + ".")
    out.append(ln)
    if m["lookback_gaps"]:
        out.append("Lookback gaps: " + "; ".join(m["lookback_gaps"]) + ".")
    sqd = c["sq_detail"]
    pen = sorted({d["group"] for d in r["scored_defects"]})
    out.append(
        f"Confidence {c['value']} = 0.40*DataCompleteness {fmt(c['dc'], 1)} + 0.30*SourceQuality "
        f"{fmt(c['sq'], 1)} (VIX {fmt(sqd['vix'], 0) if sqd['vix'] is not None else 'N/A'}, "
        f"VIX3M {fmt(sqd['vix3m'], 0) if sqd['vix3m'] is not None else 'N/A'}, "
        f"ETFs {fmt(sqd['etf'], 0)}) + 0.30*SignalCoherence {fmt(c['sc'], 0)} [{c['sc_why']}]; "
        f"base {fmt(c['base'], 2)} rounds half-up to {c['value']}"
        + (f"; deductions: {', '.join(c['deductions'])}" if c['deductions'] else "; no deductions") + ".")
    if pen:
        out.append("Feed-defect penalty -5 applied to scored series: " + "; ".join(
            f"{d['series']} ({d['source']}) {d['date']}: {d['why']}" for d in r["scored_defects"]) + ".")
    elif r["xc_defect"]:
        out.append("Feed-defect penalty applies only to series used for scoring: the Robinhood VIX "
                   "defect above is in a cross-check-only series (VIX scored from FRED), so no "
                   "SourceQuality markdown on the Robinhood ETF bars (an earlier build of this tool "
                   "marked the ETFs down 5 for it, giving 86).")
    c0 = r.get("conf_0917")
    if c0 is not None:
        cmp_ = ("unchanged" if c["value"] == c0["value"] else f"{c['value'] - c0['value']:+d}")
        out.append(f"Confidence method is new (2026-10-30): computed from inputs per the spec "
                   f"confidence_formula; on {VALIDATION_0917['asof']} data it gives {c0['value']} vs the "
                   f"external agent's published {VALIDATION_0917['confidence']} (agent's sub-inputs "
                   f"unpublished), so this run's {c['value']} compares like-for-like with "
                   f"{c0['value']} ({cmp_}), not with {VALIDATION_0917['confidence']}.")
    elif r.get("conf_0917_error"):
        out.append(f"Confidence method is new (2026-10-30, computed from inputs); {VALIDATION_0917['asof']} "
                   f"comparator not computable: {r['conf_0917_error']}.")
    out.append("Methodology unchanged (fixed 40/30/30, subweights and anchors per 2026-08-24 handoff); "
               "condition label reads 'materially below 0.90' as <=0.85 VIX/VIX3M.")
    return out


def packet(r: dict) -> str:
    m, s, c = r["m"], r["s"], r["conf"]
    lines = ["SUPERVISOR PACKET (COPY/PASTE)", "MODULE: Sentiment and Technicals",
             f"ASOF: {m['asof']} close", f"SCORE_0_100: {s['score']}",
             f"CONFIDENCE_0_100: {c['value']}", "TOP_3_DRIVERS:"]
    lines += [f"- {d}" for d in drivers(r)]
    lines.append("CHANGE_MY_MIND (TRIGGERS):")
    lines += [f"- {t}" for t in triggers(r)]
    lines.append("KEY_METRICS:")
    lines += [f"- {k}" for k in key_metrics(r)]
    lines.append("NOTES:")
    lines += [f"- {n}" for n in notes(r)]
    return "\n".join(lines)


def csv_row(r: dict) -> str:
    m, s, t = r["m"], r["s"], r["term"]
    cond, qual = condition_label(m, t)
    cols = [
        ("asof", m["asof"]), ("sentiment_tech_score", s["score"]), ("confidence", r["conf"]["value"]),
        ("condition_label", cond + (f" ({qual})" if qual else "")),
        ("technical_posture", technical_posture(m)), ("score_driver_type", driver_type(s["sides"])),
        ("volatility_complacency_score", fmt(s["volatility"], 1)),
        ("trend_extension_score", fmt(s["trend"], 1)),
        ("breadth_fragility_score", fmt(s["breadth"], 1)),
        ("vix", fmt(m["vix"])), ("vix_5d_change_pct", fmt(m["vix_5d"])),
        ("vix_20d_change_pct", fmt(m["vix_20d"])),
        ("vix3m", fmt(t["vix3m"]) if t else "N/A"),
        ("vix_minus_vix3m", fmt(t["vix"] - t["vix3m"]) if t else "N/A"),
        ("vix_div_vix3m", fmt(t["ratio"], 3) if t else "N/A"),
        ("vix_52w_position", fmt(m["vix_52w_position"], 3)),
        ("spy_close", fmt(m["spy_close"])), ("spy_50dma", fmt(m["spy_50dma"])),
        ("spy_200dma", fmt(m["spy_200dma"])), ("spy_vs_50dma_pct", fmt(m["spy_vs_50dma"])),
        ("spy_vs_200dma_pct", fmt(m["spy_vs_200dma"])),
        ("spy_drawdown_252d_pct", fmt(m["spy_drawdown_signed"])),
    ]
    for k in PAIRS:
        cols += [(f"{k}_20d_pct", fmt(m[f"{k}_20d"])), (f"{k}_63d_pct", fmt(m[f"{k}_63d"]))]
    dn = (f"VIX {r['sources']['vix_label']}; VIX3M {r['sources']['vix3m_label']}"
          + (f" dated {t['date']}" if t else "") + "; ETFs Robinhood raw closes (dividends omitted)")
    cols += [("score_change", f"{r['delta']:+d}"), ("change_classification", change_class(r["delta"])),
             ("data_notes", dn)]

    def q(v):
        v = str(v)
        return '"' + v.replace('"', '""') + '"' if ("," in v or '"' in v) else v
    return ",".join(c for c, _ in cols) + "\n" + ",".join(q(v) for _, v in cols)


def print_summary(r: dict) -> None:
    m, s, a, t, c = r["m"], r["s"], r["audit"], r["term"], r["conf"]
    print(f"AS-OF: {m['asof']} close")
    print(f"Last row per file: {a['last_dates']}")
    if a["etf_mismatch"]:
        print(f"ETF date mismatches vs SPY: {a['etf_mismatch']}")
    if a["vix_missing_vs_spy"]:
        print(f"VIX sessions missing vs SPY calendar: {a['vix_missing_vs_spy']}")
    if a["vix_off_calendar"]:
        print(f"VIX rows on non-SPY dates (excluded): {a['vix_off_calendar']}")
    xc = r["xc"]
    if xc is not None:
        print(f"VIX CROSS-CHECK FRED vs Robinhood, {xc['range'][0]}..{xc['range'][1]}, tol {XCHECK_TOL}:")
        for e in xc["disagree"]:
            print(f"  DISAGREE {e['date']}: FRED {e['primary']} Robinhood {e['secondary']} "
                  f"(diff {e['diff']:+.2f}) {e['hint']}")
        if not xc["disagree"]:
            print("  no disagreements")
        print(f"  SPY sessions missing in Robinhood VIX: {xc['missing_secondary']}")
        print(f"  SPY sessions missing in FRED VIX: {xc['missing_primary']}")
        print(f"  Robinhood VIX rows on non-SPY dates: {xc['off_calendar_secondary']}")
    print(f"VIX {m['vix']:.2f}  5D {fmt(m['vix_5d'], 4, True)}% (base {m['vix_5d_base_date']} "
          f"{m['vix_5d_base']})  20D {fmt(m['vix_20d'], 4, True)}% (base {m['vix_20d_base_date']} "
          f"{m['vix_20d_base']})")
    print(f"VIX 252-session window {m['vix_252_window_start']}..{m['asof']} obs={m['vix_252_obs']} "
          f"missing={m['vix_252_missing']} low {m['vix_252_low']} ({m['vix_252_low_date']}) "
          f"high {m['vix_252_high']} ({m['vix_252_high_date']}) position {m['vix_52w_position']:.4f}")
    if t:
        print(f"Term structure on {t['date']}: VIX {t['vix']} / VIX3M {t['vix3m']} = {t['ratio']:.4f}"
              f" (stale sessions {t['stale_sessions']}"
              + (f"; prior {t['prev_date']} ratio {t['prev_ratio']:.4f})" if 'prev_ratio' in t else ")"))
    else:
        print("Term structure: VIX3M unavailable (renormalized volatility)")
    print(f"SPY {m['spy_close']:.2f}  50DMA {m['spy_50dma']:.4f} ({fmt(m['spy_vs_50dma'], 4, True)}%)  "
          f"200DMA {m['spy_200dma']:.4f} ({fmt(m['spy_vs_200dma'], 4, True)}%)  252H "
          f"{m['spy_252_high']:.2f} ({m['spy_252_high_date']}) DD {fmt(m['spy_drawdown_signed'], 4, True)}%")
    for k in PAIRS:
        print(f"{k.upper():10s} 20D {fmt(m[f'{k}_20d'], 4, True)}% (base {m[f'{k}_20d_base_date']})  "
              f"63D {fmt(m[f'{k}_63d'], 4, True)}% (base {m[f'{k}_63d_base_date']})  "
              f"blend {fmt(s['blended'][k], 4, True)}%  risk {s['sub'][k]:.2f}")
    print(f"SPHB/SPLV base risk {s['sphb_base']:.2f} + overheating adj {s['sphb_adjustment']}")
    print("Subscores: " + ", ".join(f"{k}={v:.2f}" for k, v in s["sub"].items()))
    print("Volatility weights used: " + ", ".join(f"{k}={v:.4f}" for k, v in s["vol_weights_eff"].items()))
    print(f"Components: volatility {s['volatility']:.4f}  trend {s['trend']:.4f}  breadth {s['breadth']:.4f}")
    print(f"Raw {s['raw']:.4f} -> SCORE {s['score']} ({band_label(s['score'])}); prior "
          f"{r['prior_score']} ({r['prior_asof']}) change {r['delta']:+d} ({change_class(r['delta'])})")
    e = r["edge"]
    print(f"Rounding edge: raw {e['raw']:.4f} vs boundary {e['boundary']} distance {e['distance']:.4f}; "
          f"alt {e['alt_score']} -> change {e['alt_delta']:+d} ({e['alt_class']})")
    dv = r.get("div")
    if dv:
        print("Dividend sensitivity (trend only): " + ", ".join(
            f"{d} drop {100 * dv['drops'][d]:.3f}% ~{dv['amounts'][d]:.2f} [{dv['method'][d]}]"
            for d in dv["ex_dates"]) + f"; noise sd {dv['noise_sd_pct']}")
        print(f"  50DMA {dv['metrics']['spy_vs_50dma']:+.4f}% 200DMA {dv['metrics']['spy_vs_200dma']:+.4f}% "
              f"trend {dv['trend']:.4f} raw {dv['raw']:.4f} -> {dv['score']} ({dv['delta']:+d} {dv['class']})")
    if r.get("div_0917"):
        print(f"  same adjustment on 2026-09-17: raw {r['div_0917']['raw']:.4f} -> {r['div_0917']['score']}")
    for lr in r["later_rows"]:
        print(f"Row after as-of {lr['date']}: present {lr['have']}; absent {lr['lack']}; FRED last {lr['fred_last']}")
    print(f"VIX prints dropped (non-equity sessions): VIX {r['vix_dropped']}; VIX3M {r['vix3m_dropped']}")
    print(f"Scored-series defects: {r['scored_defects'] or 'none'}; cross-check-only defect: {r['xc_defect']}")
    if r.get("conf_0917"):
        print(f"Computed 2026-09-17 confidence comparator: {r['conf_0917']['value']}")
    print(f"Driver sides: complacency {s['sides']['complacency']:.2f} stress "
          f"{s['sides']['stress']:.2f} -> {driver_type(s['sides'])}")
    cond, qual = condition_label(m, t)
    print(f"Condition: {cond}{(' - ' + qual) if qual else ''}; posture: {technical_posture(m)}")
    print(f"Confidence {c['value']}: DC {c['dc']:.2f} {c['dc_detail']}; SQ {c['sq']:.2f} "
          f"{c['sq_detail']}; SC {c['sc']:.1f} ({c['sc_why']}); base {c['base']:.2f}; "
          f"deductions {c['deductions'] or 'none'}")


# --------------------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------------------
def _component_from_published(p: dict) -> dict:
    m = {"vix": p["vix"], "vix_5d": p["vix_5d"], "vix_20d": p["vix_20d"],
         "vix_52w_position": p["vix_52w_position"], "spy_vs_50dma": p["spy_vs_50dma"],
         "spy_vs_200dma": p["spy_vs_200dma"], "spy_drawdown_mag": p["spy_drawdown_mag"]}
    for k in PAIRS:
        m[f"{k}_20d"], m[f"{k}_63d"] = p[f"{k}_20d"], p[f"{k}_63d"]
    return score(m, p["vix_term_ratio"])


def _synthetic_m(**kw) -> dict:
    m = {"vix": 18.0, "vix_20d": 0.0, "vix_5d": 0.0, "vix_52w_position": 0.4,
         "spy_vs_50dma": 1.0, "spy_vs_200dma": 5.0, "spy_drawdown_mag": 2.0}
    for k in PAIRS:
        m[f"{k}_20d"] = m[f"{k}_63d"] = 1.0
    m.update(kw)
    return m


def self_test(cache: str, vix_csv: str | None, vix3m_csv: str | None) -> int:
    fails = 0

    def check(cond: bool, msg: str) -> None:
        nonlocal fails
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails += 1

    # 1. Primitive / sign-convention regression tests (no data needed).
    check(interp(5.0, ANCHORS["vix_level"]) == 100, "VIX below range clamps to endpoint 100")
    check(interp(50.0, ANCHORS["vix_level"]) == 100, "VIX above range clamps to endpoint 100")
    check(interp(20.0, ANCHORS["vix_level"]) == 32.5, "VIX 20 interpolates to 32.5")
    check(interp(12.0, ANCHORS["vix_level"]) > interp(20.0, ANCHORS["vix_level"]),
          "low VIX is higher risk than moderate VIX (complacency)")
    check(interp(1.08, ANCHORS["vix_term_structure"]) > interp(0.96, ANCHORS["vix_term_structure"]),
          "backwardation is higher risk than normal contango")
    check(interp(3.0, ANCHORS["rsp_spy"]) < interp(-3.0, ANCHORS["rsp_spy"]),
          "positive RSP/SPY lowers breadth risk")
    check(interp(-4.0, ANCHORS["sphb_splv"]) > interp(0.0, ANCHORS["sphb_splv"]),
          "negative SPHB/SPLV raises risk")
    check(sphb_overheat_adjustment(9.0, 30.0) == 20, "overheating uses single highest (20)")
    check(sphb_overheat_adjustment(4.0, 15.0) == 0, "overheating thresholds are strict >")
    check(sphb_overheat_adjustment(13.0, 0.0) == 25, "overheating 25 via 20D>12")
    check(int(round_half_up(62.5)) == 63 and int(round_half_up(62.4999)) == 62, "half-up rounding")
    check(fmt(0.125, 2) == "0.13" and fmt(2.675, 2) == "2.68" and fmt(64.75, 1) == "64.8",
          "display rounding is half-up (0.125->0.13, 2.675->2.68, 64.75->64.8)")
    check(fmt(-0.001, 2, True) == "0.00" and fmt(1.5, 2, True) == "+1.50", "signed display, no -0.00")
    check(abs(sum(VOL_SUBWEIGHTS.values()) - 1) < 1e-12 and abs(sum(TREND_SUBWEIGHTS.values()) - 1) < 1e-12
          and abs(sum(BREADTH_SUBWEIGHTS.values()) - 1) < 1e-12, "subweights sum to 1")
    renorm = {k: w / 0.75 for k, w in VOL_SUBWEIGHTS.items() if k != "vix_term_structure"}
    check(abs(renorm["vix_level"] - 0.4) < 1e-12 and abs(sum(renorm.values()) - 1) < 1e-12,
          "VIX3M renormalization divides by 0.75 (only when VIX3M is unavailable)")

    # 2. Condition label thresholds (separate_condition_labels).
    cl = lambda m, ratio=None, prev=None: condition_label(
        m, None if ratio is None else {"ratio": ratio, "prev_ratio": prev})[0]
    check(cl(_synthetic_m(vix=15.5), 0.95) == "Complacent", "VIX 15.50 (at or below 15.5) -> Complacent")
    check(cl(_synthetic_m(vix=15.51), 0.95) == "Neutral", "VIX 15.51, ratio 0.95 -> Neutral")
    check(cl(_synthetic_m(vix=17.0), 0.88) == "Neutral", "ratio 0.88 is not 'materially below 0.90'")
    check(condition_label(_synthetic_m(vix=17.0), {"ratio": 0.88})[1].startswith("contango steeper"),
          "ratio 0.88 reported as a steep-contango lean")
    check(cl(_synthetic_m(vix=17.0), 0.85) == "Complacent", "ratio 0.85 -> Complacent")
    check(cl(_synthetic_m(vix=17.0, vix_52w_position=0.15), 0.95) == "Complacent", "52w pos 0.15 -> Complacent")
    check(cl(_synthetic_m(vix=17.0, vix_20d=-15.0), 0.95) == "Complacent", "20D -15% -> Complacent")
    check(cl(_synthetic_m(vix=20.0), 0.95) == "Neutral", "VIX exactly 20 is not 'above 20'")
    check(cl(_synthetic_m(vix=20.01), 0.95) == "Fearful", "VIX 20.01 -> Fearful")
    check(cl(_synthetic_m(vix=18.0, vix_20d=25.01), 0.95) == "Fearful", "20D +25.01% -> Fearful")
    check(condition_label(_synthetic_m(vix=24.0), {"ratio": 0.97})[1].startswith("fear/event risk"),
          "Fearful above 200DMA in contango is described as fear/event risk, not panic")
    check(cl(_synthetic_m(vix=25.0), 1.06, 1.04) == "Fearful", "one close above 1.05 is not panic")
    check(cl(_synthetic_m(vix=25.0), 1.06, 1.06) == "Panic/systemic stress", "two closes above 1.05 -> panic")
    check(cl(_synthetic_m(vix=30.01), 0.99) == "Panic/systemic stress", "VIX 30.01 -> panic")
    brk = _synthetic_m(vix=18.0, spy_vs_200dma=-2.0, rsp_spy_20d=-1, rsp_spy_63d=-1,
                       iwm_spy_20d=-1, iwm_spy_63d=-1)
    check(cl(brk, 0.95) == "Panic/systemic stress", "trend break + breadth collapse -> panic/stress")

    # 3. Cross-check detects the misdated Robinhood bar pattern (synthetic copy of the audit case).
    cal = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    fred = {"2026-10-20": 17.31, "2026-10-21": 16.68, "2026-10-22": 19.03, "2026-10-23": 19.45,
            "2026-10-24": 19.0}
    rh = {"2026-10-20": 17.31, "2026-10-21": 19.03, "2026-10-23": 19.45}
    xc = cross_check_vix(fred, rh, cal)
    check([e["date"] for e in xc["disagree"]] == ["2026-10-21"] and "next session 2026-10-22"
          in xc["disagree"][0]["hint"], "cross-check flags RH 10-21 as the misdated 10-22 close")
    check(xc["missing_secondary"] == ["2026-10-22"], "cross-check lists RH missing 10-22")
    check(cross_check_vix(fred, {"2026-10-20": 17.36}, cal)["disagree"] == [],
          "cross-check tolerance is >0.05 (0.05 difference passes)")

    # 4. Latest COMMON VIX/VIX3M date and stale handling.
    tvix = {"2026-10-27": 17.52, "2026-10-28": 16.91}
    t1 = term_structure(tvix, {"2026-10-27": 19.57}, None, ["2026-10-27", "2026-10-28"], "2026-10-28")
    check(t1["date"] == "2026-10-27" and t1["stale_sessions"] == 1 and abs(t1["ratio"] - 17.52 / 19.57) < 1e-12,
          "VIX3M one session behind -> ratio on latest common date 10-27, flagged stale")
    t2 = term_structure(tvix, {"2026-10-27": 19.57, "2026-10-28": 19.22}, None,
                        ["2026-10-27", "2026-10-28"], "2026-10-28")
    check(t2["date"] == "2026-10-28" and t2["stale_sessions"] == 0 and "prev_ratio" in t2,
          "same-day VIX3M -> ratio on as-of, previous ratio kept for two-close tests")
    _, _, ded = confidence(100, 85, 70, vix3m_missing_or_stale=True)
    check(ded == ["-7 VIX3M missing or stale vs VIX"], "stale/missing VIX3M deducts 7")

    # 4b. VIX session-calendar rule, rounding edge, ex-date schedule, scored-only penalty.
    kept, dropped = vix_on_equity_sessions({"2026-09-04": 15.2, "2026-09-07": 15.9, "2026-09-08": 15.0},
                                           ["2026-09-04", "2026-09-08"])
    check(dropped == ["2026-09-07"] and "2026-09-07" not in kept,
          "VIX calendar rule drops FRED holiday print 2026-09-07 (Labor Day)")
    ed = rounding_edge(57.178, 63)
    check(abs(ed["distance"] - 0.322) < 1e-9 and ed["alt_score"] == 58 and ed["alt_class"] == "Modest change"
          and ed["class"] == "Meaningful change", "rounding edge 57.178: 0.322 below 57.5; 58 -> -5 Modest")
    ed2 = rounding_edge(62.889, 63)
    check(ed2["official"] == 63 and ed2["boundary"] == 62.5 and abs(ed2["distance"] - 0.389) < 1e-9,
          "rounding edge 62.889: 0.389 above 62.5")
    check(third_friday(2026, 6) == "2026-06-19" and third_friday(2026, 9) == "2026-09-18"
          and third_friday(2025, 12) == "2025-12-19", "third-Friday schedule")
    check(spy_ex_dates(["2026-06-17", "2026-06-18", "2026-06-22", "2026-09-17", "2026-09-18"])
          == ["2026-06-18", "2026-09-18"], "ex-date on a holiday Friday moves to the preceding session")
    check(spy_ex_dates(["2026-09-16", "2026-09-17"]) == [],
          "an ex-date after the calendar end is not pulled back onto the last session")
    src = {"vix": "FRED", "vix3m": "FRED", "etf": "Robinhood daily bars"}
    check(source_quality(src, set())[1]["etf"] == 85.0,
          "cross-check-only VIX defect does not mark down the Robinhood ETF bars")
    check(source_quality({**src, "vix": "Robinhood daily bars"}, {"vix"})[1] ==
          {"vix": 80.0, "vix3m": 85.0, "etf": 85.0}, "scored-series defect marks down only its own group")
    m_fake = {"vix_252_window_start": "2025-10-28", "asof": "2026-10-28"}
    check(scored_series_defects(src, m_fake) == [], "FRED-scored VIX: Robinhood VIX defect not a scored defect")
    check([d["date"] for d in scored_series_defects({**src, "vix": "Robinhood daily bars"}, m_fake)]
          == ["2026-10-21"], "Robinhood-scored VIX: known 2026-10-21 misdated bar is a scored defect")

    # 5. Anchor mapping applied to the agent of record's OWN published 09-17 metrics.
    v = VALIDATION_0917
    pub = _component_from_published(v["metrics"])
    for k in ("volatility", "trend", "breadth"):
        check(abs(pub[k] - v["components"][k]) <= COMPONENT_TOL,
              f"09-17 published metrics -> {k} {pub[k]:.2f} vs published {v['components'][k]}")
    check(pub["score"] == v["score"], f"09-17 published metrics -> score {pub['score']} == {v['score']}")

    # 6. Recompute 09-17 from the raw cache (and FRED files when supplied) and compare.
    if not cache or not os.path.isdir(cache):
        print("FAIL --cache DIR (or SENTIMENT_TECH_CACHE) is required for the 09-17 data validation")
        return 1
    inp = load_inputs(cache, vix_csv, vix3m_csv, None if vix3m_csv else v["vix3m"])
    r = run(inp, v["asof"], v["score"], v["asof"], v["components"])
    m, s, t = r["m"], r["s"], r["term"]
    p = v["metrics"]
    print(f"INFO 09-17 sources: VIX {inp['sources']['vix_label']}; VIX3M {inp['sources']['vix3m_label']}")
    check(abs(m["vix"] - p["vix"]) < 1e-9, f"VIX {m['vix']} == {p['vix']}")
    check(t is not None and abs(t["vix3m"] - v["vix3m"]) < 1e-9 and t["date"] == v["asof"],
          f"VIX3M {t and t['vix3m']} on {t and t['date']} == published {v['vix3m']}")
    check(abs(round(t["ratio"], 3) - p["vix_term_ratio"]) < 1e-9,
          f"VIX/VIX3M {t['ratio']:.4f} rounds to published {p['vix_term_ratio']}")
    check(abs(m["spy_close"] - p["spy_close"]) < 1e-9, f"SPY {m['spy_close']} == {p['spy_close']}")
    check(abs(m["vix_5d"] - p["vix_5d"]) < 0.01, f"VIX 5D {m['vix_5d']:.4f} vs {p['vix_5d']}")
    check(abs(m["vix_20d"] - p["vix_20d"]) < 0.01, f"VIX 20D {m['vix_20d']:.4f} vs {p['vix_20d']}")
    for k, tol in (("vix_52w_position", 0.005), ("spy_vs_50dma", 0.05), ("spy_vs_200dma", 0.05),
                   ("spy_drawdown_mag", 0.01)) + tuple((f"{q}_{L}d", 0.01) for q in PAIRS for L in (20, 63)):
        check(abs(m[k] - p[k]) <= tol, f"{k} {m[k]:.4f} vs published {p[k]} (tol {tol})")
    for k in ("volatility", "trend", "breadth"):
        check(abs(s[k] - v["components"][k]) <= COMPONENT_TOL,
              f"09-17 cache -> {k} {s[k]:.2f} vs published {v['components'][k]} (tol {COMPONENT_TOL})")
    check(s["score"] == v["score"],
          f"09-17 cache -> score {s['score']} (raw {s['raw']:.3f}) == published {v['score']}")
    print(f"INFO known residual: cache raw {s['raw']:.3f} vs published-component raw "
          f"{0.4 * 69.3 + 0.3 * 52.5 + 0.3 * 64.8:.3f} (two-decimal published inputs); both round to 63")
    print(f"INFO 09-17 computed confidence {r['conf']['value']} vs published {v['confidence']} "
          f"(DC {r['conf']['dc']:.1f}, SQ {r['conf']['sq']:.1f}, SC {r['conf']['sc']:.0f}); "
          f"agent of record did not publish its confidence sub-inputs - residual reported, not tuned")
    if inp["sources"]["vix"] == "FRED":
        fred_cal = sorted(d for d in inp["data"]["VIX"] if d <= v["asof"])
        fb = fred_cal[len(fred_cal) - 1 - 20]
        fred_20d = pct_change(inp["data"]["VIX"][v["asof"]], inp["data"]["VIX"][fb])
        check(m["vix_20d_base_date"] == "2026-08-19" and fb != m["vix_20d_base_date"]
              and abs(fred_20d - p["vix_20d"]) > 1.0,
              f"equity-session rule reproduces 09-17 VIX 20D (base {m['vix_20d_base_date']}, "
              f"{m['vix_20d']:+.2f}%); FRED-calendar count would give base {fb}, {fred_20d:+.2f}%")
    check(r["scored_defects"] == [] and r["conf"]["sq_detail"]["etf"] == 85.0,
          "09-17: no scored-series defect, ETF SourceQuality 85")
    if r.get("div"):
        print(f"INFO 09-17 dividend sensitivity (trend only): raw {r['div']['raw']:.3f} -> {r['div']['score']} "
              f"(raw-close official {s['score']})")
    s_nr = score(m, None)
    print(f"INFO 09-17 renormalized (no VIX3M) would give volatility {s_nr['volatility']:.2f}, score "
          f"{s_nr['score']} (raw {s_nr['raw']:.3f}) vs five-subweight {s['score']}")
    print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILURE(S)'}")
    return 0 if fails == 0 else 1


def _parse_components(sv: str | None) -> dict | None:
    if not sv:
        return None
    parts = [float(x) for x in sv.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("--prior-components needs volatility,trend,breadth")
    return dict(zip(("volatility", "trend", "breadth"), parts))


def _parse_dividends(sv: str | None) -> dict | None:
    if not sv:
        return None
    out = {}
    for part in sv.split(","):
        d, amt = part.split(":")
        _dt.date.fromisoformat(d.strip())
        out[d.strip()] = float(amt)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=DEFAULT_CACHE, help="ETF (and Robinhood VIX) cache directory")
    ap.add_argument("--vix-csv", help="FRED VIXCLS CSV (scoring source; Robinhood VIX becomes cross-check)")
    ap.add_argument("--vix3m-csv", help="FRED VXVCLS CSV (VIX3M stand-in)")
    ap.add_argument("--vix3m", type=float, help="manual VIX3M close dated at the as-of (if no --vix3m-csv)")
    ap.add_argument("--asof")
    ap.add_argument("--prior-score", type=int, default=VALIDATION_0917["score"])
    ap.add_argument("--prior-asof", default=VALIDATION_0917["asof"])
    ap.add_argument("--prior-components", default="69.3,52.5,64.8",
                    help="prior volatility,trend,breadth components (published 2026-09-17 default)")
    ap.add_argument("--spy-dividends",
                    help="confirmed SPY dividends for the sensitivity, DATE:AMOUNT[,DATE:AMOUNT...] "
                         "(ex-dates); default estimates them from the cached SPX series")
    ap.add_argument("--csv-out", help="write the one-row CSV here")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test(a.cache, a.vix_csv, a.vix3m_csv)
    if not a.cache:
        ap.error("--cache is required")
    if not a.vix_csv:
        print("WARNING: no --vix-csv; scoring VIX from Robinhood bars (known misdated bar 2026-10-21)",
              file=sys.stderr)
    inp = load_inputs(a.cache, a.vix_csv, a.vix3m_csv, a.vix3m)
    r = run(inp, a.asof, a.prior_score, a.prior_asof, _parse_components(a.prior_components),
            spy_dividends=_parse_dividends(a.spy_dividends))
    print_summary(r)
    print("\n```")
    print(packet(r))
    print("```\n")
    row = csv_row(r)
    print(row)
    if a.csv_out:
        with open(a.csv_out, "w") as fh:
            fh.write(row + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
