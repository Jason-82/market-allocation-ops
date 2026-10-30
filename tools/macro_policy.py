#!/usr/bin/env python3
"""Macro & Policy (US equities) module calculator (stdlib only).

Implements supervisor/modules/macro_policy.yaml:
  current_scoring_methodology (30/25/15/15/15 formula, every subweight, anchor table and rule),
  macro_regime_label_rules, data_pipeline.asof_selection_rule / incomplete_row_rule,
  calculations.formulas, supervisor_integration.confidence_definition and output_contract.

Data: FRED fredgraph.csv downloads (current vintage only; fredgraph ignores vintage_date, so
real-time vintages are unavailable). Facts that FRED does not carry (FOMC action/vote/guidance,
SEP medians, the release-text payroll revision, the GDP estimate vintage, catalyst dates) come
from a --facts JSON file and are treated as secondary-sourced.

Usage:
    python3 tools/macro_policy.py --fetch DIR
    python3 tools/macro_policy.py --cache DIR --facts FACTS.json [--asof YYYY-MM-DD]
        [--validate] [--json OUT.json]
    python3 tools/macro_policy.py --self-test [--cache DIR [--facts JSON]]

Facts keys added for the 2025-10 CPI gap and narrative flags (per run):
    cpi_yoy_override / core_cpi_yoy_override  {"YYYY-MM": {"value", "source", "date"}}: official BLS
        12-month % change, used only for a month whose FRED t-12 NSA level is blank.
    fed.guidance_statement  statement/press-conference-only classification; the spec's hike-voter
        rule (>= 2 voters prefer a hike -> hawkish) is applied on top in code.
    fed.minutes_released / fed.minutes_date, gdp_vintage.headline_corroborated,
    release_dates_source / release_dates_primary_verified; top level: known_data_gaps,
    primary_hosts_unreachable.

Decision support only; not financial advice.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import ssl
import sys
import urllib.request

# --------------------------------------------------------------------------------------
# Spec constants (supervisor/modules/macro_policy.yaml). Keep in sync with the spec.
# --------------------------------------------------------------------------------------
WEIGHTS = {
    "InflationRisk": 0.30,
    "LaborRecessionRisk": 0.25,
    "GrowthRisk": 0.15,
    "RatesCurveRisk": 0.15,
    "FedPolicyRisk": 0.15,
}

SUBWEIGHTS = {
    "InflationRisk": {
        "core_pce_yoy": 0.30, "core_pce_3m_annualized": 0.25, "core_cpi_yoy": 0.15,
        "core_cpi_3m_annualized": 0.10, "headline_inflation_shock": 0.10,
        "inflation_direction": 0.10,
    },
    "LaborRecessionRisk": {
        "sahm_rule": 0.30, "unemployment_3m_trend": 0.15, "payroll_3m_average": 0.25,
        "payroll_revisions": 0.10, "participation_and_employment_ratio": 0.10,
        "household_establishment_divergence": 0.10,
    },
    "GrowthRisk": {
        "real_gdp_qoq_saar": 0.35,
        "real_final_sales_to_private_domestic_purchasers_saar": 0.45,
        "real_pce_3m_annualized": 0.10, "gdp_revision_and_two_quarter_trend": 0.10,
    },
    "RatesCurveRisk": {
        "absolute_10y_yield": 0.35, "spread_10y_3m": 0.25, "spread_10y_2y": 0.10,
        "spread_2y_3m": 0.20, "recent_10y_3m_inversion_legacy": 0.10,
    },
    "FedPolicyRisk": {
        "policy_rate_gap_to_long_run_neutral": 0.25, "recent_policy_action": 0.20,
        "statement_press_conference_minutes_guidance": 0.25,
        "vote_and_dissent_balance": 0.15, "sep_rate_path_change": 0.15,
    },
}

ANCHORS = {
    "core_pce_yoy": [(1.5, 10), (2.0, 20), (2.5, 35), (3.0, 55), (3.5, 75), (4.0, 90), (5.0, 100)],
    "core_pce_3m_annualized": [(0.0, 0), (1.5, 10), (2.0, 20), (2.5, 35), (3.0, 50), (4.0, 75), (6.0, 100)],
    "core_cpi_yoy": [(1.5, 10), (2.0, 20), (2.5, 35), (3.0, 55), (3.5, 75), (4.0, 90), (5.0, 100)],
    "core_cpi_3m_annualized": [(0.0, 0), (1.5, 10), (2.0, 20), (2.5, 35), (3.0, 50), (4.0, 75), (6.0, 100)],
    "headline_cpi_or_pce_yoy": [(1.5, 10), (2.0, 20), (3.0, 45), (4.0, 70), (5.0, 90), (6.0, 100)],
    "sahm_rule_pp": [(-0.10, 5), (0.10, 15), (0.20, 30), (0.30, 45), (0.40, 65), (0.50, 80), (0.75, 100)],
    "unemployment_3m_average_change_pp": [(-0.30, 10), (-0.10, 20), (0.00, 30), (0.10, 45), (0.20, 65), (0.40, 90), (0.60, 100)],
    "payroll_3m_average_thousands": [(-100, 100), (0, 90), (25, 75), (50, 60), (75, 45), (100, 30), (150, 15), (200, 5)],
    "combined_latest_two_month_payroll_revision_thousands": [(-200, 100), (-150, 90), (-100, 75), (-50, 50), (0, 25), (50, 10)],
    "six_month_participation_stress_pp": [(0.00, 20), (0.20, 35), (0.40, 55), (0.70, 80), (1.00, 100)],
    "real_gdp_qoq_saar": [(-2.0, 100), (0.0, 80), (0.5, 70), (1.0, 60), (1.5, 50), (2.0, 40), (3.0, 20), (4.0, 10), (5.0, 5)],
    "private_domestic_final_sales_saar": [(-2.0, 100), (0.0, 85), (0.5, 75), (1.0, 65), (1.5, 55), (2.0, 40), (3.0, 20), (4.0, 10), (5.0, 5)],
    "real_pce_3m_annualized": [(-2.0, 100), (0.0, 85), (1.0, 65), (2.0, 40), (3.0, 20), (4.0, 10)],
    "latest_estimate_minus_advance_estimate_pp": [(-2.0, 100), (-1.0, 85), (-0.5, 65), (0.0, 40), (0.5, 20), (1.0, 10)],
    "dgs10_percent": [(2.5, 10), (3.0, 20), (3.5, 35), (4.0, 50), (4.5, 70), (5.0, 85), (6.0, 100)],
    "spread_10y_3m_pp": [(-1.50, 100), (-1.00, 90), (-0.50, 75), (0.00, 55), (0.50, 30), (1.00, 20), (2.00, 10)],
    "spread_10y_2y_pp": [(-1.00, 90), (-0.50, 70), (0.00, 50), (0.50, 30), (1.00, 20), (2.00, 10)],
    "months_since_last_negative_10y_3m": [(0, 80), (6, 70), (12, 50), (18, 25), (24, 0)],
    "target_midpoint_minus_sep_long_run_median_pp": [(-1.00, 30), (0.00, 40), (0.50, 55), (1.00, 70), (2.00, 90), (3.00, 100)],
    "current_year_sep_median_change_vs_prior_sep_pp": [(-1.00, 10), (-0.50, 25), (0.00, 50), (0.50, 75), (1.00, 100)],
}

ACTION_SCORES = {
    "orderly_cut_with_disinflation_and_stable_labor": 25, "dovish_hold": 35,
    "balanced_hold": 50, "hawkish_hold": 70, "twenty_five_bp_hike": 90,
    "emergency_cut_or_intermeeting_action": 90,
}
CUT_ACTIONS = {"orderly_cut_with_disinflation_and_stable_labor", "emergency_cut_or_intermeeting_action"}
GUIDANCE_SCORES = {
    "clearly_dovish": 25, "balanced_data_dependent": 50,
    "hawkish_or_higher_for_longer": 75, "explicit_likely_tightening": 90,
}

CONF_BASE = 95
CONF_PENALTIES = {
    "advance_gdp_only": 5,
    "combined_two_month_payroll_revision_at_or_below_minus_100k": 5,
    "material_household_establishment_divergence": 5,
    "cpi_and_pce_direction_conflict": 5,
    "pce_more_than_two_reference_months_behind_cpi": 5,
    "required_submetric_missing_but_reweightable": 5,
    "component_more_than_30_percent_missing": 10,
    "source_discrepancy_not_fully_resolved": 5,
}
MISSING_WEIGHT_LIMIT = 0.30

# thresholds.stable_component_alerts and guidance_classification (narrative alerts; not scored).
THRESH = {
    "core_pce_3m_raise": 3.5, "core_pce_yoy_lower": 3.0, "core_pce_3m_lower": 2.5,
    "sahm_lower": 0.20, "sahm_caution": 0.35, "sahm_recession": 0.50,
    "payroll_alert_k": 50.0, "payroll_lower_k": 100.0, "revision_penalty_k": -100.0,
    "pfs_lower": 3.0, "dgs10_raise": 4.75, "dgs10_lower": 4.25, "spread_2y3m_raise": 0.50,
    "hike_voters_raise": 3, "hawkish_hike_voters": 2, "divergence_material_k": 750.0,
}
GUIDANCE_LABELS = {
    "clearly_dovish": "dovish", "balanced_data_dependent": "balanced/data-dependent",
    "hawkish_or_higher_for_longer": "hawkish", "explicit_likely_tightening": "explicit likely tightening",
}
ACTION_LABELS = {
    "orderly_cut_with_disinflation_and_stable_labor": "orderly cut", "dovish_hold": "dovish hold",
    "balanced_hold": "balanced hold", "hawkish_hold": "hawkish hold", "twenty_five_bp_hike": "25bp hike",
    "emergency_cut_or_intermeeting_action": "emergency/intermeeting action",
}
HIKE_ACTIONS = {"twenty_five_bp_hike"}
# Series whose t-12 level is needed for CPI YoY (NSA, per adjusted_or_unadjusted_prices.cpi_yoy).
CPI_SERIES = ("CPIAUCNS", "CPILFENS", "CPIAUCSL", "CPILFESL")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]

# Implementation choices the spec leaves open (disclosed in NOTES / methodology deviations).
DIVERGENCE_FLAT_K = 100.0   # a 3-month survey change smaller than 100k is "approximately flat"
LEGACY_DAYS_PER_MONTH = 365.25 / 12.0

SERIES = [
    "DGS10", "DGS2", "DGS3MO", "UNRATE", "SAHMCURRENT", "PAYEMS", "CE16OV", "CIVPART",
    "EMRATIO", "LNS11300060", "LNS12300060", "CPIAUCSL", "CPIAUCNS", "CPILFESL", "CPILFENS",
    "PCEPI", "PCEPILFE", "PCEC96", "A191RL1Q225SBEA", "PB0000031Q225SBEA",
    "LB0000031Q020SBEA", "DFEDTARU", "DFEDTARL",
]
DAILY = {"DGS10", "DGS2", "DGS3MO", "DFEDTARU", "DFEDTARL"}
QUARTERLY = {"A191RL1Q225SBEA", "PB0000031Q225SBEA", "LB0000031Q020SBEA"}

# Embedded 2026-09-16 rates facts for the offline self-test (from FRED, unrevised daily H.15).
SELFTEST_0916 = {"DGS10": 5.01, "DGS3MO": 4.14, "DGS2": 4.74,
                 "last_negative_10y_3m": "2025-10-16", "published_rates_curve": 58.7}


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def clamp(x, lo, hi):
    return min(max(x, lo), hi)


def lininterp(x, anchors):
    if x <= anchors[0][0]:
        return float(anchors[0][1])
    if x >= anchors[-1][0]:
        return float(anchors[-1][1])
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    raise AssertionError("unreachable")


def sign_eps(x, eps):
    if x < -eps:
        return -1
    if x > eps:
        return 1
    return 0


def round_half_up(x, nd=0):
    q = 10 ** nd
    return math.floor(x * q + 0.5) / q


def ym_shift(ym, k):
    y, m = int(ym[:4]), int(ym[5:7])
    idx = y * 12 + (m - 1) + k
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def quarter_of(date):
    return f"{date[:4]}-Q{(int(date[5:7]) - 1) // 3 + 1}"


def q_shift(q, k):
    y, n = int(q[:4]), int(q[-1])
    idx = y * 4 + (n - 1) + k
    return f"{idx // 4:04d}-Q{idx % 4 + 1}"


def fmt(x, nd=2):
    return "n/a" if x is None else f"{x:.{nd}f}"


def month_name(ym):
    return f"{MONTHS[int(ym[5:7]) - 1]} {ym[:4]}"


def rel(x, thr, near):
    """Where a metric sits relative to an alert line (narrative wording, computed)."""
    if x is None:
        return "n/a vs"
    if x > thr:
        return "just above" if x - thr <= near else "above"
    if x < thr:
        return "just below" if thr - x <= near else "below"
    return "at"


def article(word):
    return "an" if word[:1].upper() in "AEIOU" else "a"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------
def fetch(dirpath):
    os.makedirs(dirpath, exist_ok=True)
    ctx = ssl.create_default_context()
    ca = "/root/.ccr/ca-bundle.crt"
    for sid in SERIES:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
        try:
            raw = urllib.request.urlopen(url, context=ctx, timeout=60).read()
        except ssl.SSLError:
            if not os.path.exists(ca):
                raise
            raw = urllib.request.urlopen(
                url, context=ssl.create_default_context(cafile=ca), timeout=60).read()
        with open(os.path.join(dirpath, sid + ".csv"), "wb") as fh:
            fh.write(raw)
        print(f"fetched {sid}: {len(raw.splitlines()) - 1} rows")


class Data:
    def __init__(self, dirpath=None):
        self.daily, self.monthly, self.quarterly = {}, {}, {}
        for sid in (SERIES if dirpath else ()):
            path = os.path.join(dirpath, sid + ".csv")
            if not os.path.exists(path):
                continue
            rows = {}
            with open(path, newline="") as fh:
                rd = csv.reader(fh)
                next(rd)
                for r in rd:
                    if len(r) < 2:
                        continue
                    v = None if r[1].strip() in (".", "") else float(r[1])
                    rows[r[0]] = v
            if sid in DAILY:
                self.daily[sid] = rows
            elif sid in QUARTERLY:
                self.quarterly[sid] = {quarter_of(d): v for d, v in rows.items()}
            else:
                self.monthly[sid] = {d[:7]: v for d, v in rows.items()}

    def m(self, sid, ym):
        return self.monthly.get(sid, {}).get(ym)

    def clone(self):
        """Copy whose monthly series can be extended or edited in memory (scenario runs);
        daily and quarterly series are shared read-only."""
        c = Data()
        c.daily, c.quarterly = self.daily, self.quarterly
        c.monthly = {k: dict(v) for k, v in self.monthly.items()}
        return c

    def gaps(self, sid, since=None):
        """Blank or absent months strictly inside a monthly series' observed range."""
        ser = self.monthly.get(sid) or {}
        have = sorted(k for k, v in ser.items() if v is not None)
        if not have:
            return []
        out, ym = [], have[0]
        while ym < have[-1]:
            if ser.get(ym) is None and (since is None or ym >= since):
                out.append(ym)
            ym = ym_shift(ym, 1)
        return out

    def q(self, sid, q):
        return self.quarterly.get(sid, {}).get(q)

    def latest_common_treasury(self, asof=None):
        a, b, c = (self.daily[s] for s in ("DGS10", "DGS2", "DGS3MO"))
        dates = sorted(d for d in a if a[d] is not None and b.get(d) is not None
                       and c.get(d) is not None and (asof is None or d <= asof))
        return dates[-1]

    def last_negative_10y_3m(self, asof):
        a, c = self.daily["DGS10"], self.daily["DGS3MO"]
        neg = [d for d in a if d <= asof and a[d] is not None and c.get(d) is not None
               and round(a[d] - c[d], 2) < 0]
        return max(neg) if neg else None

    def latest_month(self, sid):
        return max(k for k, v in self.monthly[sid].items() if v is not None)

    def latest_quarter(self, sid):
        return max(k for k, v in self.quarterly[sid].items() if v is not None)


# --------------------------------------------------------------------------------------
# Component calculations
# --------------------------------------------------------------------------------------
def combine(component, subs, prior=None):
    """Weighted component score with incomplete_row_rule.missing_component_weight."""
    w = SUBWEIGHTS[component]
    missing = [k for k in w if subs.get(k) is None]
    miss_w = sum(w[k] for k in missing)
    flags = []
    if miss_w == 0:
        return sum(w[k] * subs[k] for k in w), missing, flags
    if miss_w <= MISSING_WEIGHT_LIMIT + 1e-12:
        avail = 1.0 - miss_w
        flags.append("required_submetric_missing_but_reweightable")
        return sum(w[k] * subs[k] for k in w if k not in missing) / avail, missing, flags
    flags.append("component_more_than_30_percent_missing")
    return (prior if prior is not None else None), missing, flags


def score_2y3m(s):
    s = round(s, 2)
    if s < -0.75:
        return 70.0
    if s < -0.25:
        return 55.0
    if s < -0.10:
        return 35.0
    if s <= 0.20:
        return 25.0
    if s <= 0.50:
        return 55.0
    if s <= 0.75:
        return 70.0
    return 85.0


def rates_block(dgs10, dgs3mo, dgs2, asof, last_neg):
    s103 = round(dgs10 - dgs3mo, 2)
    s102 = round(dgs10 - dgs2, 2)
    s23 = round(dgs2 - dgs3mo, 2)
    if s103 < 0:
        months = 0.0
    elif last_neg is None:
        months = 24.0
    else:
        months = (dt.date.fromisoformat(asof) - dt.date.fromisoformat(last_neg)).days / LEGACY_DAYS_PER_MONTH
    subs = {
        "absolute_10y_yield": lininterp(dgs10, ANCHORS["dgs10_percent"]),
        "spread_10y_3m": lininterp(s103, ANCHORS["spread_10y_3m_pp"]),
        "spread_10y_2y": lininterp(s102, ANCHORS["spread_10y_2y_pp"]),
        "spread_2y_3m": score_2y3m(s23),
        "recent_10y_3m_inversion_legacy": lininterp(months, ANCHORS["months_since_last_negative_10y_3m"]),
    }
    score, missing, flags = combine("RatesCurveRisk", subs)
    raw = {"DGS10": dgs10, "DGS3MO": dgs3mo, "DGS2": dgs2, "spread_10y_3m": s103,
           "spread_10y_2y": s102, "spread_2y_3m": s23, "last_negative_10y_3m": last_neg,
           "months_since_last_negative_10y_3m": months}
    return score, subs, raw, flags


def _pct(a, b):
    return None if a is None or b is None else 100.0 * (a / b - 1.0)


def _ann3(a, b):
    return None if a is None or b is None else 100.0 * ((a / b) ** 4 - 1.0)


def _cpi_yoy(d, sid, ym, ovr, r, key):
    """CPI YoY from NSA levels; for a month whose t-12 level is blank (e.g. the 2025-10
    shutdown gap) use the official BLS YoY supplied in the facts, never a synthetic level."""
    a, b = d.m(sid, ym), d.m(sid, ym_shift(ym, -12))
    if a is not None and b is not None:
        r["yoy_basis"][key] = "computed"
        return _pct(a, b)
    e = (ovr or {}).get(ym)
    if b is None and e is not None:
        lack = [f for f in ("value", "source", "date") if e.get(f) in (None, "")]
        if lack:
            raise ValueError(f"{key} override for {ym} lacks {lack}; an official BLS YoY needs a source URL and date")
        r["yoy_basis"][key] = f"official YoY override {float(e['value'])} ({e['source']}, {e['date']})"
        r["yoy_overrides_used"].append({"metric": key, "month": ym, "value": float(e["value"]),
                                        "source": e["source"], "date": e["date"]})
        return float(e["value"])
    why = f"{sid} t-12 level {ym_shift(ym, -12)} blank" if b is None else f"{sid} level {ym} blank"
    r["yoy_basis"][key] = "missing: " + why + ("" if b is not None else ", no official YoY override supplied")
    r["yoy_missing"].append({"metric": key, "month": ym, "why": why})
    return None


def inflation_block(d, cpi_p, pce_p, prior=None, pce_override=None, cpi_yoy_override=None,
                    core_cpi_yoy_override=None):
    r = {"yoy_basis": {}, "yoy_overrides_used": [], "yoy_missing": []}
    r["CPI_YoY"] = _cpi_yoy(d, "CPIAUCNS", cpi_p, cpi_yoy_override, r, "CPI_YoY")
    r["CoreCPI_YoY"] = _cpi_yoy(d, "CPILFENS", cpi_p, core_cpi_yoy_override, r, "CoreCPI_YoY")
    r["CPI_mm"] = _pct(d.m("CPIAUCSL", cpi_p), d.m("CPIAUCSL", ym_shift(cpi_p, -1)))
    r["CoreCPI_mm"] = _pct(d.m("CPILFESL", cpi_p), d.m("CPILFESL", ym_shift(cpi_p, -1)))
    r["CPI_3m_ann"] = _ann3(d.m("CPIAUCSL", cpi_p), d.m("CPIAUCSL", ym_shift(cpi_p, -3)))
    r["CoreCPI_3m_ann"] = _ann3(d.m("CPILFESL", cpi_p), d.m("CPILFESL", ym_shift(cpi_p, -3)))
    c3 = ym_shift(cpi_p, -3)
    r["CoreCPI_YoY_t_minus_3m"] = _cpi_yoy(d, "CPILFENS", c3, core_cpi_yoy_override, r, "CoreCPI_YoY_t_minus_3m")
    r["PCE_YoY"] = _pct(d.m("PCEPI", pce_p), d.m("PCEPI", ym_shift(pce_p, -12)))
    r["CorePCE_YoY"] = _pct(d.m("PCEPILFE", pce_p), d.m("PCEPILFE", ym_shift(pce_p, -12)))
    r["PCE_mm"] = _pct(d.m("PCEPI", pce_p), d.m("PCEPI", ym_shift(pce_p, -1)))
    r["CorePCE_mm"] = _pct(d.m("PCEPILFE", pce_p), d.m("PCEPILFE", ym_shift(pce_p, -1)))
    r["PCE_3m_ann"] = _ann3(d.m("PCEPI", pce_p), d.m("PCEPI", ym_shift(pce_p, -3)))
    r["CorePCE_3m_ann"] = _ann3(d.m("PCEPILFE", pce_p), d.m("PCEPILFE", ym_shift(pce_p, -3)))
    p3 = ym_shift(pce_p, -3)
    r["CorePCE_YoY_t_minus_3m"] = _pct(d.m("PCEPILFE", p3), d.m("PCEPILFE", ym_shift(p3, -12)))

    # Direction terms are computed on one consistent (current) vintage before the validation-only
    # as-reported PCE override, so a first-published level is never differenced against a revised
    # earlier month. Official CPI YoY overrides (substitutes for uncomputable YoY, above) do enter.
    need = ("CorePCE_YoY", "CorePCE_YoY_t_minus_3m", "CoreCPI_YoY", "CoreCPI_YoY_t_minus_3m",
            "CorePCE_3m_ann", "CoreCPI_3m_ann")
    terms = None
    if all(r[k] is not None for k in need):
        terms = [
            sign_eps(r["CorePCE_YoY"] - r["CorePCE_YoY_t_minus_3m"], 0.10),
            sign_eps(r["CoreCPI_YoY"] - r["CoreCPI_YoY_t_minus_3m"], 0.10),
            sign_eps(r["CorePCE_3m_ann"] - r["CorePCE_YoY"], 0.10),
            sign_eps(r["CoreCPI_3m_ann"] - r["CoreCPI_YoY"], 0.10),
        ]
    if pce_override:  # validation only: as-first-published PCE levels (pre-revision vintage)
        for k, v in pce_override.items():
            if k in r:
                r[k] = float(v)
        r["pce_override"] = sorted(pce_override)
    subs = {
        "core_pce_yoy": None if r["CorePCE_YoY"] is None else lininterp(r["CorePCE_YoY"], ANCHORS["core_pce_yoy"]),
        "core_pce_3m_annualized": None if r["CorePCE_3m_ann"] is None else lininterp(r["CorePCE_3m_ann"], ANCHORS["core_pce_3m_annualized"]),
        "core_cpi_yoy": None if r["CoreCPI_YoY"] is None else lininterp(r["CoreCPI_YoY"], ANCHORS["core_cpi_yoy"]),
        "core_cpi_3m_annualized": None if r["CoreCPI_3m_ann"] is None else lininterp(r["CoreCPI_3m_ann"], ANCHORS["core_cpi_3m_annualized"]),
    }
    # Headline shock: max of separately normalized headline CPI and PCE YoY; persistence haircut.
    heads = [x for x in (r["CPI_YoY"], r["PCE_YoY"]) if x is not None]
    shock = max(lininterp(x, ANCHORS["headline_cpi_or_pce_yoy"]) for x in heads) if heads else None
    gaps = []
    if r["CPI_YoY"] is not None and r["CoreCPI_YoY"] is not None:
        gaps.append(r["CPI_YoY"] - r["CoreCPI_YoY"])
    if r["PCE_YoY"] is not None and r["CorePCE_YoY"] is not None:
        gaps.append(r["PCE_YoY"] - r["CorePCE_YoY"])
    r["larger_headline_core_gap"] = max(gaps) if gaps else None
    haircut = (shock is not None and gaps and max(gaps) >= 1.0
               and r["CoreCPI_3m_ann"] is not None and r["CorePCE_3m_ann"] is not None
               and r["CoreCPI_3m_ann"] <= 3.0 and r["CorePCE_3m_ann"] <= 3.0)
    r["headline_haircut_applied"] = bool(haircut)
    subs["headline_inflation_shock"] = None if shock is None else shock * (0.75 if haircut else 1.0)
    # Direction (terms computed above on a single vintage)
    if terms is not None:
        r["direction_terms"] = terms
        subs["inflation_direction"] = clamp(50 + 12.5 * sum(terms), 0, 100)
    else:
        r["direction_terms"] = None
        subs["inflation_direction"] = None
    score, missing, flags = combine("InflationRisk", subs, prior)
    t = r["direction_terms"]
    # CPI/PCE direction conflict: the PCE and CPI terms of either direction pair (YoY change, or
    # momentum versus YoY) carry opposite nonzero signs.
    r["direction_conflict"] = bool(t and ((t[0] * t[1] < 0) or (t[2] * t[3] < 0)))
    return score, subs, r, flags


def labor_block(d, p, revision_k, payroll_override=None, prior=None, flat_k=DIVERGENCE_FLAT_K):
    r = {}
    r["UNRATE"] = d.m("UNRATE", p)
    r["SAHMCURRENT"] = d.m("SAHMCURRENT", p)
    u = [d.m("UNRATE", ym_shift(p, -k)) for k in range(6)]
    r["UNRATE_3m_trend"] = None if None in u else sum(u[:3]) / 3 - sum(u[3:]) / 3

    def chg(sid, ym, k=1):
        a, b = d.m(sid, ym), d.m(sid, ym_shift(ym, -k))
        return None if a is None or b is None else a - b

    changes = []
    for k in range(3):
        ym = ym_shift(p, -k)
        if payroll_override and ym in payroll_override:
            changes.append(float(payroll_override[ym]))
        else:
            changes.append(chg("PAYEMS", ym))
    r["payroll_changes_k"] = {ym_shift(p, -k): changes[k] for k in range(3)}
    r["payroll_change_k"] = changes[0]
    r["payroll_3m_avg_k"] = None if None in changes else sum(changes) / 3
    r["payroll_revision_2m_k"] = revision_k

    def drop(sid):
        c = chg(sid, p, 6)
        return None if c is None else max(0.0, -c)

    o = [drop("CIVPART"), drop("EMRATIO")]
    pa = [drop("LNS11300060"), drop("LNS12300060")]
    r["CIVPART"], r["EMRATIO"] = d.m("CIVPART", p), d.m("EMRATIO", p)
    r["CIVPART_6m_chg"], r["EMRATIO_6m_chg"] = chg("CIVPART", p, 6), chg("EMRATIO", p, 6)
    r["prime_LFPR_6m_chg"], r["prime_EPOP_6m_chg"] = chg("LNS11300060", p, 6), chg("LNS12300060", p, 6)
    if None in o or None in pa:
        r["participation_stress"] = None
    else:
        r["OverallDrop"], r["PrimeAgeDrop"] = max(o), max(pa)
        r["participation_stress"] = 0.60 * max(o) + 0.40 * max(pa)

    hh = chg("CE16OV", p, 3)
    es = chg("PAYEMS", p, 3)
    if payroll_override and all(v is not None for v in changes):
        es = sum(changes)  # 3-month change consistent with the overridden monthly changes
    r["household_3m_chg_k"], r["establishment_3m_chg_k"] = hh, es
    div = None
    if hh is not None and es is not None:
        diff = abs(hh - es)
        flat = abs(hh) < flat_k or abs(es) < flat_k
        same = (hh >= 0) == (es >= 0)
        if not same and not flat and diff > 1500:
            div = 90.0
        elif (not same and not flat) or diff >= 750:
            div = 70.0
        elif flat or diff >= 300:
            div = 40.0
        else:
            div = 20.0
        civ6 = r["CIVPART_6m_chg"]
        if (div >= 70 and civ6 is not None and civ6 <= -0.5
                and revision_k is not None and revision_k <= -100):
            div = 100.0
        r["survey_diff_k"], r["survey_one_flat"], r["survey_same_sign"] = diff, flat, same
    r["divergence_score"] = div

    subs = {
        "sahm_rule": None if r["SAHMCURRENT"] is None else lininterp(r["SAHMCURRENT"], ANCHORS["sahm_rule_pp"]),
        "unemployment_3m_trend": None if r["UNRATE_3m_trend"] is None else lininterp(r["UNRATE_3m_trend"], ANCHORS["unemployment_3m_average_change_pp"]),
        "payroll_3m_average": None if r["payroll_3m_avg_k"] is None else lininterp(r["payroll_3m_avg_k"], ANCHORS["payroll_3m_average_thousands"]),
        "payroll_revisions": None if revision_k is None else lininterp(revision_k, ANCHORS["combined_latest_two_month_payroll_revision_thousands"]),
        "participation_and_employment_ratio": None if r["participation_stress"] is None else lininterp(r["participation_stress"], ANCHORS["six_month_participation_stress_pp"]),
        "household_establishment_divergence": div,
    }
    score, missing, flags = combine("LaborRecessionRisk", subs, prior)
    return score, subs, r, flags


def growth_block(d, q, pce_p, advance_gdp, gdp_override=None, prior=None):
    r = {}
    gdp = d.q("A191RL1Q225SBEA", q)
    pfs = d.q("PB0000031Q225SBEA", q)
    r["pfs_source"] = "PB0000031Q225SBEA"
    if pfs is None:
        lv, lp = d.q("LB0000031Q020SBEA", q), d.q("LB0000031Q020SBEA", q_shift(q, -1))
        pfs = None if lv is None or lp is None else 100.0 * ((lv / lp) ** 4 - 1.0)
        r["pfs_source"] = "LB0000031Q020SBEA (computed SAAR)"
    if gdp_override:
        gdp = gdp_override.get("real_gdp_saar", gdp)
        pfs = gdp_override.get("private_final_sales_saar", pfs)
        r["pfs_source"] += " / as-reported override"
    r["Real_GDP_SAAR"], r["Private_final_sales_SAAR"] = gdp, pfs
    r["Real_GDP_SAAR_prev_q"] = d.q("A191RL1Q225SBEA", q_shift(q, -1))
    r["Real_PCE_3m_ann"] = _ann3(d.m("PCEC96", pce_p), d.m("PCEC96", ym_shift(pce_p, -3)))
    r["advance_gdp"] = advance_gdp
    r["gdp_minus_advance"] = None if gdp is None or advance_gdp is None else round(gdp - advance_gdp, 1)
    subs = {
        "real_gdp_qoq_saar": None if gdp is None else lininterp(gdp, ANCHORS["real_gdp_qoq_saar"]),
        "real_final_sales_to_private_domestic_purchasers_saar": None if pfs is None else lininterp(pfs, ANCHORS["private_domestic_final_sales_saar"]),
        "real_pce_3m_annualized": None if r["Real_PCE_3m_ann"] is None else lininterp(r["Real_PCE_3m_ann"], ANCHORS["real_pce_3m_annualized"]),
        "gdp_revision_and_two_quarter_trend": None if r["gdp_minus_advance"] is None else lininterp(r["gdp_minus_advance"], ANCHORS["latest_estimate_minus_advance_estimate_pp"]),
    }
    score, missing, flags = combine("GrowthRisk", subs, prior)
    return score, subs, r, flags


def hike_voters(fed):
    """Voters preferring a hike: everyone not dissenting against a hike decision, otherwise
    the hike dissents."""
    if fed.get("action") in HIKE_ACTIONS and fed.get("voters"):
        return fed["voters"] - fed.get("cut_dissents", 0) - fed.get("hold_dissents", 0)
    return fed.get("hike_dissents", 0)


def effective_guidance(fed):
    """guidance_classification.hawkish: hawkish when the statement/press conference/minutes
    indicate tightening is likely or necessary, OR when at least two voters prefer a hike.
    With a statement-only classification in the facts ('guidance_statement') the hike-voter
    rule is applied here, so guidance follows the dissent count in every scenario. Without it
    the facts' 'guidance' is used as given."""
    stmt = fed.get("guidance_statement")
    if stmt is None:
        return fed.get("guidance"), "the facts' classification (no statement-only reading supplied)"
    hv = hike_voters(fed)
    hawk = "hawkish_or_higher_for_longer"
    if hv >= THRESH["hawkish_hike_voters"] and GUIDANCE_SCORES[stmt] < GUIDANCE_SCORES[hawk]:
        return hawk, f"hike-voter rule ({hv} prefer a hike; statement alone {GUIDANCE_LABELS[stmt]})"
    if hv < THRESH["hawkish_hike_voters"]:
        return stmt, f"statement ({hv} prefer a hike, below the {THRESH['hawkish_hike_voters']}-voter hawkish rule)"
    return stmt, "statement"


def fed_block(fed, sep, inflation_risk, core_pce_yoy, labor_risk, growth_risk, prior=None):
    r = {}
    if fed is None:
        fed = {}
    lo, hi = fed.get("lower"), fed.get("upper")
    mid = None if lo is None or hi is None else (lo + hi) / 2
    r["midpoint"] = mid
    lr = (sep or {}).get("long_run_median")
    gap = None if mid is None or lr is None else mid - lr
    r["gap_to_long_run"] = gap
    gap_s = None if gap is None else lininterp(gap, ANCHORS["target_midpoint_minus_sep_long_run_median_pp"])
    r["under_restriction_floor"] = False
    if gap_s is not None and core_pce_yoy is not None and inflation_risk is not None:
        if mid < core_pce_yoy and inflation_risk >= 60:
            r["under_restriction_floor"] = gap_s < 70
            gap_s = max(gap_s, 70.0)
    action = fed.get("action")
    act_s = None if action is None else float(ACTION_SCORES[action])
    r["recessionary_cut_floor"] = False
    if act_s is not None and action in CUT_ACTIONS and (
            (labor_risk or 0) >= 70 or (growth_risk or 0) >= 70):
        r["recessionary_cut_floor"] = act_s < 70
        act_s = max(act_s, 70.0)
    g, g_rule = effective_guidance(fed)
    r["guidance"], r["guidance_rule"] = g, g_rule
    r["guidance_facts_mismatch"] = (fed.get("guidance_statement") is not None
                                    and fed.get("guidance") is not None and fed["guidance"] != g)
    guid_s = None
    if g is not None:
        adj = clamp(float(fed.get("minutes_adjustment", 0)), -10, 10)
        guid_s = clamp(GUIDANCE_SCORES[g] + adj, 0, 100)
    v = fed.get("voters")
    vote_s = None
    if v:
        vote_s = clamp(50 + 100 * (fed.get("hike_dissents", 0) - fed.get("cut_dissents", 0)) / v, 0, 100)
    sep_chg = None
    if sep and sep.get("current_year_median") is not None and sep.get("prior_current_year_median") is not None:
        sep_chg = round(sep["current_year_median"] - sep["prior_current_year_median"], 2)
    r["sep_change"] = sep_chg
    sep_s = None if sep_chg is None else lininterp(sep_chg, ANCHORS["current_year_sep_median_change_vs_prior_sep_pp"])
    subs = {
        "policy_rate_gap_to_long_run_neutral": gap_s,
        "recent_policy_action": act_s,
        "statement_press_conference_minutes_guidance": guid_s,
        "vote_and_dissent_balance": vote_s,
        "sep_rate_path_change": sep_s,
    }
    score, missing, flags = combine("FedPolicyRisk", subs, prior)
    return score, subs, r, flags


def regime_label(inf, lab, gro):
    if inf >= 60:
        label = "Stagflation risk" if (lab >= 65 or gro >= 65) else "Inflationary growth"
    else:
        label = "Recession risk" if (lab >= 65 or gro >= 65) else "Disinflation + expansion"
    borderline = abs(inf - 60) <= 2 or abs(lab - 65) <= 2 or abs(gro - 65) <= 2
    return label, borderline


def recession_assessment(lab, gro, sahm, payroll_avg):
    """Interpretive mapping (not scored). Spec names the four categories only."""
    worst = max(lab, gro)
    if (sahm is not None and sahm >= 0.50) or worst >= 70:
        return "high"
    if (sahm is not None and sahm >= 0.35) or worst >= 55:
        return "elevated"
    # "low" needs healthy payrolls: spec labor_lower_risk = 3m average stabilizes above 100,000.
    if worst >= 35 or (payroll_avg is not None and payroll_avg < 100):
        return "low-to-moderate"
    return "low"


def policy_impulse(fed, rates):
    """Interpretive mapping (not scored): mean of FedPolicyRisk and RatesCurveRisk."""
    m = (fed + rates) / 2
    if m < 30:
        return "supportive"
    if m < 45:
        return "mildly supportive"
    if m < 55:
        return "mixed"
    if m < 70:
        return "mildly restrictive"
    return "restrictive"


def change_class(delta, regime_changed):
    a = abs(delta)
    if a > 10 or regime_changed:
        return "regime shift"
    if a >= 6:
        return "meaningful change"
    if a >= 3:
        return "modest change"
    return "unchanged"


# --------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------
def run(d, run_facts, asof=None, overrides=False, flat_k=DIVERGENCE_FLAT_K):
    rates_asof = d.latest_common_treasury(asof)
    y = {s: d.daily[s][rates_asof] for s in ("DGS10", "DGS3MO", "DGS2")}
    last_neg = d.last_negative_10y_3m(rates_asof)
    periods = dict(run_facts.get("periods") or {})
    periods.setdefault("labor", d.latest_month("UNRATE"))
    periods.setdefault("cpi", d.latest_month("CPIAUCNS"))
    periods.setdefault("pce", d.latest_month("PCEPILFE"))
    periods.setdefault("gdp", d.latest_quarter("A191RL1Q225SBEA"))
    prior = run_facts.get("prior") or {}
    gv = run_facts.get("gdp_vintage") or {}
    rev = (run_facts.get("payroll_revision") or {}).get("value_k")

    out = {"rates_asof": rates_asof, "periods": periods, "comp": {}, "subs": {}, "raw": {},
           "flags": []}
    rc, rs, rr, rf = rates_block(y["DGS10"], y["DGS3MO"], y["DGS2"], rates_asof, last_neg)
    ic, isub, ir, iflags = inflation_block(d, periods["cpi"], periods["pce"], prior.get("InflationRisk"),
                                           run_facts.get("as_reported_pce") if overrides else None,
                                           run_facts.get("cpi_yoy_override"),
                                           run_facts.get("core_cpi_yoy_override"))
    lc, ls, lr, lf = labor_block(d, periods["labor"], rev,
                                 run_facts.get("as_reported_payrolls_k") if overrides else None,
                                 prior.get("LaborRecessionRisk"), flat_k)
    gc, gs, gr, gf = growth_block(d, periods["gdp"], periods["pce"], gv.get("advance_real_gdp_saar"),
                                  gv.get("as_reported") if overrides else None,
                                  prior.get("GrowthRisk"))
    fc, fs, fr, ff = fed_block(run_facts.get("fed"), run_facts.get("sep"), ic,
                               ir.get("CorePCE_YoY"), lc, gc, prior.get("FedPolicyRisk"))
    for name, c, s, raw, fl in (("InflationRisk", ic, isub, ir, iflags),
                                ("LaborRecessionRisk", lc, ls, lr, lf),
                                ("GrowthRisk", gc, gs, gr, gf),
                                ("RatesCurveRisk", rc, rs, rr, rf),
                                ("FedPolicyRisk", fc, fs, fr, ff)):
        out["comp"][name], out["subs"][name], out["raw"][name] = c, s, raw
        out["flags"] += [(name, f) for f in fl]
    weighted = sum(WEIGHTS[k] * out["comp"][k] for k in WEIGHTS)
    out["weighted"] = weighted
    out["score"] = int(round_half_up(clamp(weighted, 0, 100)))

    # Confidence
    pens = []
    if gv.get("vintage") == "advance":
        pens.append("advance_gdp_only")
    if rev is not None and rev <= -100:
        pens.append("combined_two_month_payroll_revision_at_or_below_minus_100k")
    if (lr.get("divergence_score") or 0) >= 70:
        pens.append("material_household_establishment_divergence")
    if ir.get("direction_conflict"):
        pens.append("cpi_and_pce_direction_conflict")
    cp, pp = periods["cpi"], periods["pce"]
    lag = (int(cp[:4]) * 12 + int(cp[5:7])) - (int(pp[:4]) * 12 + int(pp[5:7]))
    if lag > 2:
        pens.append("pce_more_than_two_reference_months_behind_cpi")
    fl = {f for _, f in out["flags"]}
    for f in ("required_submetric_missing_but_reweightable", "component_more_than_30_percent_missing"):
        if f in fl:
            pens.append(f)
    if not run_facts.get("primary_verified", False):
        pens.append("source_discrepancy_not_fully_resolved")
    out["penalties"] = pens
    out["confidence"] = int(clamp(CONF_BASE - sum(CONF_PENALTIES[p] for p in pens), 40, 95))
    label, border = regime_label(ic, lc, gc)
    out["regime"], out["regime_borderline"] = label, border
    out["recession"] = recession_assessment(lc, gc, lr["SAHMCURRENT"], lr["payroll_3m_avg_k"])
    out["impulse"] = policy_impulse(fc, rc)
    if prior.get("score") is not None:
        out["prior"] = prior
        out["delta"] = out["score"] - prior["score"]
        out["contrib"] = {k: WEIGHTS[k] * (out["comp"][k] - prior[k]) for k in WEIGHTS if k in prior}
        out["change_class"] = change_class(out["delta"], False)
    return out


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------
CSV_COLS = ["rates_asof", "DGS10", "DGS3MO", "DGS2", "spread_10y_3m", "spread_10y_2y",
            "spread_2y_3m", "UNRATE", "UNRATE_period", "SAHMCURRENT", "SAHM_period",
            "payroll_change_k", "payroll_3m_avg_k", "payroll_revision_2m_k", "CIVPART",
            "EMRATIO", "CPI_YoY", "CoreCPI_YoY", "CPI_3m_ann", "CoreCPI_3m_ann", "PCE_YoY",
            "CorePCE_YoY", "PCE_3m_ann", "CorePCE_3m_ann", "Real_GDP_SAAR",
            "Private_final_sales_SAAR", "Fed_lower_bound", "Fed_upper_bound", "hike_dissents",
            "cut_dissents", "InflationRisk", "LaborRecessionRisk", "GrowthRisk",
            "RatesCurveRisk", "FedPolicyRisk", "MacroRiskScore", "Confidence", "Regime"]


def csv_row(o, rf):
    R, L, I, G = o["raw"]["RatesCurveRisk"], o["raw"]["LaborRecessionRisk"], o["raw"]["InflationRisk"], o["raw"]["GrowthRisk"]
    fed = rf.get("fed") or {}
    p = o["periods"]
    vals = [o["rates_asof"], fmt(R["DGS10"]), fmt(R["DGS3MO"]), fmt(R["DGS2"]),
            fmt(R["spread_10y_3m"]), fmt(R["spread_10y_2y"]), fmt(R["spread_2y_3m"]),
            fmt(L["UNRATE"], 1), p["labor"], fmt(L["SAHMCURRENT"]), p["labor"],
            fmt(L["payroll_change_k"], 0), fmt(L["payroll_3m_avg_k"], 1),
            fmt(L["payroll_revision_2m_k"], 0), fmt(L["CIVPART"], 1), fmt(L["EMRATIO"], 1),
            fmt(I["CPI_YoY"], 1), fmt(I["CoreCPI_YoY"], 1), fmt(I["CPI_3m_ann"], 1),
            fmt(I["CoreCPI_3m_ann"], 1), fmt(I["PCE_YoY"], 1), fmt(I["CorePCE_YoY"], 1),
            fmt(I["PCE_3m_ann"], 1), fmt(I["CorePCE_3m_ann"], 1), fmt(G["Real_GDP_SAAR"], 1),
            fmt(G["Private_final_sales_SAAR"], 1), fmt(fed.get("lower")), fmt(fed.get("upper")),
            str(fed.get("hike_dissents", "")), str(fed.get("cut_dissents", ""))]
    vals += [fmt(o["comp"][k], 1) for k in WEIGHTS]
    vals += [str(o["score"]), str(o["confidence"]), o["regime"]]
    return ",".join(CSV_COLS) + "\n" + ",".join(f'"{v}"' if "," in v or "+" in v else v for v in vals)


def report(o, rf):
    lines = []
    w = lines.append
    p = o["periods"]
    w(f"Macro & Policy (US equities) | rates {o['rates_asof']} close | labor {p['labor']} | "
      f"CPI {p['cpi']} | PCE {p['pce']} | GDP {p['gdp']} {(rf.get('gdp_vintage') or {}).get('vintage', '?')}")
    for k in WEIGHTS:
        w(f"  {k:<20} {o['comp'][k]:6.2f}  (w {WEIGHTS[k]:.2f}, contributes {WEIGHTS[k] * o['comp'][k]:6.3f})")
        for sk, sv in o["subs"][k].items():
            w(f"      {sk:<58} {fmt(sv, 2)}")
    w(f"  weighted {o['weighted']:.4f} -> SCORE_0_100 {o['score']} | confidence {o['confidence']} "
      f"penalties {o['penalties']}")
    w(f"  regime {o['regime']}{' (borderline)' if o['regime_borderline'] else ''} | recession "
      f"{o['recession']} | impulse {o['impulse']}")
    if "delta" in o:
        w(f"  vs prior {o['prior']['score']} ({o['prior']['asof']}): {o['delta']:+d} [{o['change_class']}]")
        for k, v in o["contrib"].items():
            w(f"      {k:<20} {o['prior'][k]:6.1f} -> {o['comp'][k]:6.2f}  contribution {v:+.3f}")
    w("  raw inputs:")
    for k in WEIGHTS:
        for rk, rv in o["raw"][k].items():
            w(f"      {k}.{rk} = {rv if not isinstance(rv, float) else round(rv, 4)}")
    return "\n".join(lines)


VALIDATION_DATE = "2026-09-16"


def published_weighted(pub):
    """Unrounded weighted score rebuilt from the published component scores (44.215 on
    2026-09-16; see continuity_snapshot all_packets_reconciled). Validation compares against
    this, not against the rounded SCORE_0_100."""
    return sum(WEIGHTS[k] * pub[k] for k in WEIGHTS)


def fed_guidance_backsolve(fed_comp_published, fed_subs):
    """Guidance subscore that would reproduce a published FedPolicyRisk given the other subs."""
    w = SUBWEIGHTS["FedPolicyRisk"]
    gk = "statement_press_conference_minutes_guidance"
    rest = sum(w[k] * fed_subs[k] for k in w if k != gk)
    return (fed_comp_published - rest) / w[gk]


def validate(d, facts, quiet=False):
    rf = facts["runs"][VALIDATION_DATE]
    pub = rf["published"]
    pub_w = published_weighted(pub)
    cur = run(d, rf, VALIDATION_DATE, overrides=False)
    asr = run(d, rf, VALIDATION_DATE, overrides=True)
    # Sensitivity of the recompute to the household/establishment "approximately flat" rule:
    # strict reading = no flat exemption, so opposite three-month signs always score 70.
    cur_strict = run(d, rf, VALIDATION_DATE, overrides=False, flat_k=0.0)
    asr_strict = run(d, rf, VALIDATION_DATE, overrides=True, flat_k=0.0)
    back = fed_guidance_backsolve(pub["FedPolicyRisk"], cur["subs"]["FedPolicyRisk"])
    v = {"pub": pub, "pub_weighted": pub_w, "cur": cur, "asr": asr,
         "cur_strict": cur_strict, "asr_strict": asr_strict, "fed_guidance_backsolve": back,
         "diff_current": cur["weighted"] - pub_w, "diff_vintage": asr["weighted"] - pub_w}
    v["rc_ok"] = abs(cur["comp"]["RatesCurveRisk"] - pub["RatesCurveRisk"]) <= 2
    v["sc_current"] = abs(v["diff_current"]) <= 3
    v["sc_vintage"] = abs(v["diff_vintage"]) <= 3
    v["ok"] = v["rc_ok"] and v["sc_current"]
    if quiet:
        return v
    print(f"VALIDATION {VALIDATION_DATE} (published | current-vintage recompute | as-reported overrides)")
    for k in WEIGHTS:
        pv, c, a = pub[k], cur["comp"][k], asr["comp"][k]
        print(f"  {k:<20} {pv:7.3f} | {c:7.2f} (diff {c - pv:+6.2f}) | {a:7.2f} (diff {a - pv:+6.2f})")
    print(f"  {'weighted':<20} {pub_w:7.3f} | {cur['weighted']:7.2f} (diff {v['diff_current']:+6.2f}) | "
          f"{asr['weighted']:7.2f} (diff {v['diff_vintage']:+6.2f})   [published unrounded, from its components]")
    print(f"  rounded score: current-vintage {cur['score']}, as-reported {asr['score']}, published {pub['score']}")
    for s in ("DGS10", "DGS3MO", "DGS2"):
        print(f"  {s} {cur['raw']['RatesCurveRisk'][s]} vs published {pub[s]}")
    print(f"  RatesCurveRisk within 2: {v['rc_ok']}")
    print(f"  weighted within 3 of {pub_w:.3f}: current-vintage {v['sc_current']}, vintage-matched {v['sc_vintage']}")
    print(f"  flat-rule sensitivity (strict = no 'approximately flat' exemption): current-vintage "
          f"{cur['weighted']:.2f}->{cur['score']} vs strict {cur_strict['weighted']:.2f}->{cur_strict['score']}; "
          f"vintage-matched {asr['weighted']:.2f}->{asr['score']} vs strict "
          f"{asr_strict['weighted']:.2f}->{asr_strict['score']}; Labor as-reported "
          f"{asr['comp']['LaborRecessionRisk']:.2f} vs strict {asr_strict['comp']['LaborRecessionRisk']:.2f} "
          f"(published {pub['LaborRecessionRisk']})")
    print(f"  FedPolicyRisk guidance back-solve from published {pub['FedPolicyRisk']}: {back:.2f} "
          f"(guidance 75 was chosen with reference to this; the Fed match is not independent)")
    print("  'every gap explained' is a judgment recorded in the packet NOTES, not tested here")
    for k in ("InflationRisk", "LaborRecessionRisk", "GrowthRisk"):
        print(f"  {k} as-reported subs: " + ", ".join(f"{a}={b:.2f}" for a, b in asr["subs"][k].items() if b is not None))
    return v


def decompose(today, cur0916, prior, quiet=False):
    """Split change vs prior into revision/method (recompute@prior minus published) and
    like-for-like economic change (today minus current-vintage recompute@prior)."""
    rows, tot_r, tot_e = {}, 0.0, 0.0
    for k in WEIGHTS:
        rv = WEIGHTS[k] * (cur0916["comp"][k] - prior[k])
        ec = WEIGHTS[k] * (today["comp"][k] - cur0916["comp"][k])
        rows[k] = (rv, ec)
        tot_r += rv
        tot_e += ec
    if not quiet:
        print("DECOMPOSITION vs prior (weighted points): total = revision + economic")
        for k, (rv, ec) in rows.items():
            print(f"  {k:<20} total {rv + ec:+.3f} = revision {rv:+.3f} + economic {ec:+.3f}")
        print(f"  overall              total {tot_r + tot_e:+.3f} = revision {tot_r:+.3f} + economic {tot_e:+.3f}")
    return {"rows": rows, "revision": tot_r, "economic": tot_e}


# --------------------------------------------------------------------------------------
# Rounding / confidence sensitivity (all computed; nothing here is a literal result)
# --------------------------------------------------------------------------------------
def _rerun(d, rf, asof, mutate):
    f = json.loads(json.dumps(rf))
    mutate(f)
    return run(d, f, asof)


def _set(path, value):
    def m(f):
        node = f
        for k in path[:-1]:
            node = node.setdefault(k, {})
        node[path[-1]] = value
    return m


def _bisect(fn, lo, hi, target, iters=80):
    flo, fhi = fn(lo) - target, fn(hi) - target
    if flo == 0:
        return lo
    if fhi == 0:
        return hi
    if (flo > 0) == (fhi > 0):
        return None
    for _ in range(iters):
        mid = (lo + hi) / 2
        fm = fn(mid) - target
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2


def _guid(g):
    """Scenario: the statement/press-conference classification becomes g. The spec's hike-voter
    rule is still applied on top (effective_guidance)."""
    def m(f):
        f["fed"]["guidance"] = g
        if "guidance_statement" in f["fed"]:
            f["fed"]["guidance_statement"] = g
    return m


def _missing_subs(o):
    return [f"{c}.{k}" for c, subs in o["subs"].items() for k, v in subs.items() if v is None]


def _summ(key, label, o):
    I = o["raw"]["InflationRisk"]
    return {"key": key, "label": label, "weighted": o["weighted"], "score": o["score"],
            "confidence": o["confidence"], "penalties": list(o["penalties"]),
            "terms": I.get("direction_terms"), "conflict": I.get("direction_conflict"),
            "InflationRisk": o["comp"]["InflationRisk"], "FedPolicyRisk": o["comp"]["FedPolicyRisk"],
            "guidance": o["raw"]["FedPolicyRisk"].get("guidance"),
            "CorePCE_YoY": I.get("CorePCE_YoY"), "CorePCE_3m_ann": I.get("CorePCE_3m_ann"),
            "CoreCPI_YoY": I.get("CoreCPI_YoY"), "CoreCPI_3m_ann": I.get("CoreCPI_3m_ann"),
            "missing": _missing_subs(o), "periods": dict(o["periods"]),
            "yoy_overrides_used": I.get("yoy_overrides_used") or []}


def next_print(d, rf, asof, base_periods, cpi=None, core_cpi=None, pce=None, core_pce=None,
               real_pce=None, cpi_yoy=None, core_cpi_yoy=None, yoy_source="hypothetical scenario"):
    """Append a hypothetical next monthly print (m/m %, seasonally adjusted) to in-memory copies
    of the series and recompute the FULL score through every submetric (YoY, 3m annualized,
    headline shock and haircut, direction terms, conflict, reweighting, Fed floors, confidence).

    Headline defaults to core when only core is given. Real PCE (PCEC96) is appended only when
    real_pce is given; otherwise the growth real-PCE submetric is missing for the new month and
    GrowthRisk is reweighted (disclosed). CPI YoY for the new month is never synthesized from SA
    m/m: it comes from NSA levels if FRED has them, else from an official BLS YoY override in the
    facts, else from cpi_yoy/core_cpi_yoy (labelled hypothetical), else it is missing. Overrides
    only apply to months whose t-12 level is blank."""
    dd = d.clone()
    f = json.loads(json.dumps(rf))
    per = dict(base_periods)
    f["periods"] = per
    if core_pce is not None or pce is not None:
        b = base_periods["pce"]
        m = ym_shift(b, 1)
        cm = core_pce if core_pce is not None else pce
        hm = pce if pce is not None else core_pce
        dd.monthly["PCEPILFE"][m] = dd.monthly["PCEPILFE"][b] * (1 + cm / 100)
        dd.monthly["PCEPI"][m] = dd.monthly["PCEPI"][b] * (1 + hm / 100)
        if real_pce is not None and dd.m("PCEC96", b) is not None:
            dd.monthly["PCEC96"][m] = dd.monthly["PCEC96"][b] * (1 + real_pce / 100)
        per["pce"] = m
    if core_cpi is not None or cpi is not None:
        b = base_periods["cpi"]
        m = ym_shift(b, 1)
        cm = core_cpi if core_cpi is not None else cpi
        hm = cpi if cpi is not None else core_cpi
        dd.monthly["CPILFESL"][m] = dd.monthly["CPILFESL"][b] * (1 + cm / 100)
        dd.monthly["CPIAUCSL"][m] = dd.monthly["CPIAUCSL"][b] * (1 + hm / 100)
        for key, v in (("cpi_yoy_override", cpi_yoy), ("core_cpi_yoy_override", core_cpi_yoy)):
            if v is not None and m not in (f.get(key) or {}):
                f.setdefault(key, {})[m] = {"value": v, "source": yoy_source, "date": "scenario"}
        per["cpi"] = m
    return run(dd, f, asof)


PCE_CASES = (("cool", 0.15), ("ordinary", 0.25), ("firm", 0.30), ("hot", 0.35))


def print_scenarios(d, rf, asof, o):
    """Hypothetical next CPI/PCE prints, each recomputed through the full score."""
    base = o["periods"]
    I = o["raw"]["InflationRisk"]
    pm, cm = ym_shift(base["pce"], 1), ym_shift(base["cpi"], 1)
    out = {"pce_month": pm, "cpi_month": cm, "pce": [], "joint": [], "cpi": []}
    for tag, mm in PCE_CASES:
        r = next_print(d, rf, asof, base, core_pce=mm)
        out["pce"].append(_summ(f"pce_{tag}", f"{tag} {month_name(pm)} core PCE +{mm:.2f}% m/m", r) | {"mm": mm, "tag": tag})
    r = next_print(d, rf, asof, base, cpi=0.0, core_cpi=0.0, pce=0.0, core_pce=0.0)
    out["joint"].append(_summ("flat", f"{month_name(cm)} CPI and {month_name(pm)} PCE both 0.00% m/m", r))
    r = next_print(d, rf, asof, base, core_cpi=0.25)
    out["cpi"].append(_summ("cpi_ordinary", f"ordinary {month_name(cm)} core CPI +0.25% m/m", r))
    real_ovr = cm in (rf.get("core_cpi_yoy_override") or {})
    if I.get("CPI_YoY") is not None and I.get("CoreCPI_YoY") is not None and not real_ovr:
        hy, cy = round(I["CPI_YoY"], 1), round(I["CoreCPI_YoY"], 1)
        r = next_print(d, rf, asof, base, core_cpi=0.25, cpi_yoy=hy, core_cpi_yoy=cy)
        out["cpi"].append(_summ("cpi_ordinary_yoy", f"ordinary {month_name(cm)} core CPI +0.25% m/m with "
                                f"official YoY supplied (hypothetically {cy}% core / {hy}% headline)", r))
    # Level arithmetic for the next core PCE print (break-even m/m).
    L = d.monthly.get("PCEPILFE", {})
    t, t2, t11 = base["pce"], ym_shift(base["pce"], -2), ym_shift(base["pce"], -11)
    if None not in (L.get(t), L.get(t2)):
        out["pce_mm_for_3m_raise"] = 100 * (L[t2] * (1 + THRESH["core_pce_3m_raise"] / 100) ** 0.25 / L[t] - 1)
    if None not in (L.get(t), L.get(t11)):
        out["pce_mm_for_yoy_lower"] = 100 * ((1 + THRESH["core_pce_yoy_lower"] / 100) * L[t11] / L[t] - 1)
    return out


def sensitivity(d, rf, asof, o):
    w, sc = o["weighted"], o["score"]
    lo_edge, hi_edge = sc - 0.5, sc + 0.5          # round_half_up: [sc-0.5, sc+0.5) -> sc
    s = {"weighted": w, "score": sc, "lo_edge": lo_edge, "hi_edge": hi_edge,
         "margin_down": w - lo_edge, "margin_up": hi_edge - w}
    s["component_breakeven"] = {k: (-(w - lo_edge) / WEIGHTS[k], (hi_edge - w) / WEIGHTS[k])
                                for k in WEIGHTS}
    fed = rf.get("fed") or {}
    sep = rf.get("sep") or {}
    rev = (rf.get("payroll_revision") or {}).get("value_k")

    def scen(key, label, mutate):
        return _summ(key, label, _rerun(d, rf, asof, mutate))

    down, up = [], []
    if rev is not None:
        fw = lambda x: _rerun(d, rf, asof, _set(("payroll_revision", "value_k"), x))["weighted"]
        be = _bisect(fw, rev, 0.0, lo_edge)
        if be is not None:
            k = math.ceil(be)
            down.append(scen("revision", f"payroll revision {k:+d}k or less negative (now {rev:+.0f}k; break-even {be:+.2f}k)",
                             _set(("payroll_revision", "value_k"), k)))
        s["revision_breakeven_up"] = _bisect(fw, -300.0, rev, hi_edge)
    lr = sep.get("long_run_median")
    if lr is not None:
        fl = lambda x: _rerun(d, rf, asof, _set(("sep", "long_run_median"), x))["weighted"]
        be = _bisect(fl, lr, lr + 1.0, lo_edge)
        nxt = round(lr + 0.1, 1)
        down.append(scen("sep_lr", f"longer-run SEP median {nxt} (now {lr}; break-even {fmt(be, 3)})",
                         _set(("sep", "long_run_median"), nxt)))
        s["sep_lr_breakeven_up"] = _bisect(fl, lr - 3.0, lr, hi_edge)
    hd = fed.get("hike_dissents") or 0
    if hd:
        for n in sorted({hd - 1, 0}, reverse=True):
            down.append(scen(f"dissents_{n}", f"{plural(n, 'hike dissent')} instead of {hd}",
                             _set(("fed", "hike_dissents"), n)))
    thr = THRESH["hike_voters_raise"]
    for n in sorted({hd + 1, thr}):
        if n > hd:
            up.append(scen(f"dissents_{n}", f"{plural(n, 'hike dissent')}", _set(("fed", "hike_dissents"), n)))
    bal = "balanced_data_dependent"
    if fed.get("guidance") != bal or fed.get("guidance_statement", bal) != bal:
        r = scen("guidance_balanced", f"statement guidance balanced ({GUIDANCE_SCORES[bal]})", _guid(bal))
        if abs(r["weighted"] - w) > 1e-9:
            down.append(r)
    s["balanced_statement_blocked"] = (effective_guidance(fed)[1].startswith("hike-voter rule"))
    if fed.get("action") != "balanced_hold":
        down.append(scen("action_balanced", f"action balanced hold ({ACTION_SCORES['balanced_hold']})",
                         _set(("fed", "action"), "balanced_hold")))
    up.append(scen("hike", f"FOMC 25bp hike ({ACTION_SCORES['twenty_five_bp_hike']})",
                   _set(("fed", "action"), "twenty_five_bp_hike")))
    up.append(scen("guidance_explicit", f"guidance explicit likely tightening ({GUIDANCE_SCORES['explicit_likely_tightening']})",
                   _guid("explicit_likely_tightening")))

    def both(f):
        f["fed"]["action"] = "twenty_five_bp_hike"
        _guid("explicit_likely_tightening")(f)
    up.append(scen("hike_explicit", "hike + explicit-tightening guidance together", both))
    s["down"], s["up"] = down, up
    s["by_key"] = {x["key"]: x for x in down + up}
    # Household/establishment divergence: perturb the CE16OV level (real data path, full rerun).
    L = o["raw"]["LaborRecessionRisk"]
    s["divergence_750"] = None
    if L.get("divergence_score") is not None and L["divergence_score"] < 70 and L.get("survey_diff_k") is not None:
        gap = THRESH["divergence_material_k"] - L["survey_diff_k"]
        dd = d.clone()
        p = o["periods"]["labor"]
        sign = 1.0 if L["household_3m_chg_k"] >= L["establishment_3m_chg_k"] else -1.0
        dd.monthly["CE16OV"][p] = dd.monthly["CE16OV"][p] + sign * gap
        r = run(dd, rf, asof)
        s["divergence_750"] = {"diff_k": L["survey_diff_k"], "gap_k": gap, "weighted": r["weighted"],
                               "score": r["score"], "confidence": r["confidence"],
                               "divergence": r["raw"]["LaborRecessionRisk"]["divergence_score"]}
    s["prints"] = print_scenarios(d, rf, asof, o)
    return s


def _sc(w):
    return int(round_half_up(clamp(w, 0, 100)))


# --------------------------------------------------------------------------------------
# Supervisor packet (packet contract: supervisor/packet_template.md)
# --------------------------------------------------------------------------------------
PACKET_MODULE = "Macro and Policy"   # official name per packet_template.md


def crosses(cur, thr, above=True):
    """Trigger verb that matches where the metric is now relative to the threshold."""
    if above:
        return "rises above" if cur <= thr else "stays above"
    return "falls below" if cur >= thr else "stays below"


def _cat(facts, needle, after):
    for c in sorted(facts.get("catalysts") or [], key=lambda c: c["date"]):
        if needle in c["event"] and c["date"] > after:
            return c["date"]
    return "date n/a"


def describe_terms(t):
    """Plain-language reading of the four inflation_direction terms."""
    if t is None:
        return "inflation direction not computable"
    yo = {1: "rising", 0: "flat", -1: "falling"}
    mo = {1: "above", 0: "in line with", -1: "below"}
    return (f"core CPI momentum {mo[t[3]]} its YoY, core PCE momentum {mo[t[2]]} its YoY; core PCE YoY "
            f"{yo[t[0]]} and core CPI YoY {yo[t[1]]} vs 3 months earlier")


def previous_decision(facts, fed):
    cur = fed.get("decision_date")
    best = None
    for r in (facts.get("runs") or {}).values():
        f = r.get("fed") or {}
        dd = f.get("decision_date")
        if dd and cur and dd < cur and (best is None or dd > best[0]):
            best = (dd, f.get("action"))
    return best


def _after_prev(facts, fed):
    pv = previous_decision(facts, fed)
    if not pv:
        return ""
    dd, act = pv
    if act in HIKE_ACTIONS:
        return f" after the {dd} hike"
    if act in CUT_ACTIONS:
        return f" after the {dd} cut"
    return f" after a {ACTION_LABELS.get(act, act)} on {dd}"


def driver_lines(o, rf, facts):
    I, L, G = o["raw"]["InflationRisk"], o["raw"]["LaborRecessionRisk"], o["raw"]["GrowthRisk"]
    R, F, p = o["raw"]["RatesCurveRisk"], o["raw"]["FedPolicyRisk"], o["periods"]
    fed, sep, pr = rf.get("fed") or {}, rf.get("sep") or {}, o.get("prior") or {}
    gv = rf.get("gdp_vintage") or {}
    T = THRESH

    def head(k):
        was = f" (was {pr[k]} at {pr['asof']})" if k in pr else ""
        return f"{k} {o['comp'][k]:.1f}{was}, contributes {WEIGHTS[k] * o['comp'][k]:.2f}:"

    if I["headline_haircut_applied"]:
        hc = "(persistence haircut applied)"
    else:
        hc = (f"(no persistence haircut: core CPI 3m ann {fmt(I['CoreCPI_3m_ann'])}% / core PCE "
              f"{fmt(I['CorePCE_3m_ann'])}% not both <= 3.0)")
    sahm, u_tr = L["SAHMCURRENT"], L["UNRATE_3m_trend"]
    benign = sahm is not None and sahm < T["sahm_lower"] and u_tr is not None and u_tr <= 0
    avg, rev = L["payroll_3m_avg_k"], L["payroll_revision_2m_k"]
    hd = fed.get("hike_dissents", 0)
    curve_pos = R["spread_10y_3m"] > 0 and R["spread_10y_2y"] > 0
    txt = {
        "InflationRisk": f"{head('InflationRisk')} core PCE {fmt(I['CorePCE_YoY'])}% YoY ({p['pce']}) "
                         f"{rel(I['CorePCE_YoY'], T['core_pce_yoy_lower'], 0.25)} the {T['core_pce_yoy_lower']:.1f}% "
                         f"lower-risk line; headline CPI {fmt(I['CPI_YoY'])}% ({p['cpi']}) / PCE {fmt(I['PCE_YoY'])}% "
                         f"({p['pce']}) YoY, headline-core gap {fmt(I['larger_headline_core_gap'])}pp {hc}; "
                         + describe_terms(I["direction_terms"]),
        "LaborRecessionRisk": f"{head('LaborRecessionRisk')} Sahm {fmt(sahm)} and UNRATE {fmt(L['UNRATE'], 1)}% "
                              f"(3m trend {fmt(u_tr)}pp) {'benign' if benign else 'not benign'}; payroll 3m avg "
                              f"{fmt(avg, 1)}k ({p['labor']}) {rel(avg, T['payroll_alert_k'], 10)} the "
                              f"{T['payroll_alert_k']:.0f}k alert and {rel(avg, T['payroll_lower_k'], 10)} the "
                              f"{T['payroll_lower_k']:.0f}k lower-risk line; two-month revision {fmt(rev, 0)}k",
        "GrowthRisk": f"{head('GrowthRisk')} real GDP {fmt(G['Real_GDP_SAAR'], 1)}% SAAR and private domestic final "
                      f"sales {fmt(G['Private_final_sales_SAAR'], 1)}% ({p['gdp']} {gv.get('vintage', 'vintage n/a')}), "
                      f"final sales {rel(G['Private_final_sales_SAAR'], T['pfs_lower'], 0.25)} the {T['pfs_lower']:.1f}% "
                      f"lower-risk line; real PCE 3m ann {fmt(G['Real_PCE_3m_ann'])}%",
        "RatesCurveRisk": f"{head('RatesCurveRisk')} DGS10 {R['DGS10']:.2f}% ({o['rates_asof']}) scores "
                          f"{o['subs']['RatesCurveRisk']['absolute_10y_yield']:.1f}, {rel(R['DGS10'], T['dgs10_raise'], 0.10)} "
                          f"the {T['dgs10_raise']:.2f}% alert; "
                          + (f"a positive curve (10y-3m {R['spread_10y_3m']:+.2f}, 10y-2y {R['spread_10y_2y']:+.2f}) offsets; "
                             if curve_pos else
                             f"curve 10y-3m {R['spread_10y_3m']:+.2f}, 10y-2y {R['spread_10y_2y']:+.2f} (not both positive); ")
                          + f"2y-3m {R['spread_2y_3m']:+.2f} {rel(R['spread_2y_3m'], T['spread_2y3m_raise'], 0.05)} the "
                            f"+{T['spread_2y3m_raise']:.2f} repricing alert",
        "FedPolicyRisk": f"{head('FedPolicyRisk')} {fed.get('vote_text', 'vote n/a')} "
                         f"({ACTION_LABELS.get(fed.get('action'), fed.get('action'))}) at {fmt(fed.get('lower'))}-"
                         f"{fmt(fed.get('upper'))}% on {fed.get('decision_date')}{_after_prev(facts, fed)}, "
                         f"{plural(hd, 'hike dissent')}, guidance {GUIDANCE_LABELS.get(F['guidance'], F['guidance'])} "
                         f"({fmt(o['subs']['FedPolicyRisk']['statement_press_conference_minutes_guidance'], 0)}; "
                         f"{F['guidance_rule']}); SEP {sep.get('latest_sep_date')} current-year median "
                         f"{sep.get('current_year_median')} ({F['sep_change']:+.1f} vs prior SEP), midpoint "
                         f"{F['gap_to_long_run']:+.3f}pp vs {sep.get('long_run_median')} longer-run",
    }
    order = sorted(WEIGHTS, key=lambda k: WEIGHTS[k] * o["comp"][k], reverse=True)
    return [txt[k] for k in order[:3]]


def _pen_delta(base_pens, new_pens):
    gone = [p for p in base_pens if p not in new_pens]
    added = [p for p in new_pens if p not in base_pens]
    parts = [f"-{CONF_PENALTIES[p]} {p} removed" for p in gone] + [f"-{CONF_PENALTIES[p]} {p} added" for p in added]
    return ", ".join(parts) if parts else "penalties unchanged"


def trigger_lines(o, rf, facts, sens):
    I, L = o["raw"]["InflationRisk"], o["raw"]["LaborRecessionRisk"]
    fed = rf.get("fed") or {}
    asof = o["rates_asof"]
    by = sens["by_key"]
    pr = sens["prints"]
    fomc = _cat(facts, "FOMC", asof)
    cpi_d = _cat(facts, f"{month_name(pr['cpi_month'])} CPI", asof)
    pce_d = _cat(facts, f"{month_name(pr['pce_month'])} Personal Income", asof)
    pm = month_name(pr["pce_month"]).split()[0]
    cm = month_name(pr["cpi_month"]).split()[0]
    T = THRESH
    hike, combo = by["hike"], by["hike_explicit"]
    thr = T["hike_voters_raise"]
    three = by.get(f"dissents_{thr}")
    hd = fed.get("hike_dissents", 0)
    t = I.get("direction_terms") or [None] * 4
    cpi_mom = "stays above" if t[3] == 1 else "moves above"
    be3 = pr.get("pce_mm_for_3m_raise")
    raise_t = (f"Raise: core PCE 3m annualized {crosses(I['CorePCE_3m_ann'], T['core_pce_3m_raise'])} "
               f"{T['core_pce_3m_raise']}% (now {I['CorePCE_3m_ann']:.2f}%, {o['periods']['pce']}"
               + (f"; needs {article(pm)} {pm} core PCE print above {be3:+.2f}% m/m" if be3 is not None else "")
               + f") while core CPI momentum {cpi_mom} its YoY, or the FOMC hikes at {fomc} (hike alone "
               f"{hike['weighted']:.2f} -> {hike['score']}; with explicit-tightening guidance {combo['weighted']:.2f} -> "
               f"{combo['score']}), or hike-preferring voters reach {thr} (now {hd}"
               + (f"; {thr} alone {three['weighted']:.2f} -> {three['score']})" if three else ")"))
    bel = pr.get("pce_mm_for_yoy_lower")
    lower_t = (f"Lower: core PCE YoY {crosses(I['CorePCE_YoY'], T['core_pce_yoy_lower'], above=False)} "
               f"{T['core_pce_yoy_lower']}% (now {I['CorePCE_YoY']:.2f}%, {o['periods']['pce']}"
               + (f"; needs {article(pm)} {pm} core PCE print below {bel:+.2f}% m/m" if bel is not None else "")
               + f") and the payroll 3m average {crosses(L['payroll_3m_avg_k'], T['payroll_lower_k'])} "
               f"{T['payroll_lower_k']:.0f}k (now {L['payroll_3m_avg_k']:.1f}k, {o['periods']['labor']})")
    dis = [by[k] for k in sorted((k for k in by if k.startswith("dissents_") and int(k.split("_")[1]) < hd),
                                 key=lambda k: -int(k.split("_")[1]))]
    if dis:
        lower_t += ("; or hike dissents fall"
                    + (f" below the spec's {T['hawkish_hike_voters']}-voter hawkish basis, which also returns guidance to "
                       f"the statement's {GUIDANCE_LABELS.get(fed.get('guidance_statement'), '?')} "
                       if sens.get("balanced_statement_blocked") else " ")
                    + "(" + "; ".join(f"{x['label'].split(' instead')[0]}: guidance "
                                      f"{GUIDANCE_SCORES.get(x['guidance'], 'n/a')}, {x['weighted']:.2f} -> {x['score']}"
                                      for x in dis) + ")")
    if "guidance_balanced" in by:
        g = by["guidance_balanced"]
        lower_t += f"; or guidance shifts to balanced (alone {g['weighted']:.2f} -> {g['score']})"
    elif sens.get("balanced_statement_blocked"):
        lower_t += "; a balanced statement alone does not lower guidance while two or more voters prefer a hike"
    # Most uncertain signal: CPI/PCE direction conflict, tested by recomputed next-print scenarios.
    if I.get("direction_conflict"):
        pc = {x["tag"]: x for x in pr["pce"]}
        pce_txt = "; ".join(f"{x['tag']} +{x['mm']:.2f}% -> {x['weighted']:.2f} ({x['score']}), terms {x['terms']}"
                            for x in pr["pce"])
        flat = pr["joint"][0]
        cpi0 = next((x for x in pr["cpi"] if x["key"] == "cpi_ordinary"), None)
        cpiy = next((x for x in pr["cpi"] if x["key"] == "cpi_ordinary_yoy"), None)
        third = (f"Most uncertain, CPI/PCE direction conflict: core CPI 3m ann {I['CoreCPI_3m_ann']:.2f}% is "
                 f"{'above' if t[3] == 1 else 'not above'} its {I['CoreCPI_YoY']:.2f}% YoY while core PCE 3m ann "
                 f"{I['CorePCE_3m_ann']:.2f}% is {'below' if t[2] == -1 else 'not below'} its {I['CorePCE_YoY']:.2f}% YoY. "
                 f"Recomputed {pm} core PCE ({pce_d}; headline PCE same m/m, real PCE not modelled so GrowthRisk "
                 f"reweights): {pce_txt}; {flat['label']} -> {flat['weighted']:.2f} ({flat['score']})")
        if "ordinary" in pc:
            third = third.replace(f"Recomputed {pm}", f"Ordinary {pm} core PCE +{pc['ordinary']['mm']:.2f}% m/m -> "
                                                       f"{pc['ordinary']['score']}. Recomputed {pm}", 1)
        if cpi0:
            if cpi0["yoy_overrides_used"]:
                third += (f". {cm} CPI ({cpi_d}) with the official BLS YoY in the facts: +0.25% core -> "
                          f"{cpi0['weighted']:.2f} ({cpi0['score']}), confidence {cpi0['confidence']}")
            else:
                third += (f". {cm} CPI ({cpi_d}) cannot settle the conflict from FRED alone: its t-12 level is blank, so "
                          f"without the official BLS YoY core_cpi_yoy and inflation_direction drop out (+0.25% core -> "
                          f"{cpi0['weighted']:.2f} ({cpi0['score']}), confidence {cpi0['confidence']}: "
                          f"{_pen_delta(o['penalties'], cpi0['penalties'])})")
                if cpiy:
                    third += (f"; with the official YoY supplied the direction terms return ({cpiy['terms']}, "
                              f"{cpiy['weighted']:.2f} -> {cpiy['score']}, confidence {cpiy['confidence']}: "
                              f"{_pen_delta(o['penalties'], cpiy['penalties'])})")
    elif sens.get("divergence_750"):
        dv = sens["divergence_750"]
        third = (f"Most uncertain, labor survey divergence: household minus payroll 3m gap {dv['diff_k']:.0f}k is "
                 f"{dv['gap_k']:.0f}k from the {T['divergence_material_k']:.0f}k line; crossing it scores "
                 f"{dv['divergence']:.0f} ({dv['weighted']:.2f} -> {dv['score']}), confidence {dv['confidence']}")
    else:
        third = (f"Most uncertain, Fed reaction at {fomc}: guidance and dissents are secondary-sourced; a hike gives "
                 f"{hike['weighted']:.2f} -> {hike['score']}")
    return [raise_t, lower_t, third]


GOVERNANCE_NOTE = ("Supervisor self-run at Jason's request (2026-10-30: 'it would be much easier going forward if you "
                   "could simply run them all yourself'), replacing the external agent; methodology unchanged.")


def cpi_gap_note(d, o, rf, facts):
    """Disclose blank CPI months (FRED) and which current/upcoming CPI runs need an official YoY."""
    p = o["periods"]["cpi"]
    since = ym_shift(p, -27)  # gaps old enough to matter neither for YoY nor the t-3 YoY are ignored
    gaps = {}
    for sid in CPI_SERIES:
        for g in d.gaps(sid, since):
            gaps.setdefault(g, []).append(sid)
    if not gaps:
        return None
    I = o["raw"]["InflationRisk"]
    causes = facts.get("known_data_gaps") or {}
    parts = []
    for g, sids in sorted(gaps.items()):
        cause = (causes.get(g) or {}).get("cause", "cause not supplied in the facts")
        yoy_m, t3_m = ym_shift(g, 12), ym_shift(g, 15)
        txt = f"CPI data gap: FRED {', '.join(sids)} blank for {g} ({cause}; no synthetic level created). "
        used = [u for u in I.get("yoy_overrides_used") or []]
        miss = [m for m in I.get("yoy_missing") or []]
        if used or miss:
            txt += (f"This run ({p}) is affected: "
                    + "; ".join([f"{u['metric']} {u['month']} uses official YoY {u['value']} ({u['source']}, {u['date']})"
                                 for u in used] + [f"{m['metric']} {m['month']} missing ({m['why']})" for m in miss])
                    + ". ")
        else:
            txt += f"This run ({p}) is unaffected (its t-12 and t-3 YoY inputs exist). "
        if yoy_m > p:
            rel_d = _cat(facts, f"{month_name(yoy_m)} CPI", o["rates_asof"])
            have = [k for k in ("cpi_yoy_override", "core_cpi_yoy_override") if yoy_m in (rf.get(k) or {})]
            txt += (f"The {month_name(yoy_m)} CPI due {rel_d} will need the official BLS headline and core YoY in the "
                    f"facts (cpi_yoy_override / core_cpi_yoy_override for {yoy_m}, with source URL and date) because its "
                    f"t-12 level {g} is blank"
                    + (f" (present now: {', '.join(have)})" if have else " (none supplied yet)")
                    + f"; without them core_cpi_yoy and inflation_direction ({100 * (SUBWEIGHTS['InflationRisk']['core_cpi_yoy'] + SUBWEIGHTS['InflationRisk']['inflation_direction']):.0f}% "
                    f"of InflationRisk) are reweighted, confidence takes -{CONF_PENALTIES['required_submetric_missing_but_reweightable']} "
                    f"required_submetric_missing_but_reweightable, and the headline shock uses PCE only. ")
        if t3_m > p:
            txt += f"Keep the {yoy_m} core YoY override through the {t3_m} CPI run (its t-3 direction term)"
        parts.append(txt.strip())
    return " ".join(parts)


def _validation_attribution(facts, val):
    """Which vintage-matched overrides the validation run used, from the facts."""
    rf9 = facts["runs"][VALIDATION_DATE]
    per = rf9.get("periods") or {}
    used = []
    if rf9.get("as_reported_pce"):
        used.append(f"{per.get('pce', '?')} PCE")
    if (rf9.get("gdp_vintage") or {}).get("as_reported"):
        used.append(f"{per.get('gdp', '?')} GDP")
    pay = sorted(rf9.get("as_reported_payrolls_k") or {})
    if pay:
        used.append(f"{pay[0]} to {pay[-1]} payrolls" if len(pay) > 1 else f"{pay[0]} payrolls")
    return used


def note_lines(o, rf, facts, sens, val, dec, d=None, strict_live=None):
    I, L, G = o["raw"]["InflationRisk"], o["raw"]["LaborRecessionRisk"], o["raw"]["GrowthRisk"]
    F = o["raw"]["FedPolicyRisk"]
    fed, sep = rf.get("fed") or {}, rf.get("sep") or {}
    pr = o.get("prior") or {}
    notes = [GOVERNANCE_NOTE]
    pens = " ".join(f"-{CONF_PENALTIES[p]} {p}" for p in o["penalties"])
    src = ""
    if "source_discrepancy_not_fully_resolved" in o["penalties"]:
        sec = [n for n, k in (("Fed action/vote/guidance", fed), ("SEP medians", sep),
                              ("the payroll revision", rf.get("payroll_revision") or {}),
                              ("the GDP vintage", rf.get("gdp_vintage") or {})) if k.get("sources")]
        hosts = facts.get("primary_hosts_unreachable") or []
        sec_txt = (", ".join(sec[:-1]) + " and " + sec[-1]) if len(sec) > 1 else (sec[0] if sec else "facts")
        src = (f". source_discrepancy: {sec_txt} are secondary-sourced"
               + (f" ({', '.join(hosts)} unreachable)" if hosts else ""))
    notes.append(f"Confidence {CONF_BASE} {pens} = {o['confidence']} (spec base 95, clamp 40-95); the same value feeds "
                 f"this packet and the CSV Confidence column{src}")
    gap = cpi_gap_note(d, o, rf, facts) if d is not None else None
    if gap:
        notes.append(gap)
    dn = "; ".join(f"{x['label']} -> {x['weighted']:.3f} ({x['score']})" for x in sens["down"])
    dis_rule = (f"; dissent cases apply the spec's hawkish basis, so below {THRESH['hawkish_hike_voters']} hike voters "
                f"guidance falls to the statement's {GUIDANCE_LABELS.get(fed.get('guidance_statement'), '?')}"
                if sens.get("balanced_statement_blocked") else "")
    notes.append(f"Rounding sensitivity, down: weighted {sens['weighted']:.3f} is {sens['margin_down']:.3f} above the "
                 f"half-up edge {sens['lo_edge']:.1f}, so {sens['score']} becomes {sens['score'] - 1} if any one holds (one at a "
                 f"time, all else held{dis_rule}): {dn}")
    cb = ", ".join(f"{k} {v[1]:+.2f}" for k, v in sens["component_breakeven"].items())
    cbd = ", ".join(f"{k} {v[0]:+.2f}" for k, v in sens["component_breakeven"].items())
    upx = "; ".join(f"{x['label']} -> {x['weighted']:.3f} ({x['score']})" for x in sens["up"])
    rbu = sens.get("revision_breakeven_up")
    srb = sens.get("sep_lr_breakeven_up")
    notes.append(f"Rounding sensitivity, up: {sens['margin_up']:.3f} below {sens['hi_edge']:.1f}; reaching "
                 f"{sens['score'] + 1} needs a component move of {cb} (down-edge equivalents {cbd}); facts: {upx}; "
                 f"payroll revision break-even {fmt(rbu, 1)}k (also costs {CONF_PENALTIES['combined_two_month_payroll_revision_at_or_below_minus_100k']} "
                 f"confidence at {THRESH['revision_penalty_k']:.0f}k or worse); longer-run SEP break-even {fmt(srb, 2)}")
    prn = sens["prints"]
    allp = prn["pce"] + prn["joint"] + prn["cpi"]
    notes.append("Next-print scenarios (series extended in memory, full recompute; not forecasts): "
                 + "; ".join(f"{x['label']} -> {x['weighted']:.2f} ({x['score']}), conf {x['confidence']}"
                             + (f", missing {', '.join(m.split('.')[1] for m in x['missing'])}" if x["missing"] else "")
                             for x in allp))
    dv = sens.get("divergence_750")
    if dv:
        notes.append(f"Confidence sensitivity: household-establishment 3m gap {L['survey_diff_k']:.0f}k (household "
                     f"{L['household_3m_chg_k']:+.0f}k vs payroll {L['establishment_3m_chg_k']:+.0f}k, "
                     f"{'same' if L['survey_same_sign'] else 'opposite'} sign) is {dv['gap_k']:.0f}k below "
                     f"{THRESH['divergence_material_k']:.0f}k; household level +{dv['gap_k']:.0f}k recomputed gives "
                     f"divergence {L['divergence_score']:.0f}->{dv['divergence']:.0f}, {dv['weighted']:.3f} ({dv['score']}) "
                     f"and confidence {dv['confidence']}")
    if pr:
        rows = ", ".join(f"{k} {rv + ec:+.2f} (rev {rv:+.2f}, econ {ec:+.2f})" for k, (rv, ec) in dec["rows"].items())
        notes.append(f"Change vs prior {pr['score']} ({pr['asof']}): {o['delta']:+d}, '{o['change_class']}'. Weighted "
                     f"{published_weighted(pr):.3f} -> {o['weighted']:.3f} = revision/method {dec['revision']:+.2f} + "
                     f"like-for-like economic {dec['economic']:+.2f}; {rows}")
    cur, asr = val["cur"], val["asr"]
    pub = val["pub"]
    used = _validation_attribution(facts, val)
    rf9 = facts["runs"][VALIDATION_DATE]
    gi = asr["comp"]["InflationRisk"] - pub["InflationRisk"]
    if not val["sc_current"] and val["sc_vintage"] and used:
        attrib = (f"current-vintage outside, vintage-matched within once as-reported {', '.join(used)} replace the "
                  f"FRED current vintage, so the excess is attributed to FRED revisions to those inputs")
    else:
        attrib = (f"current-vintage {'within' if val['sc_current'] else 'outside'}, vintage-matched "
                  f"{'within' if val['sc_vintage'] else 'outside'}"
                  + (f" (as-reported overrides: {', '.join(used)})" if used else " (no as-reported overrides in the facts)"))
    infl_txt = ""
    if abs(gi) > 1.0:
        t3 = ym_shift((rf9.get("periods") or {}).get("pce", "0000-01"), -3)
        have_t3 = "CorePCE_YoY_t_minus_3m" in (rf9.get("as_reported_pce") or {})
        infl_txt = (f"; the Inflation gap {gi:+.2f} is not fully explained"
                    + ("" if have_t3 else f" (as-published {t3} core PCE YoY not in the facts, so direction terms stay "
                                          f"on the current vintage)"))
    notes.append(f"Validation {VALIDATION_DATE}: against the published unrounded {val['pub_weighted']:.3f} (rebuilt from "
                 f"its published components), current-vintage recompute {cur['weighted']:.2f} (diff "
                 f"{val['diff_current']:+.2f}, rounds {cur['score']}) and vintage-matched {asr['weighted']:.2f} (diff "
                 f"{val['diff_vintage']:+.2f}, rounds {asr['score']}); RatesCurveRisk {cur['comp']['RatesCurveRisk']:.2f} vs "
                 f"{pub['RatesCurveRisk']}; vintage-matched gaps Inflation {gi:+.2f}, Labor "
                 f"{asr['comp']['LaborRecessionRisk'] - pub['LaborRecessionRisk']:+.2f}, Growth "
                 f"{asr['comp']['GrowthRisk'] - pub['GrowthRisk']:+.2f}; tolerance 3 points: {attrib}{infl_txt}")
    cs, ass = val["cur_strict"], val["asr_strict"]
    if strict_live is not None:
        same = abs(strict_live["weighted"] - o["weighted"]) < 1e-9
        live_txt = (f"the {o['rates_asof']} run is {'unaffected' if same else 'affected'} by that rule "
                    f"(strict {strict_live['weighted']:.3f} -> {strict_live['score']}; "
                    f"{'same' if L.get('survey_same_sign') else 'opposite'} signs)")
    else:
        live_txt = f"the {o['rates_asof']} run was not re-tested under the strict rule"
    notes.append(f"The {VALIDATION_DATE} Labor recompute depends on the 'approximately flat' household rule (|3m change| "
                 f"< {DIVERGENCE_FLAT_K:.0f}k; household {cur['raw']['LaborRecessionRisk']['household_3m_chg_k']:+.0f}k vs "
                 f"payroll {cur['raw']['LaborRecessionRisk']['establishment_3m_chg_k']:+.0f}k): it gives divergence "
                 f"{cur['raw']['LaborRecessionRisk']['divergence_score']:.0f} and vintage-matched Labor "
                 f"{asr['comp']['LaborRecessionRisk']:.2f} (published {pub['LaborRecessionRisk']}); the strict rule "
                 f"(opposite signs -> 70) gives current-vintage {cs['weighted']:.2f} -> {cs['score']} not {cur['score']}, "
                 f"and vintage-matched {ass['weighted']:.2f} -> {ass['score']}; {live_txt}")
    gscore = o["subs"]["FedPolicyRisk"]["statement_press_conference_minutes_guidance"]
    mrel = fed.get("minutes_released")
    mon = month_name(fed.get("decision_date", "0000-01")[:7]).split()[0] if fed.get("decision_date") else "latest"
    if mrel is True:
        mtxt = (f"{mon} minutes released {fed.get('minutes_date', '(date not supplied)')}, minutes adjustment "
                f"{fed.get('minutes_adjustment', 0):+.0f}")
    elif mrel is False:
        mtxt = f"{mon} minutes not yet released, so no minutes adjustment"
    else:
        mtxt = f"{mon} minutes status not supplied in the facts; minutes adjustment {fed.get('minutes_adjustment', 0):+.0f}"
    mshort = mtxt.split(",")[0] if mrel is False else mtxt
    mm = ("; facts 'guidance' disagrees with the rule-derived classification" if F.get("guidance_facts_mismatch") else "")
    vg = cur["raw"]["FedPolicyRisk"].get("guidance")
    notes.append(f"FedPolicyRisk guidance={GUIDANCE_SCORES.get(vg, 'n/a')} ('{GUIDANCE_LABELS.get(vg, vg)}') at {VALIDATION_DATE} was chosen with reference to a back-solve "
                 f"from the published {pub['FedPolicyRisk']} (back-solve {val['fed_guidance_backsolve']:.2f}; statement "
                 f"text not retrieved), so the {cur['comp']['FedPolicyRisk']:.2f} Fed match and the self-test are not "
                 f"independent evidence; {fed.get('decision_date')} guidance {gscore:.0f} "
                 f"({GUIDANCE_LABELS.get(F['guidance'], F['guidance'])}) rests on {F['guidance_rule']}; {mtxt}{mm}")
    rd = rf.get("release_dates") or {}
    gv = rf.get("gdp_vintage") or {}
    per = o["periods"]
    ref = (f"labor/CPI/PCE ref {per['labor']}" if per["labor"] == per["cpi"] == per["pce"]
           else f"labor/CPI/PCE ref {per['labor']}/{per['cpi']}/{per['pce']}")
    after_close = gv.get("release_date") and gv["release_date"] > o["rates_asof"]
    no_sep = sep.get("latest_sep_date") != fed.get("decision_date")
    rsrc = rf.get("release_dates_source")
    rver = rf.get("release_dates_primary_verified")
    corro = gv.get("headline_corroborated")
    notes.append(f"Data: FRED current vintage only (fredgraph; no ALFRED). Mixed dates: Treasury {o['rates_asof']} close; "
                 f"{ref} (released {rd.get('labor')}, {rd.get('cpi')}, {rd.get('pce')}); "
                 f"GDP {per['gdp']} {gv.get('vintage')} released {gv.get('release_date')}"
                 + (" (after the rates close)" if after_close else "") + "; "
                 + (f"no SEP at the {fed.get('decision_date')} meeting, latest SEP {sep.get('latest_sep_date')} vs "
                    f"{sep.get('prior_sep_date')}; " if no_sep else f"SEP {sep.get('latest_sep_date')} vs {sep.get('prior_sep_date')}; ")
                 + f"{mshort}. Release dates from {rsrc or 'source not stated'}"
                 + (", verified at BLS/BEA/Fed" if rver else ", not verified at BLS/BEA/Fed")
                 + (f"; {per['gdp']} GDP {fmt(G['Real_GDP_SAAR'], 1)}% is from FRED with no corroborating release text found"
                    if corro is False else ""))
    cats = "; ".join(f"{c['date']} {c['time_et']} ET {c['event']}" for c in sorted(facts.get("catalysts") or [],
                                                                             key=lambda c: c["date"])[:5])
    notes.append(f"Interpretive (not scored): recession risk {o['recession']}; net policy impulse {o['impulse']}. "
                 f"Next catalysts: {cats}. Decision support only; not financial advice")
    return notes


def packet(o, rf, facts, sens, val, dec, d=None, strict_live=None):
    I, L, G = o["raw"]["InflationRisk"], o["raw"]["LaborRecessionRisk"], o["raw"]["GrowthRisk"]
    R, F, p = o["raw"]["RatesCurveRisk"], o["raw"]["FedPolicyRisk"], o["periods"]
    fed, sep = rf.get("fed") or {}, rf.get("sep") or {}
    gv, rd = rf.get("gdp_vintage") or {}, rf.get("release_dates") or {}
    rv = rf.get("payroll_revision") or {}
    ra = o["rates_asof"]
    conf = o["confidence"]  # single source for packet and CSV
    pr = o.get("prior") or {}
    asof = (f"{ra} close (Treasury); labor/CPI/PCE ref {p['labor']}/{p['cpi']}/{p['pce']}; GDP {p['gdp']} "
            f"{gv.get('vintage')} (released {gv.get('release_date')}); FOMC {fed.get('decision_date')}")
    div_txt = (f"{L['divergence_score']:.0f} (household {L['household_3m_chg_k']:+.0f}k vs payroll "
               f"{L['establishment_3m_chg_k']:+.0f}k over 3m, gap {L['survey_diff_k']:.0f}k, "
               f"{'same' if L['survey_same_sign'] else 'opposite'} sign)")
    def yb(k):
        b = (I.get("yoy_basis") or {}).get(k, "computed")
        return "NSA" if b == "computed" else b

    km = [
        f"DGS10={R['DGS10']:.2f}% ({ra} close)", f"DGS3MO={R['DGS3MO']:.2f}% ({ra} close)",
        f"DGS2={R['DGS2']:.2f}% ({ra} close)", f"spread_10y_3m={R['spread_10y_3m']:+.2f}pp ({ra} close)",
        f"spread_10y_2y={R['spread_10y_2y']:+.2f}pp ({ra} close)", f"spread_2y_3m={R['spread_2y_3m']:+.2f}pp ({ra} close)",
        f"last_10y_3m_inversion={R['last_negative_10y_3m']} ({R['months_since_last_negative_10y_3m']:.1f} months before {ra})",
        f"UNRATE={L['UNRATE']:.1f}% ({p['labor']})", f"SAHMCURRENT={L['SAHMCURRENT']:.2f}pp ({p['labor']})",
        f"payroll_change={L['payroll_change_k']:+.0f}k ({p['labor']})",
        f"payroll_3m_average={L['payroll_3m_avg_k']:.1f}k ({ym_shift(p['labor'], -2)} to {p['labor']})",
        f"payroll_revision={L['payroll_revision_2m_k']:+.0f}k (reported {rv.get('reported')}; {rv.get('detail')})",
        f"labor_force_participation={L['CIVPART']:.1f}% ({p['labor']}; 6m {L['CIVPART_6m_chg']:+.1f}pp)",
        f"EMRATIO={L['EMRATIO']:.1f}% ({p['labor']}; 6m {L['EMRATIO_6m_chg']:+.1f}pp)",
        f"survey_divergence={div_txt} ({p['labor']})",
        f"CPI_MoM={fmt(I['CPI_mm'], 1)}% ({p['cpi']}, SA)", f"CPI_YoY={fmt(I['CPI_YoY'], 1)}% ({p['cpi']}, {yb('CPI_YoY')})",
        f"CoreCPI_MoM={fmt(I['CoreCPI_mm'], 1)}% ({p['cpi']}, SA)", f"CoreCPI_YoY={fmt(I['CoreCPI_YoY'], 1)}% ({p['cpi']}, {yb('CoreCPI_YoY')})",
        f"PCE_MoM={I['PCE_mm']:.1f}% ({p['pce']})", f"PCE_YoY={I['PCE_YoY']:.1f}% ({p['pce']})",
        f"CorePCE_MoM={I['CorePCE_mm']:.1f}% ({p['pce']})", f"CorePCE_YoY={I['CorePCE_YoY']:.1f}% ({p['pce']})",
        f"CoreCPI_3m_ann={I['CoreCPI_3m_ann']:.2f}% ({p['cpi']})", f"CorePCE_3m_ann={I['CorePCE_3m_ann']:.2f}% ({p['pce']})",
        f"inflation_direction_terms={I['direction_terms']} ({p['cpi']}/{p['pce']}; conflict {'yes' if I['direction_conflict'] else 'no'})",
        f"Real_GDP_SAAR={G['Real_GDP_SAAR']:.1f}% ({p['gdp']} {gv.get('vintage')} estimate)",
        f"Private_domestic_final_sales_SAAR={G['Private_final_sales_SAAR']:.1f}% ({p['gdp']} {gv.get('vintage')} estimate)",
        f"Real_PCE_3m_ann={G['Real_PCE_3m_ann']:.2f}% ({p['pce']})",
        f"Fed_target_range={fed.get('lower'):.2f}-{fed.get('upper'):.2f}% ({fed.get('decision_date')})",
        f"FOMC_vote={fed.get('vote_text')}, {fed.get('hike_dissents')} hike / {fed.get('cut_dissents')} cut dissents ({fed.get('decision_date')})",
        f"FOMC_guidance={F['guidance']} ({fed.get('decision_date')}; subscore "
        f"{fmt(o['subs']['FedPolicyRisk']['statement_press_conference_minutes_guidance'], 0)}; {F['guidance_rule']})",
        f"SEP_change=current-year median {sep.get('current_year_median')} vs {sep.get('prior_current_year_median')} "
        f"({F['sep_change']:+.1f}pp; {sep.get('latest_sep_date')} vs {sep.get('prior_sep_date')}); longer-run {sep.get('long_run_median')}",
    ]
    km += [f"{k}={o['comp'][k]:.1f}" + (f" (prior {pr[k]})" if k in pr else "") for k in WEIGHTS]
    km.append(f"MacroRiskScore={o['score']} (weighted {o['weighted']:.3f}"
              + (f"; prior {pr['score']} at {pr['asof']}, change {o['delta']:+d} '{o['change_class']}')" if pr else ")"))
    km.append(f"Macro_regime={o['regime']}{' (borderline)' if o['regime_borderline'] else ' (not borderline)'} ({ra})")
    km.append(f"CONFIDENCE={conf}")
    lines = ["SUPERVISOR PACKET (COPY/PASTE)", f"MODULE: {PACKET_MODULE}", f"ASOF: {asof}",
             f"SCORE_0_100: {o['score']}", f"CONFIDENCE_0_100: {conf}", "TOP_3_DRIVERS:"]
    lines += [f"- {x}" for x in driver_lines(o, rf, facts)]
    lines.append("CHANGE_MY_MIND (TRIGGERS):")
    lines += [f"- {x}" for x in trigger_lines(o, rf, facts, sens)]
    lines.append("KEY_METRICS:")
    lines += [f"- {x}" for x in km]
    lines.append("NOTES:")
    lines += [f"- {x}" for x in note_lines(o, rf, facts, sens, val, dec, d, strict_live)]
    return "\n".join(lines)


def print_scenario_block(sens):
    pr = sens["prints"]
    print(f"NEXT-PRINT SCENARIOS (CPI {pr['cpi_month']}, PCE {pr['pce_month']}; in-memory series, full recompute)")
    for x in pr["pce"] + pr["joint"] + pr["cpi"]:
        print(f"  {x['label']:<78} weighted {x['weighted']:7.3f} -> {x['score']}  conf {x['confidence']}  "
              f"Infl {x['InflationRisk']:.2f}  terms {x['terms']}  conflict {x['conflict']}  "
              f"missing {x['missing'] or '-'}")
    for k in ("pce_mm_for_3m_raise", "pce_mm_for_yoy_lower"):
        if pr.get(k) is not None:
            print(f"  {k} = {pr[k]:+.4f}% m/m")
    print("POLICY SCENARIOS")
    for x in sens["down"] + sens["up"]:
        print(f"  {x['label']:<78} weighted {x['weighted']:7.3f} -> {x['score']}  Fed {x['FedPolicyRisk']:.2f}  "
              f"guidance {x['guidance']}")
    print()


def packet_csv_consistent(pkt, csv_text):
    hdr, row = csv_text.split("\n")
    vals = next(csv.reader([row]))
    cols = hdr.split(",")
    c = dict(zip(cols, vals))
    pk = {ln.split(":", 1)[0]: ln.split(":", 1)[1].strip() for ln in pkt.splitlines() if ln.startswith(("SCORE_0_100", "CONFIDENCE_0_100"))}
    return c["Confidence"] == pk["CONFIDENCE_0_100"] and c["MacroRiskScore"] == pk["SCORE_0_100"]


def self_test(cache=None, facts_path=None):
    assert abs(lininterp(5.01, ANCHORS["dgs10_percent"]) - 85.15) < 1e-9
    assert lininterp(-5, ANCHORS["sahm_rule_pp"]) == 5.0
    assert lininterp(9, ANCHORS["sahm_rule_pp"]) == 100.0
    assert sign_eps(0.1, 0.10) == 0 and sign_eps(0.11, 0.10) == 1 and sign_eps(-0.2, 0.1) == -1
    assert score_2y3m(0.60) == 70 and score_2y3m(0.20) == 25 and score_2y3m(0.21) == 55
    assert score_2y3m(-0.80) == 70 and score_2y3m(-0.30) == 55 and score_2y3m(-0.15) == 35
    assert round_half_up(44.5) == 45 and round_half_up(44.215) == 44
    assert abs(sum(WEIGHTS.values()) - 1) < 1e-12
    for comp, sw in SUBWEIGHTS.items():
        assert abs(sum(sw.values()) - 1) < 1e-12, comp
    t = SELFTEST_0916
    rc, subs, raw, _ = rates_block(t["DGS10"], t["DGS3MO"], t["DGS2"], "2026-09-16",
                                   t["last_negative_10y_3m"])
    assert abs(rc - t["published_rates_curve"]) <= 0.05, rc
    # Fed policy reproduces the agent's 69.1 at 2026-09-16 from the stated policy facts.
    fc, _, _, _ = fed_block({"lower": 3.75, "upper": 4.0, "action": "twenty_five_bp_hike",
                             "guidance": "hawkish_or_higher_for_longer", "voters": 12,
                             "hike_dissents": 0, "cut_dissents": 0},
                            {"current_year_median": 4.1, "prior_current_year_median": 3.8,
                             "long_run_median": 3.2}, 47.3, 3.0, 27.1, 27.2)
    assert abs(fc - 69.1) <= 0.05, fc
    # Validation reference is the published UNROUNDED weighted score from its components.
    pub0916 = {"InflationRisk": 47.3, "LaborRecessionRisk": 27.1, "GrowthRisk": 27.2,
               "RatesCurveRisk": 58.7, "FedPolicyRisk": 69.1}
    assert abs(published_weighted(pub0916) - 44.215) < 1e-9
    # The 09-16 guidance=75 is what a back-solve from the published 69.1 implies (disclosed).
    _, fsub, _, _ = fed_block({"lower": 3.75, "upper": 4.0, "action": "twenty_five_bp_hike",
                               "guidance": "hawkish_or_higher_for_longer", "voters": 12,
                               "hike_dissents": 0, "cut_dissents": 0},
                              {"current_year_median": 4.1, "prior_current_year_median": 3.8,
                               "long_run_median": 3.2}, 47.3, 3.0, 27.1, 27.2)
    assert abs(fed_guidance_backsolve(69.1, fsub) - 75.15) < 1e-9
    # Trigger verbs follow the metric's side of the threshold.
    assert crosses(2.90, 3.5) == "rises above" and crosses(3.6, 3.5) == "stays above"
    assert crosses(3.12, 3.0, above=False) == "falls below" and crosses(2.9, 3.0, above=False) == "stays below"
    assert _sc(44.5) == 45 and _sc(44.4999) == 44 and _sc(45.5) == 46
    print(f"offline: RatesCurveRisk(2026-09-16) = {rc:.3f} (published 58.7); "
          f"FedPolicyRisk(2026-09-16) = {fc:.3f} (published 69.1)")
    _selftest_offline_cpi_override()
    _selftest_offline_guidance()
    if not cache:
        print("cache tests SKIPPED (pass --cache DIR [--facts JSON]); offline tests only")
        print("self-test OK")
        return
    d = Data(cache)
    asof = d.latest_common_treasury("2026-09-16")
    assert asof == "2026-09-16", asof
    y = {s: d.daily[s][asof] for s in ("DGS10", "DGS3MO", "DGS2")}
    assert y == {k: t[k] for k in ("DGS10", "DGS3MO", "DGS2")}, y
    ln = d.last_negative_10y_3m(asof)
    assert ln == t["last_negative_10y_3m"], ln
    rc2, _, _, _ = rates_block(y["DGS10"], y["DGS3MO"], y["DGS2"], asof, ln)
    assert abs(rc2 - 58.7) <= 0.05, rc2
    print(f"cache: RatesCurveRisk(2026-09-16) = {rc2:.3f} from FRED cache; last 10y-3m inversion {ln}")
    if facts_path is None and os.path.exists(os.path.join(cache, "macro_facts.json")):
        facts_path = os.path.join(cache, "macro_facts.json")
    if not facts_path:
        print("facts tests SKIPPED (no --facts and no macro_facts.json in the cache)")
        print("self-test OK")
        return
    with open(facts_path) as fh:
        facts = json.load(fh)
    _selftest_live(d, facts)
    print("self-test OK")


SELFTEST_LIVE = {
    # Verified result for the 2026-10-28 run.
    "weighted": 44.5484, "score": 45, "confidence": 80,
    # Verifier's next-print expectations (October 2026 core PCE, headline PCE same m/m, real PCE
    # not modelled so GrowthRisk reweights): weighted to 2 decimals.
    "pce": {0.25: (46.75, 47), 0.30: (47.36, 47), 0.35: (47.97, 48)},
    # Both CPI and PCE 0.0% m/m (no official October CPI YoY supplied): 41.66 -> 42.
    "flat": (41.66, 42),
}


def _selftest_offline_cpi_override():
    """Synthetic series: a blank t-12 CPI level removes core CPI YoY (reweight flag); an official
    YoY override equal to the true value restores the exact gap-free result; an unsourced
    override is rejected."""
    d = Data()
    start = "2024-01"
    for sid, g in (("CPIAUCNS", 0.0030), ("CPILFENS", 0.0025), ("CPIAUCSL", 0.0030), ("CPILFESL", 0.0025),
                   ("PCEPI", 0.0024), ("PCEPILFE", 0.0022)):
        d.monthly[sid] = {}
        lv, ym = 100.0, start
        for i in range(30):
            lv *= 1 + g + (0.0004 if i % 5 == 0 else 0.0)
            d.monthly[sid][ym] = lv
            ym = ym_shift(ym, 1)
    p = "2025-12"
    full_c, full_s, full_r, full_f = inflation_block(d, p, p)
    assert full_r["yoy_missing"] == [] and full_f == []
    gap = d.clone()
    for sid in ("CPIAUCNS", "CPILFENS"):
        gap.monthly[sid][ym_shift(p, -12)] = None
    assert gap.gaps("CPILFENS") == [ym_shift(p, -12)]
    c0, s0, r0, f0 = inflation_block(gap, p, p)
    assert s0["core_cpi_yoy"] is None and s0["inflation_direction"] is None, s0
    assert f0 == ["required_submetric_missing_but_reweightable"], f0
    assert [m["metric"] for m in r0["yoy_missing"]] == ["CPI_YoY", "CoreCPI_YoY"], r0["yoy_missing"]
    ov = {p: {"value": full_r["CPI_YoY"], "source": "https://www.bls.gov/test", "date": "2026-01-01"}}
    cov = {p: {"value": full_r["CoreCPI_YoY"], "source": "https://www.bls.gov/test", "date": "2026-01-01"}}
    c1, s1, r1, f1 = inflation_block(gap, p, p, cpi_yoy_override=ov, core_cpi_yoy_override=cov)
    assert f1 == [] and abs(c1 - full_c) < 1e-12 and r1["direction_terms"] == full_r["direction_terms"], (c1, full_c)
    assert len(r1["yoy_overrides_used"]) == 2
    # An override for a month whose t-12 level exists is ignored (computed value wins).
    c2, _, r2, _ = inflation_block(d, p, p, core_cpi_yoy_override={p: {"value": 9.9, "source": "x", "date": "y"}})
    assert abs(c2 - full_c) < 1e-12 and r2["yoy_overrides_used"] == []
    try:
        inflation_block(gap, p, p, core_cpi_yoy_override={p: {"value": 3.0}})
        raise AssertionError("unsourced override accepted")
    except ValueError:
        pass
    print(f"offline: CPI t-12 gap -> reweighted ({c0:.3f}); official YoY override restores {c1:.3f} = {full_c:.3f}")


def _selftest_offline_guidance():
    base = {"action": "hawkish_hold", "voters": 12, "hike_dissents": 2, "cut_dissents": 0,
            "guidance_statement": "balanced_data_dependent"}
    assert effective_guidance(base)[0] == "hawkish_or_higher_for_longer"
    assert effective_guidance(base | {"hike_dissents": 1})[0] == "balanced_data_dependent"
    assert effective_guidance(base | {"hike_dissents": 0})[0] == "balanced_data_dependent"
    assert effective_guidance(base | {"hike_dissents": 0, "action": "twenty_five_bp_hike"})[0] == "hawkish_or_higher_for_longer"
    assert effective_guidance(base | {"guidance_statement": "explicit_likely_tightening", "hike_dissents": 0})[0] == "explicit_likely_tightening"
    nostmt = {"guidance": "hawkish_or_higher_for_longer", "hike_dissents": 0}
    assert effective_guidance(nostmt)[0] == "hawkish_or_higher_for_longer"
    print("offline: guidance follows the spec's hawkish basis (>= 2 hike voters) when a statement classification is given")


def _selftest_live(d, facts):
    E = SELFTEST_LIVE
    asof = "2026-10-28"
    rf = facts["runs"][asof]
    o = run(d, rf, asof)
    assert abs(o["weighted"] - E["weighted"]) < 5e-5 and o["score"] == E["score"] and o["confidence"] == E["confidence"], \
        (o["weighted"], o["score"], o["confidence"])
    assert o["raw"]["FedPolicyRisk"]["guidance"] == "hawkish_or_higher_for_longer"
    assert not o["raw"]["FedPolicyRisk"]["guidance_facts_mismatch"]
    # The 2025-10 shutdown gap is present on FRED and leaves the 2026-09 run untouched.
    for sid in ("CPIAUCNS", "CPILFENS", "CPILFESL"):
        assert "2025-10" in d.gaps(sid), sid
    assert o["raw"]["InflationRisk"]["yoy_missing"] == []
    base = o["periods"]
    for mm, (w, sc) in E["pce"].items():
        r = next_print(d, rf, asof, base, core_pce=mm)
        assert round(r["weighted"], 2) == w and r["score"] == sc, (mm, r["weighted"], r["score"])
        assert r["periods"]["pce"] == "2026-10"
    r0 = next_print(d, rf, asof, base, cpi=0.0, core_cpi=0.0, pce=0.0, core_pce=0.0)
    assert round(r0["weighted"], 2) == E["flat"][0] and r0["score"] == E["flat"][1], (r0["weighted"], r0["score"])
    # Without the official October CPI YoY the core YoY and direction drop out with the -5 reweight
    # penalty (the conflict penalty does not simply disappear).
    assert r0["subs"]["InflationRisk"]["core_cpi_yoy"] is None
    assert "required_submetric_missing_but_reweightable" in r0["penalties"]
    rc = next_print(d, rf, asof, base, core_cpi=0.25)
    assert "required_submetric_missing_but_reweightable" in rc["penalties"]
    assert "cpi_and_pce_direction_conflict" not in rc["penalties"] and rc["confidence"] == o["confidence"]
    ry = next_print(d, rf, asof, base, core_cpi=0.25, cpi_yoy=3.7, core_cpi_yoy=2.6)
    assert ry["subs"]["InflationRisk"]["core_cpi_yoy"] is not None
    assert "required_submetric_missing_but_reweightable" not in ry["penalties"]
    # Override equivalence on the real series: blank 2025-09 t-12 levels + official YoY = base.
    g = d.clone()
    for sid in ("CPIAUCNS", "CPILFENS"):
        g.monthly[sid]["2025-09"] = None
    I = o["raw"]["InflationRisk"]
    f = json.loads(json.dumps(rf))
    f["cpi_yoy_override"] = {"2026-09": {"value": I["CPI_YoY"], "source": "selftest", "date": "selftest"}}
    f["core_cpi_yoy_override"] = {"2026-09": {"value": I["CoreCPI_YoY"], "source": "selftest", "date": "selftest"}}
    assert abs(run(g, f, asof)["weighted"] - o["weighted"]) < 1e-12
    assert "required_submetric_missing_but_reweightable" in run(g, rf, asof)["penalties"]
    # Policy scenarios: guidance follows dissents.
    sens = sensitivity(d, rf, asof, o)
    assert sens["by_key"]["dissents_1"]["guidance"] == "balanced_data_dependent"
    assert sens["by_key"]["dissents_0"]["guidance"] == "balanced_data_dependent"
    assert sens["by_key"]["dissents_3"]["guidance"] == "hawkish_or_higher_for_longer"
    assert sens["by_key"]["dissents_1"]["FedPolicyRisk"] < o["comp"]["FedPolicyRisk"] - 3
    tl = trigger_lines(o, rf, facts, sens)
    assert not any("removes the -5 conflict penalty" in x for x in tl)
    assert "47" in tl[2] and "2026-11-10" in tl[2]
    print(f"live: 2026-10-28 {o['weighted']:.4f} -> {o['score']} / {o['confidence']}; October core PCE "
          + ", ".join(f"+{mm:.2f}% -> {v[0]:.2f}" for mm, v in E["pce"].items())
          + f"; CPI and PCE 0.0% -> {r0['weighted']:.2f} ({r0['score']}); 1 hike dissent -> guidance "
          f"{GUIDANCE_SCORES[sens['by_key']['dissents_1']['guidance']]}, {sens['by_key']['dissents_1']['weighted']:.3f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", metavar="DIR")
    ap.add_argument("--cache", metavar="DIR")
    ap.add_argument("--facts", metavar="JSON")
    ap.add_argument("--asof", metavar="YYYY-MM-DD")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--json", metavar="OUT")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        self_test(a.cache, a.facts)
        return 0
    if a.fetch:
        fetch(a.fetch)
        if not a.cache:
            return 0
    if not a.cache or not a.facts:
        ap.error("--cache and --facts are required to compute")
    d = Data(a.cache)
    with open(a.facts) as fh:
        facts = json.load(fh)
    rates_asof = d.latest_common_treasury(a.asof)
    keys = sorted(k for k in facts["runs"] if k <= rates_asof)
    if not keys:
        ap.error(f"no facts run at or before {rates_asof}")
    rf = facts["runs"][keys[-1]]
    if keys[-1] != rates_asof:
        print(f"WARNING: facts run {keys[-1]} used for rates as-of {rates_asof}", file=sys.stderr)
    o = run(d, rf, rates_asof)
    print(report(o, rf))
    print()
    csv_text = csv_row(o, rf)
    print(csv_text)
    val = dec = None
    if VALIDATION_DATE in facts["runs"]:
        print()
        val = validate(d, facts, quiet=not a.validate)
        if o.get("prior"):
            dec = decompose(o, val["cur"], o["prior"], quiet=not a.validate)
            if a.validate:
                print()
    sens = sensitivity(d, rf, rates_asof, o)
    o["sensitivity"] = sens
    strict_live = run(d, rf, rates_asof, flat_k=0.0)
    print_scenario_block(sens)
    if val is None or dec is None:
        print("NOTE: no validation run / prior in facts; packet not emitted", file=sys.stderr)
    else:
        pkt = packet(o, rf, facts, sens, val, dec, d, strict_live)
        if not packet_csv_consistent(pkt, csv_text):
            raise SystemExit("packet and CSV disagree on score/confidence")
        print()
        print("```")
        print(pkt)
        print("```")
        o["packet"] = pkt
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(o, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
