#!/usr/bin/env python3
"""Valuation / Expected Returns module calculator (stdlib only).

Implements supervisor/modules/valuation_expected_returns.yaml:
  current_scoring_methodology (30/20/10/25/15 formula, inclusive empirical percentile,
  institutional nominal-forecast mapping with range / real-return / provider / min-count rules),
  data_pipeline (history windows, price_stamp_reconciliation with k and tolerance bands,
  forward-PE rule, asof_selection_rule, incomplete_row_rule incl. stale sales and stale
  forecasts), calculations, supervisor_integration and output_contract.

Data
  Multpl by-year tables (Shiller-derived history proxy, permitted by splice_or_proxy_rules)
  and Multpl current pages (embedded S&P 500 price + stamp, sales-denominator date);
  FRED fredgraph.csv for DGS10 / DFII10 / T10YIE; State Street SPY page for FY1 P/E.
  Facts that cannot be fetched (official SPX close, institutional forecasts, context items)
  come from a --facts JSON file and are treated as secondary-sourced.

Packet text rule: every factual sentence is either computed from the cached tables or printed
  from a --facts field that carries its own source and date; anything else is omitted. Facts
  keys read beyond official_spx_close / forecasts / context: official_spx_close.{official_sp_dji,
  source_label, venue_timestamp, substitute_reason, cache_cross_check.excluded_bars,
  prior_reconciliations}, forecasts[].{corroborated, source_count, source_kind,
  publication_date_kind, fallback, cadence_months, inflation_assumption},
  context.market_cap_to_gdp.prior_carried, prior_run, governance, and searches (e.g.
  searches.ps_sales_alternate; a "none found" claim is printed only from such a record).

Conventions (resolved empirically against the agent of record; see --self-test)
  Percentile: one observation per calendar year by row date through the prior year. Multpl's
    "Jan 1, <current year>" row is a CURRENT-YEAR row and is removed as a duplicate of the
    current observation; the dated current row is the single appended observation.
    (P/S rows are dated Dec 31, so Dec 31 <prior year> is the prior-year observation.)
  ERP alignment: for each year Y >= 2000, tightness_Y = mean(DGS10 over January Y) minus the
    Multpl Jan 1 Y earnings yield. That January average is exactly Multpl's own "Jan 1, Y"
    10-year row (cross-checked on every run).

Usage
    python3 tools/valuation_expected_returns.py --fetch DIR
    python3 tools/valuation_expected_returns.py --cache DIR --facts FACTS.json
        [--asof YYYY-MM-DD] [--json OUT.json]
    python3 tools/valuation_expected_returns.py --self-test --cache DIR

Decision support only; not financial advice.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------------------
# Spec constants (supervisor/modules/valuation_expected_returns.yaml). Keep in sync.
# --------------------------------------------------------------------------------------
WEIGHTS = {
    "cape": 0.30,
    "trailing_pe": 0.20,
    "price_to_sales": 0.10,
    "erp_tightness": 0.25,
    "institutional": 0.15,
}
# institutional_forecast_mapping.anchor_points (expected nominal return %, risk)
FORECAST_ANCHORS = [(0.0, 95.0), (2.0, 80.0), (4.0, 60.0), (6.0, 35.0), (8.0, 10.0)]
TOL_NO_ADJUST = 0.0010      # |discrepancy| <= 0.10% may be treated as rounding
TOL_REJECT = 0.0100         # > 1.0% -> reject or find alternate
ERP_START_YEAR = 2000
NEUTRAL_RISK = 50.0
METHODOLOGY_VERSION = "VALUATION_V2_NOMINAL_FORECAST_MAPPING"

UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
UA_FRED = "Python-urllib/3"  # fred.stlouisfed.org drops connections for browser UAs here

MULTPL = "https://www.multpl.com"
SOURCES = {
    # cache file: url
    "cape_by_year.html": MULTPL + "/shiller-pe/table/by-year",
    "pe_by_year.html": MULTPL + "/s-p-500-pe-ratio/table/by-year",
    "ps_by_year.html": MULTPL + "/s-p-500-price-to-sales/table/by-year",
    "ey_by_year.html": MULTPL + "/s-p-500-earnings-yield/table/by-year",
    "dy_by_year.html": MULTPL + "/s-p-500-dividend-yield/table/by-year",
    "tsy10_by_year.html": MULTPL + "/10-year-treasury-rate/table/by-year",
    "spx_price.html": MULTPL + "/s-p-500-historical-prices",
    "ps_page.html": MULTPL + "/s-p-500-price-to-sales",
    "ey_page.html": MULTPL + "/s-p-500-earnings-yield",
    "dy_page.html": MULTPL + "/s-p-500-dividend-yield",
    "tsy10_page.html": MULTPL + "/10-year-treasury-rate",
    "sps_page.html": MULTPL + "/s-p-500-sales",
    "sps_by_quarter.html": MULTPL + "/s-p-500-sales/table/by-quarter",
    "spy_ssga.html": "https://www.ssga.com/us/en/intermediary/etfs/state-street-spdr-sp-500-etf-trust-spy",
    "DGS10.csv": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
    "DFII10.csv": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10",
    "T10YIE.csv": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10YIE",
}
TABLES = {"cape": "cape_by_year.html", "pe": "pe_by_year.html", "ps": "ps_by_year.html",
          "ey": "ey_by_year.html", "dy": "dy_by_year.html", "t10": "tsy10_by_year.html"}

# Agent-of-record validation cases (published current values, not recomputed here).
VALIDATION = {
    "2026-08-14": {
        "current": {"cape": 42.20, "pe": 29.75, "ps": 3.81, "ey": 3.36, "ust10y": 4.68},
        "forecasts": [
            {"provider": "Research Affiliates", "basis": "nominal", "low": 3.2, "high": 3.2,
             "publication_date": "2026-05-21", "model_date": "2026-05-21"},
            {"provider": "Vanguard", "basis": "nominal", "low": 4.2, "high": 6.2,
             "publication_date": "2026-07-22", "model_date": "2026-06-30"},
            {"provider": "Schwab", "basis": "nominal", "low": 5.9, "high": 5.9,
             "publication_date": "2026-01-02", "model_date": "2025-10-31"},
        ],
        "published": {"cape": 99.4, "trailing_pe": 96.8, "price_to_sales": 100.0,
                      "erp_tightness": 89.3, "institutional": 49.8,
                      "unrounded": 88.97, "score": 89},
    },
    "2026-09-17": {
        # Published packet: CAPE 40.61, PE 25.86, P/S 3.74, EY 3.87%, UST10Y 4.94%,
        # ERP -1.07% -> ERP_Tightness_Risk 81.5, k 0.990751, SCORE 86. Only the ERP risk and
        # the score were published as numbers; the forecasts below are those publicly
        # available by 2026-09-17 (RA 07-31 data, VCMM 06-30 run, Schwab 2026 edition).
        "current": {"cape": 40.61, "pe": 25.86, "ps": 3.74, "ey": 3.87, "ust10y": 4.94},
        "forecasts": [
            {"provider": "Research Affiliates", "basis": "nominal", "low": 3.3, "high": 3.3,
             "publication_date": "2026-08-17", "model_date": "2026-07-31"},
            {"provider": "Vanguard", "basis": "nominal", "low": 4.2, "high": 6.2,
             "publication_date": "2026-07-22", "model_date": "2026-06-30"},
            {"provider": "Schwab", "basis": "nominal", "low": 5.9, "high": 5.9,
             "publication_date": "2026-01-02", "model_date": "2025-10-31"},
        ],
        "published": {"erp_tightness": 81.5, "score": 86},
    },
}
COMPONENT_TOL = 1.5


# --------------------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------------------
def _get(url):
    ua = UA_FRED if "fred.stlouisfed.org" in url else UA_BROWSER
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    try:
        return urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=90).read()
    except ssl.SSLError:
        ca = "/root/.ccr/ca-bundle.crt"
        return urllib.request.urlopen(
            req, context=ssl.create_default_context(cafile=ca), timeout=90).read()


def fetch(dirpath):
    os.makedirs(dirpath, exist_ok=True)
    ok = True
    for name, url in SOURCES.items():
        try:
            raw = _get(url)
            with open(os.path.join(dirpath, name), "wb") as fh:
                fh.write(raw)
            print(f"fetched {name}: {len(raw)} bytes  <- {url}")
        except Exception as exc:  # report and continue; compute() flags what is missing
            ok = False
            print(f"FAILED {name}: {exc}  <- {url}", file=sys.stderr)
    stamp = os.path.join(dirpath, "fetch_log.json")
    with open(stamp, "w") as fh:
        json.dump({"retrieved_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "sources": SOURCES}, fh, indent=1)
    return ok


# --------------------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------------------
def _read(dirpath, name):
    with open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _num(text):
    t = html.unescape(re.sub(r"<[^>]+>", "", text)).replace(" ", "").replace("†", "")
    t = t.replace("%", "").replace(",", "").strip()
    return float(t) if re.fullmatch(r"-?\d+(\.\d+)?", t) else None


def parse_multpl_table(text):
    """Rows of (date, value_or_None, estimated) in page order (newest first)."""
    t = text[text.find('id="datatable"'):]
    t = t[:t.find("</table>")]
    out = []
    for d, v in re.findall(r"<tr[^>]*>\s*<td>([^<]+)</td>\s*<td>(.*?)</td>", t, re.S):
        try:
            day = dt.datetime.strptime(d.strip(), "%b %d, %Y").date()
        except ValueError:
            continue  # malformed row -> excluded
        out.append((day, _num(v), "Estimate" in v))
    return out


def parse_multpl_current(text):
    """(value, stamp_text) from a Multpl page's #current block."""
    m = re.search(r'id="current"(.*?)Mean:', text, re.S)
    if not m:
        return None, None
    block = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))))
    v = re.search(r":\s*([\d,]+\.\d+)\s*%?", block)
    stamp = re.search(r"(\d{1,2}:\d{2} [AP]M E[DS]T, \w{3} \w{3} \d{1,2}|At market close \w{3} \w{3} \d{1,2}, \d{4})", block)
    return (float(v.group(1).replace(",", "")) if v else None,
            stamp.group(1) if stamp else None)


def parse_sales_denominator(text):
    """Month-end date of the 12-month sales Multpl uses for the current P/S."""
    m = re.search(r"12 month sales ending\s+([A-Za-z]+),?\s+(\d{4})", html.unescape(text))
    if not m:
        return None
    d = dt.datetime.strptime(f"{m.group(1)} 1 {m.group(2)}", "%B %d %Y").date()
    return _month_end(d)


def parse_earnings_denominator(text):
    m = re.search(r"12\s+month\s+(?:earnings|dividends)\s+through\s+([A-Za-z]+),?\s+(\d{4})",
                  html.unescape(re.sub(r"<[^>]+>", " ", text)))
    if not m:
        return None
    return _month_end(dt.datetime.strptime(f"{m.group(1)} 1 {m.group(2)}", "%B %d %Y").date())


def parse_fred(dirpath, sid):
    path = os.path.join(dirpath, sid + ".csv")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="") as fh:
        rd = csv.reader(fh)
        next(rd, None)
        for r in rd:
            if len(r) >= 2 and r[1].strip() not in ("", "."):
                out[dt.date.fromisoformat(r[0])] = float(r[1])
    return out


def parse_ssga(text):
    """SPY Index Characteristics: FY1 P/E, trailing P/E and their as-of date."""
    i = text.find("Index Characteristics")
    if i < 0:
        return {}
    seg = text[i:i + 4000]
    asof = re.search(r'class="date">as of (\w{3} \d{1,2} \d{4})<', seg)
    def kv(label):
        m = re.search(r">\s*" + re.escape(label) + r"\s*</th>\s*<td[^>]*>([^<]+)<", seg)
        return _num(m.group(1)) if m else None
    return {"fy1_pe": kv("Price/Earnings Ratio FY1"), "pe": kv("Price/Earnings"),
            "asof": dt.datetime.strptime(asof.group(1), "%b %d %Y").date() if asof else None}


def _month_end(d):
    nxt = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def _parse_date(s):
    if s is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return _month_end(dt.date.fromisoformat(s + "-01"))
    return dt.date.fromisoformat(s)


def months_between(a, b):
    return (b.year - a.year) * 12 + (b.month - a.month) + (b.day - a.day) / 31.0


# --------------------------------------------------------------------------------------
# Methodology
# --------------------------------------------------------------------------------------
def annual_history(rows, current_year, keep_current_year_jan1=False):
    """One observation per completed calendar year, by row date, through current_year-1.

    Removes the dated current row and any other current-year row (Multpl's "Jan 1, <current
    year>" snapshot), blanks and malformed rows. Never forward-fills."""
    seen, out = set(), []
    for i, (d, v, _est) in enumerate(rows):
        if keep_current_year_jan1 and i > 0 and d.year == current_year and (d.month, d.day) == (1, 1):
            out.append((d, v))  # rejected convention B, diagnostics only
            continue
        if v is None or d.year >= current_year or d.year in seen:
            continue
        seen.add(d.year)
        out.append((d, v))
    return sorted(out)


def inclusive_percentile(history_values, current):
    obs = list(history_values) + [current]
    cnt = sum(1 for x in obs if x <= current)
    return 100.0 * cnt / len(obs), cnt, len(obs)


def forecast_to_risk(r):
    pts = FORECAST_ANCHORS
    if r <= pts[0][0]:
        return pts[0][1]
    if r >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= r <= x1:
            return y0 + (y1 - y0) * (r - x0) / (x1 - x0)
    raise ValueError(r)


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return dt.date(y, m, min(d.day, _month_end(dt.date(y, m, 1)).day))


def real_to_nominal(real_pct, infl_pct):
    """Fisher compounding: (1+r)(1+i)-1, in percent."""
    return ((1.0 + real_pct / 100.0) * (1.0 + infl_pct / 100.0) - 1.0) * 100.0


def institutional_anchor(forecasts, asof):
    """Equal-weight provider risks with the range, real-return, staleness and min-count rules.

    Staleness (incomplete_row_rule.stale_forecast): flag > 12 months; drop > 18 months only when
    a newer edition should already exist, i.e. publication + the provider's cadence
    (`cadence_months`, default 12 = annual) is on or before ASOF. A provider whose cadence is
    demonstrably slower is kept and flagged.
    Real returns (real_return_rule): converted to nominal only when the same provider supplies a
    compatible inflation assumption (`inflation_assumption`: {value, provider, horizon_years});
    otherwise excluded from the scored basket and shown as context."""
    used, excluded, flags = [], [], []
    by_provider = {}
    for f in forecasts:
        pub, model = _parse_date(f.get("publication_date")), _parse_date(f.get("model_date"))
        if pub and pub > asof:
            excluded.append((f["provider"], "published after ASOF"))
            continue
        low, high, conv = f["low"], f["high"], None
        if f.get("basis", "nominal") != "nominal":
            ia = f.get("inflation_assumption") or {}
            same = ia.get("provider") == f["provider"]  # must be stated, never assumed
            hz_ok = ia.get("horizon_years") is None or abs(ia["horizon_years"] - 10) <= 2
            if f.get("basis") == "real" and ia.get("value") is not None and same and hz_ok:
                low, high = real_to_nominal(low, ia["value"]), real_to_nominal(high, ia["value"])
                conv = (f"real {f['low']:.1f}%-{f['high']:.1f}% + {f['provider']} inflation "
                        f"{ia['value']:.1f}% -> nominal {low:.2f}%-{high:.2f}%")
                flags.append(f"{f['provider']} {conv}")
            else:
                excluded.append((f["provider"], "real-return forecast without a compatible same-provider "
                                 "inflation assumption; context only"))
                continue
        age = months_between(model or pub, asof)
        cadence = f.get("cadence_months", 12)
        next_due = add_months(pub or model, cadence)
        if age > 18:
            if next_due <= asof:
                excluded.append((f["provider"], f"model date {model} older than 18 months and a newer "
                                 f"edition was due by {next_due} ({cadence}-month cadence)"))
                continue
            flags.append(f"{f['provider']} forecast older than 18 months but kept: {cadence}-month "
                         f"cadence, next edition not due until {next_due}")
        elif age > 12:
            flags.append(f"{f['provider']} forecast older than 12 months (model {model})")
        prev = by_provider.get(f["provider"])
        if prev is None or (model or pub) > prev[1]:
            by_provider[f["provider"]] = (dict(f, low=low, high=high, conversion=conv, age_months=age,
                                               next_edition_due=next_due), model or pub)
    for p, (f, _key) in by_provider.items():
        mid = (f["low"] + f["high"]) / 2.0
        used.append({"provider": p, "low": f["low"], "high": f["high"], "midpoint": mid,
                     "risk": forecast_to_risk(mid), "publication_date": f.get("publication_date"),
                     "model_date": f.get("model_date"), "asset": f.get("asset"),
                     "age_months": f["age_months"], "next_edition_due": f["next_edition_due"],
                     "conversion": f["conversion"], "corroborated": f.get("corroborated"),
                     "fallback": f.get("fallback"), "source_quality": f.get("source_quality"),
                     "source_url": f.get("source_url"),
                     "raw": {k: v for k, v in f.items() if k in ("publication_date_kind", "source_count",
                                                                 "source_kind", "cadence_months")}})
    if len(used) >= 2:
        risk = sum(u["risk"] for u in used) / len(used)
        mid = sum(u["midpoint"] for u in used) / len(used)
    else:
        flags.append(f"only {len(used)} usable provider(s): neutral risk 50 retained at 15%")
        risk, mid = NEUTRAL_RISK, (used[0]["midpoint"] if used else None)
    return {"risk": risk, "blended_midpoint": mid, "providers": used,
            "excluded": excluded, "flags": flags}


def risk_to_forecast(risk):
    """Inverse of forecast_to_risk on the open interval (10, 95); None outside it."""
    pts = FORECAST_ANCHORS
    if not (pts[-1][1] < risk < pts[0][1]):
        return None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if y1 <= risk <= y0:
            return x0 + (x1 - x0) * (risk - y0) / (y1 - y0)
    return None


def round_half_up(u):
    return int(u + 0.5)


def business_days_between(a, b):
    """Weekdays in (a, b]; holidays ignored."""
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        n += d.weekday() < 5
    return n


def t10_crosscheck(t10_rows, dgs10):
    """Multpl 'Jan 1, Y' 10Y rows vs the January-Y mean of FRED DGS10, every year both exist."""
    years, mism = [], []
    for d, v, _e in t10_rows:
        if (d.month, d.day) != (1, 1) or v is None:
            continue
        jan = [x for k, x in dgs10.items() if k.year == d.year and k.month == 1]
        if not jan:
            continue
        years.append(d.year)
        if abs(round(sum(jan) / len(jan), 2) - v) > 0.011:
            mism.append((d.year, round(sum(jan) / len(jan), 4), v))
    return sorted(years), mism


def reconcile(official, embedded):
    k = official / embedded
    disc = embedded / official - 1.0
    if abs(disc) <= TOL_NO_ADJUST:
        cls = "no_adjustment (<=0.10%, rounding)"
    elif abs(disc) <= TOL_REJECT:
        cls = "adjust_and_disclose (>0.10% and <=1.0%, explicit stamps)"
    else:
        cls = "reject_or_find_alternate (>1.0%)"
    return k, disc, cls


def erp_history(ey_rows, dgs10, current_year, t10_rows=None, conv_b=False):
    """[(year, tightness)] for 2000..current_year-1 plus the alignment cross-check."""
    ey = {d.year: v for d, v in annual_history(ey_rows, current_year, conv_b) if d.month == 1}
    mult = {d.year: v for d, v in annual_history(t10_rows or [], current_year, conv_b)}
    out, mism = [], []
    for y in range(ERP_START_YEAR, current_year + (1 if conv_b else 0)):
        jan = [v for d, v in dgs10.items() if d.year == y and d.month == 1]
        if not jan or y not in ey:
            continue  # incomplete row -> excluded, never forward-filled
        t10 = sum(jan) / len(jan)
        if y in mult and abs(round(t10, 2) - mult[y]) > 0.011:
            mism.append((y, round(t10, 4), mult[y]))
        out.append((y, t10 - ey[y], t10, ey[y]))
    return out, mism


def score_components(hist, cur, forecasts, asof):
    """Component risks from annual histories + one current observation per metric."""
    comp = {}
    for key, hk in (("cape", "cape"), ("trailing_pe", "pe"), ("price_to_sales", "ps")):
        pct, cnt, n = inclusive_percentile([v for _, v in hist[hk]], cur[hk])
        comp[key] = {"value": cur[hk], "percentile": pct, "risk": pct, "count": cnt, "N": n,
                     "first_year": hist[hk][0][0].year, "last_hist_year": hist[hk][-1][0].year}
    tight = cur["ust10y"] - cur["ey"]
    pct, cnt, n = inclusive_percentile([t for _, t, _, _ in hist["erp"]], tight)
    comp["erp_tightness"] = {"value": tight, "percentile": pct, "risk": pct, "count": cnt, "N": n,
                             "first_year": hist["erp"][0][0], "last_hist_year": hist["erp"][-1][0]}
    inst = institutional_anchor(forecasts, asof)
    comp["institutional"] = {"risk": inst["risk"], "detail": inst}
    unrounded = sum(WEIGHTS[k] * comp[k]["risk"] for k in WEIGHTS)
    return comp, unrounded, int(unrounded + 0.5)  # round half up, after the full weighted sum


def load_histories(cache, current_year, conv_b=False):
    rows = {k: parse_multpl_table(_read(cache, f)) for k, f in TABLES.items()}
    dgs10 = parse_fred(cache, "DGS10")
    hist = {k: annual_history(rows[k], current_year, conv_b) for k in ("cape", "pe", "ps", "ey", "dy")}
    hist["erp"], mism = erp_history(rows["ey"], dgs10, current_year, rows["t10"], conv_b)
    return rows, hist, dgs10, mism


def _floor(x, places):
    m = 10 ** places
    return math.floor(x * m + 1e-9) / m


def _ceil(x, places):
    m = 10 ** places
    return math.ceil(x * m - 1e-9) / m


def rounding_edge(hist, cur, forecasts, asof, comp, unrounded, score, spx):
    """Two-sided rounding-edge sensitivity (disclosed only; never substituted for the score).

    Distance from the unrounded score to the boundary above (score+0.5, reached -> score+1) and
    below (score-0.5, crossed -> score-1), then single-input changes, each applied alone:
    forecast fallbacks and range ends, forecast-midpoint flip thresholds, and the nearest
    percentile step in each scored ratio, expressed as an SPX level or 10Y yield."""
    up = (score + 0.5) - unrounded
    down = unrounded - (score - 0.5)
    out = {"unrounded": unrounded, "score": score,
           "up_boundary": score + 0.5, "distance_up": up,
           "down_boundary": score - 0.5, "distance_down": down,
           "nearest": "up" if up <= down else "down",
           "scenarios": [], "forecast_thresholds": [], "percentile_steps": []}

    def run(label, kind, cur2=None, fc2=None):
        _c, u2, s2 = score_components(hist, cur2 or cur, fc2 if fc2 is not None else forecasts, asof)
        out["scenarios"].append({"change": label, "kind": kind, "unrounded": u2, "score": s2,
                                 "flips": s2 != score})

    # Forecast fallbacks (and one 0.1pp step beyond, away from the scored value) and range ends.
    for i, f in enumerate(forecasts):
        fb = f.get("fallback")
        if fb and fb.get("value") is not None and fb["value"] != (f["low"] + f["high"]) / 2.0:
            step = 0.1 if fb["value"] > f["low"] else -0.1
            for v, tag in ((fb["value"], f"fallback, pub {fb.get('publication_date')}"),
                           (round(fb["value"] + step, 4), "0.1pp beyond the fallback")):
                fc2 = [dict(g, low=v, high=v) if j == i else g for j, g in enumerate(forecasts)]
                run(f"{f['provider']} {v:.1f}% ({tag})", "forecast", fc2=fc2)
        if f["high"] != f["low"]:
            for v, tag in ((f["low"], "range low"), (f["high"], "range high")):
                fc2 = [dict(g, low=v, high=v) if j == i else g for j, g in enumerate(forecasts)]
                run(f"{f['provider']} {v:.1f}% ({tag} instead of midpoint)", "forecast", fc2=fc2)

    # Forecast-midpoint thresholds: exact, since the anchor is an equal-weight provider mean.
    inst = comp["institutional"]["detail"]
    n, w = len(inst["providers"]), WEIGHTS["institutional"]
    if n >= 2:
        for p in inst["providers"]:
            m_up = risk_to_forecast(p["risk"] + n * up / w)
            m_dn = risk_to_forecast(p["risk"] - n * down / w)
            out["forecast_thresholds"].append({"provider": p["provider"], "midpoint": p["midpoint"],
                                               "flip_up_if_at_or_below": m_up,
                                               "flip_down_if_above": m_dn})

    # Nearest percentile step for each scored ratio (ties included).
    for key, hk, label in (("cape", "cape", "CAPE10"), ("trailing_pe", "pe", "SP500_PE_TTM"),
                           ("price_to_sales", "ps", "SP500_PSales")):
        vals, c = [v for _, v in hist[hk]], cur[hk]
        higher, lower = [v for v in vals if v > c], [v for v in vals if v <= c]
        for direction, thr, probe in (("up", min(higher) if higher else None, lambda t: t),
                                      ("down", max(lower) if lower else None, lambda t: t - 1e-9)):
            if thr is None:
                out["percentile_steps"].append({"metric": label, "direction": direction, "threshold": None})
                continue
            _c2, u2, s2 = score_components(hist, dict(cur, **{hk: probe(thr)}), forecasts, asof)
            out["percentile_steps"].append({
                "metric": label, "direction": direction, "threshold": thr, "current": c,
                "spx_equiv": spx * thr / c, "spx_move_pct": (thr / c - 1.0) * 100.0,
                "unrounded": u2, "score": s2, "flips": s2 != score})
    vals = [t for _, t, _, _ in hist["erp"]]
    c = cur["ust10y"] - cur["ey"]
    higher, lower = [v for v in vals if v > c], [v for v in vals if v <= c]
    for direction, thr, eps in (("up", min(higher) if higher else None, 0.0),
                                ("down", max(lower) if lower else None, -1e-9)):
        if thr is None:
            out["percentile_steps"].append({"metric": "ERP_Tightness", "direction": direction, "threshold": None})
            continue
        _c2, u2, s2 = score_components(hist, dict(cur, ust10y=cur["ey"] + thr + eps), forecasts, asof)
        ey2 = cur["ust10y"] - thr
        out["percentile_steps"].append({
            "metric": "ERP_Tightness", "direction": direction, "threshold": thr, "current": c,
            "ust10y_equiv": cur["ey"] + thr, "ust10y_move_bp": (thr - c) * 100.0,
            "spx_equiv": spx * cur["ey"] / ey2 if ey2 > 0 else None,
            "spx_move_pct": (cur["ey"] / ey2 - 1.0) * 100.0 if ey2 > 0 else None,
            "unrounded": u2, "score": s2, "flips": s2 != score})
    return out


# --------------------------------------------------------------------------------------
# Live run
# --------------------------------------------------------------------------------------
def compute(cache, facts, asof=None, validation=None):
    rows = {k: parse_multpl_table(_read(cache, f)) for k, f in TABLES.items()}
    stamp_date = rows["cape"][0][0]
    for k in ("pe", "ps", "ey", "dy"):
        if rows[k][0][0] != stamp_date:
            raise SystemExit(f"Multpl current-row dates disagree: {k} {rows[k][0][0]} vs CAPE {stamp_date}")
    asof = dt.date.fromisoformat(asof) if asof else stamp_date
    if asof != stamp_date:
        raise SystemExit(f"Multpl stamp {stamp_date} != requested ASOF {asof}; incompatible trading dates")
    rows, hist, dgs10, mism = load_histories(cache, asof.year)
    if mism:
        raise SystemExit(f"ERP alignment cross-check failed (FRED Jan avg vs Multpl Jan 1): {mism}")
    xc_years, xc_mism = t10_crosscheck(rows["t10"], dgs10)  # full overlap, not just the ERP window

    embedded, stamp = parse_multpl_current(_read(cache, "spx_price.html"))
    if stamp is None or not stamp.endswith(f"{asof:%b} {asof.day}") or f"{asof:%a}" not in stamp:
        raise SystemExit(f"Multpl embedded-price stamp {stamp!r} does not match {asof}")
    off = facts["official_spx_close"]
    if dt.date.fromisoformat(off["date"]) != asof:
        raise SystemExit("official SPX close date does not match ASOF")
    k, disc, cls = reconcile(off["value"], embedded)
    if cls.startswith("reject"):
        raise SystemExit(f"price-stamp discrepancy {disc:.4%} > 1.0%: reject or find alternate")
    kk = 1.0 if cls.startswith("no_adjust") else k

    src = {key: rows[key][0][1] for key in ("cape", "pe", "ps", "ey", "dy")}
    est = {key: rows[key][0][2] for key in ("cape", "pe", "ps", "ey", "dy")}
    adj = {"cape": src["cape"] * kk, "pe": src["pe"] * kk, "ps": src["ps"] * kk,
           "ey": src["ey"] / kk, "dy": src["dy"] / kk}

    flags = []
    # Rates: same-day official preferred; else prior official + same-day cross-check, disclosed.
    def last_on_or_before(series, d):
        ds = [x for x in series if x <= d]
        return (max(ds), series[max(ds)]) if ds else (None, None)
    t10_date, t10 = last_on_or_before(dgs10, asof)
    real_date, real = last_on_or_before(parse_fred(cache, "DFII10"), asof)
    bei_series = parse_fred(cache, "T10YIE")
    bei_date, bei = last_on_or_before(bei_series, t10_date)
    bei_same_date, bei_same = last_on_or_before(bei_series, asof)
    xc = facts.get("ust10y_same_day_cross_check")
    if t10_date != asof:
        flags.append(f"UST10Y official DGS10 is {t10_date} ({t10:.2f}%); same-day cross-check "
                     f"{xc['value']:.2f}% ({xc['date']}, secondary) shown, not scored" if xc else
                     f"UST10Y official DGS10 is {t10_date}; no same-day cross-check")
    cur = {"cape": adj["cape"], "pe": adj["pe"], "ps": adj["ps"], "ey": adj["ey"], "ust10y": t10}

    comp, unrounded, score = score_components(hist, cur, facts["forecasts"], asof)
    inst = comp["institutional"]["detail"]
    flags += inst["flags"]

    # Denominators
    sales_end = parse_sales_denominator(_read(cache, "ps_page.html"))
    earn_end = parse_earnings_denominator(_read(cache, "ey_page.html"))
    div_end = parse_earnings_denominator(_read(cache, "dy_page.html"))
    sales_age = months_between(sales_end, asof) if sales_end else None
    sps_end = None
    if os.path.exists(os.path.join(cache, "sps_by_quarter.html")):
        sps_rows = [r_ for r_ in parse_multpl_table(_read(cache, "sps_by_quarter.html")) if r_[1] is not None]
        sps_end = sps_rows[0][0] if sps_rows else None
    searches = facts.get("searches", {})
    ps_search = searches.get("ps_sales_alternate")
    stale = []
    if sales_age is not None and sales_age > 6:
        stale.append(f"P/S sales denominator {sales_end} is {sales_age:.1f} months old (>2 quarters)")
    material = sales_age is not None and sales_age > 12
    if material:
        s = "P/S sales denominator >12 months: alternate consistent source required by spec"
        if sps_end:
            s += f"; Multpl sales/share by-quarter table latest row {sps_end}"
        if ps_search:
            s += (f"; search {ps_search.get('date')} ({', '.join(ps_search.get('hosts', []))}): "
                  f"{ps_search.get('result')}")
        stale.append(s + "; component still scored on the latest Multpl-reported sales and disclosed")
    ssga = parse_ssga(_read(cache, "spy_ssga.html"))

    # Sensitivity (disclosed, never substituted)
    sens = {}
    if xc:
        c2, u2, s2 = score_components(hist, dict(cur, ust10y=xc["value"]), facts["forecasts"], asof)
        sens["ust10y_same_day_cross_check"] = {"erp_tightness_risk": c2["erp_tightness"]["risk"],
                                               "unrounded": u2, "score": s2}
    _rb, hb, _gb, _mb = load_histories(cache, asof.year, conv_b=True)
    cb, ub, sb = score_components(hb, cur, facts["forecasts"], asof)
    sens["rejected_convention_B"] = {"components": {k2: cb[k2]["risk"] for k2 in WEIGHTS},
                                     "unrounded": ub, "score": sb}
    u3 = unrounded + WEIGHTS["price_to_sales"] * (NEUTRAL_RISK - comp["price_to_sales"]["risk"])
    sens["price_to_sales_neutral_50"] = {"unrounded": u3, "score": round_half_up(u3)}
    max_hist_ps = max(v for _, v in hist["ps"])
    sens["price_to_sales_breakeven_sales_growth"] = cur["ps"] / max_hist_ps - 1.0
    edge = rounding_edge(hist, cur, facts["forecasts"], asof, comp, unrounded, score, off["value"])
    for label, kind, s_ in (
            ("UST10Y same-day cross-check instead of prior-day DGS10", "rate",
             sens.get("ust10y_same_day_cross_check")),
            ("P/S neutral-50 substitution (spec option if denominator stays >12 months)", "methodology",
             sens["price_to_sales_neutral_50"]),
            ("rejected convention B (keep current-year Jan 1 rows)", "methodology",
             sens["rejected_convention_B"])):
        if s_:
            edge["scenarios"].append({"change": label, "kind": kind, "unrounded": s_["unrounded"],
                                      "score": s_["score"], "flips": s_["score"] != score})

    # Facts drawn from the run's own data, so the packet carries no run-specific literals.
    est_hist = {d: e for d, _v, e in rows["ps"]}
    ps_max_d, ps_max_v = max(hist["ps"], key=lambda dv: dv[1])
    observed = {
        "cape_higher_obs": [(d, v) for d, v in hist["cape"] if v > cur["cape"]],
        "pe_higher_obs": [(d, v) for d, v in hist["pe"] if v > cur["pe"]],
        "ps_hist_max": (ps_max_d, ps_max_v, est_hist.get(ps_max_d, False)),
        "ps_first_row": hist["ps"][0][0], "ps_last_row": hist["ps"][-1][0],
        "t10_crosscheck_years": (xc_years[0], xc_years[-1], len(xc_years)) if xc_years else None,
        "t10_crosscheck_mismatches": xc_mism,
        "rate_lag_business_days": business_days_between(t10_date, asof) if t10_date else None,
        "estimated_current_rows": [k2 for k2, e in est.items() if e],
        "current_year_jan1_rows_removed": sorted(
            k2 for k2 in ("cape", "pe", "ey", "dy", "t10")
            if any(d.year == asof.year and (d.month, d.day) == (1, 1) for d, _v, _e in rows[k2][1:])),
        "sps_table_end": sps_end,
    }
    official_is_spdji = bool(off.get("official_sp_dji"))

    # Confidence: spec bands (supervisor_integration.confidence_guidance). Band placement is a
    # documented heuristic, not a spec formula: material staleness caps at the 70-79 band top
    # (79), otherwise 80-89 band top (89); minus 1 per further disclosed minor issue.
    # Every condition is evaluated from the run's data or the facts file.
    minor = []
    if not official_is_spdji:
        minor.append(f"SPX close substituted ({off.get('source_label', 'not S&P DJI')})")
    minor.append("Multpl used as Shiller-derived history proxy")  # TABLES are Multpl pages
    if inst["providers"] and all((p.get("source_quality") or "").startswith("secondary")
                                 for p in inst["providers"]):
        minor.append("institutional forecasts secondary-sourced")
    if t10_date != asof:
        minor.append(f"UST10Y official {observed['rate_lag_business_days']} business day(s) behind ASOF")
    for p in inst["providers"]:
        if p.get("corroborated") is False:
            minor.append(f"{p['provider']} latest figure single-sourced, uncorroborated")
    for p in inst["providers"]:
        if p["age_months"] > 11.5:
            minor.append(f"{p['provider']} model date {p['model_date']} is {p['age_months']:.1f} months old")
    confidence = (79 if material else 89) - len(minor)

    key_values = {  # mirrors latest_state.component_risks field names
        "cape_percentile_risk": comp["cape"]["risk"],
        "trailing_pe_percentile_risk": comp["trailing_pe"]["risk"],
        "price_to_sales_percentile_risk": comp["price_to_sales"]["risk"],
        "erp_tightness_percentile_risk": comp["erp_tightness"]["risk"],
        "institutional_10y_forecast_risk": comp["institutional"]["risk"],
        "weighted_unrounded_score": unrounded,
        "score": score, "confidence": confidence,
    }
    check = sum(WEIGHTS[k2] * key_values[f] for k2, f in (
        ("cape", "cape_percentile_risk"), ("trailing_pe", "trailing_pe_percentile_risk"),
        ("price_to_sales", "price_to_sales_percentile_risk"),
        ("erp_tightness", "erp_tightness_percentile_risk"),
        ("institutional", "institutional_10y_forecast_risk")))
    if abs(check - key_values["weighted_unrounded_score"]) > 1e-9 or round_half_up(check) != score:
        raise SystemExit(f"key_values weighted_unrounded_score {unrounded} != component sum {check}")

    # Prior-run comparison: prior official score from facts; component attribution recomputed
    # from the prior run's published inputs (VALIDATION) through today's annual history.
    prior = facts.get("prior_run")
    if prior and validation and prior.get("asof") in validation and prior["asof"] in VALIDATION:
        pv = validation[prior["asof"]]
        prior = dict(prior, inputs=VALIDATION[prior["asof"]]["current"],
                     recomputed_components=pv["components"], recomputed_unrounded=pv["unrounded"],
                     weighted_deltas={k2: WEIGHTS[k2] * (comp[k2]["risk"] - pv["components"][k2])
                                      for k2 in WEIGHTS})

    return {
        "asof": asof, "score": score, "unrounded": unrounded, "confidence": confidence,
        "components": comp, "weights": WEIGHTS,
        "source": src, "estimated": est, "adjusted": adj,
        "reconciliation": {"official": off["value"], "official_source": off.get("source"), "official_meta": off,
                           "embedded": embedded, "stamp": stamp, "k": k, "applied_k": kk,
                           "discrepancy": disc, "classification": cls},
        "rates": {"ust10y": t10, "ust10y_date": t10_date, "real": real, "real_date": real_date,
                  "breakeven": bei, "breakeven_date": bei_date,
                  "breakeven_same_day": bei_same, "breakeven_same_day_date": bei_same_date,
                  "cross_check": xc},
        "nominal_erp": adj["ey"] - t10, "real_yield_spread": adj["ey"] - real,
        "denominators": {"sales_end": sales_end, "sales_age_months": sales_age,
                         "earnings_end": earn_end, "dividends_end": div_end},
        "ssga": ssga, "context": facts.get("context", {}),
        "flags": flags, "stale": stale, "material_stale": material, "minor_issues": minor,
        "sensitivity": sens, "rounding_edge": edge, "erp_history": hist["erp"],
        "history_first_years": {k: hist[k][0][0].year for k in ("cape", "pe", "ps")},
        "observed": observed, "key_values": key_values, "prior_run": prior,
        "governance": facts.get("governance"), "validation": validation or {},
        "facts_meta": {"retrieved": facts.get("retrieved"), "searches": searches},
    }


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------
def _d(x):
    return x.isoformat() if isinstance(x, dt.date) else str(x)


def _host(url):
    if not url:
        return "source not supplied"
    h = urllib.parse.urlparse(url).netloc
    return h[4:] if h.startswith("www.") else h


def _regime(s):
    return ("low-risk (0-35)" if s < 35 else "neutral (35-60)" if s < 60
            else "high-risk (60-80)" if s < 80 else "extreme (80-100)")


def _change_class(d):
    d = abs(d)
    return ("effectively unchanged" if d <= 1 else "small move" if d <= 3
            else "moderate move" if d <= 5 else "large move")


def _na(date, reason):
    return f"N/A ({date}, not available: {reason or 'not supplied'})"


def _ctx_metric(name, item, a):
    if item and item.get("value") is not None:
        src = item.get("source_url")
        return f"- {name}={item['value']} ({item.get('asof', a)}" + (f"; {_host(src)}" if src else "") + ")"
    return f"- {name}=" + _na(a, (item or {}).get("reason"))


def _scen(s):
    return f"{s['change']} -> {s['unrounded']:.4f} -> {s['score']}"


def packet(r):
    a = r["asof"].isoformat()
    c, rc, rt = r["components"], r["reconciliation"], r["rates"]
    inst = c["institutional"]["detail"]
    ssga, ctx, ob, den = r["ssga"], r["context"], r["observed"], r["denominators"]
    sens, edge, xc = r["sensitivity"], r["rounding_edge"], rt["cross_check"]
    fy = r["history_first_years"]
    score = r["score"]

    # --- drivers (all from the run's history tables) ---
    hi = ob["cape_higher_obs"]
    cape_ctx = ("highest annual obs on record" if not hi else
                "higher only in " + ", ".join(f"{d:%b %Y} ({v:.2f})" for d, v in hi) if len(hi) <= 3
                else f"{len(hi)} annual obs higher")
    psd, psv, pse = ob["ps_hist_max"]
    ps_ctx = (f"above every annual obs since {fy['ps']} (prior max {psv:.2f}, {psd:%b %Y}"
              + (", Multpl estimate" if pse else "") + ")") if c["price_to_sales"]["value"] > psv else \
             (f"annual max since {fy['ps']} is {psv:.2f} ({psd:%b %Y})")

    def prov_txt(p):
        raw = p.get("raw") or {}
        pub = p["publication_date"]
        if raw.get("publication_date_kind"):
            pub = f"{raw['publication_date_kind']} {pub}"
        return (f"{p['provider']} {p['low']:.1f}%"
                + (f"-{p['high']:.1f}% (mid {p['midpoint']:.1f}%)" if p["high"] != p["low"] else "")
                + f" -> risk {p['risk']:.2f} [pub {pub}, model {p['model_date']}]")
    prov = "; ".join(prov_txt(p) for p in inst["providers"])

    fy1 = ssga.get("fy1_pe")
    spy_line = (f"- SPY_FY1_PE={fy1:.2f} ({_d(ssga.get('asof'))}, State Street, not rescaled)"
                + (f"; SPY index trailing P/E {ssga['pe']:.2f} ({_d(ssga.get('asof'))})" if ssga.get("pe") else "")
                if fy1 is not None else "- SPY_FY1_PE=" + _na(a, "State Street characteristics not parsed from cache"))
    mc = ctx.get("market_cap_to_gdp") or {}
    if mc.get("value") is not None:
        mc_line = (f"- MarketCap_GDP={mc['value']} ({mc.get('asof', a)}; {_host(mc.get('source_url'))}; context only)")
        pc = mc.get("prior_carried")
        if pc:
            mc_line += (f"; prior carried {pc['value']} ({pc['asof']}, {pc.get('source', 'source not supplied')})"
                        + (" is a different methodology, not comparable" if pc.get("comparable") is False else ""))
    else:
        mc_line = "- MarketCap_GDP=" + _na(a, mc.get("reason"))

    lines = [
        "MODULE: Valuation and Expected Returns",
        f"ASOF: {a} close",
        f"SCORE_0_100: {score}",
        f"CONFIDENCE_0_100: {r['confidence']}",
        "TOP_3_DRIVERS:",
        f"- CAPE10={c['cape']['value']:.2f} ({a} close, k-adjusted): {c['cape']['percentile']:.1f}th percentile of {c['cape']['N']} annual obs since {fy['cape']}; {cape_ctx}",
        f"- SP500_PSales={c['price_to_sales']['value']:.2f} ({a} close; sales to {_d(den['sales_end'])}): {ps_ctx}, percentile {c['price_to_sales']['percentile']:.1f}",
        f"- EarningsYield={r['adjusted']['ey']:.2f}% ({a} close) vs UST10Y={rt['ust10y']:.2f}% ({_d(rt['ust10y_date'])}): ERP_Nominal={r['nominal_erp']:+.2f}pp, tightness {c['erp_tightness']['value']:+.2f}pp = {c['erp_tightness']['percentile']:.1f}th pct of {c['erp_tightness']['N']} obs since {c['erp_tightness']['first_year']}",
        "CHANGE_MY_MIND (TRIGGERS):",
        "- CAPE10 at or below 34 (lower risk); at or above 45 (higher risk)",
        "- SP500_PSales at or below 3.0 together with SPY FY1 P/E at or below 19 (lower risk); P/S at or above 4.0 (higher risk)",
        "- ERP_Nominal at or above +0.5pp (+1.0pp strong repair) or blended 10Y forecast above ~6.5% nominal (lower risk); ERP_Nominal at or below -1.5pp or blended forecast at or below ~3.5% (higher risk)",
        "KEY_METRICS:",
        f"- CAPE10={c['cape']['value']:.2f} ({a} close; Multpl {r['source']['cape']:.2f} x k; pct {c['cape']['percentile']:.1f}, N={c['cape']['N']}, {fy['cape']}-{c['cape']['last_hist_year']}+current)",
        f"- SP500_PE_TTM={c['trailing_pe']['value']:.2f} ({a} close; Multpl{' est.' if r['estimated']['pe'] else ''} {r['source']['pe']:.2f} x k; earnings through {_d(den['earnings_end'])}; pct {c['trailing_pe']['percentile']:.1f}, N={c['trailing_pe']['N']}, {fy['pe']}-{c['trailing_pe']['last_hist_year']}+current)",
        f"- SP500_PSales={c['price_to_sales']['value']:.2f} ({a} close; denominator 12m sales ending {_d(den['sales_end'])}, {den['sales_age_months']:.1f} months lagged; pct {c['price_to_sales']['percentile']:.1f}, N={c['price_to_sales']['N']}, {fy['ps']}-{c['price_to_sales']['last_hist_year']}+current)",
        f"- EarningsYield={r['adjusted']['ey']:.2f}% ({a} close; Multpl{' est.' if r['estimated']['ey'] else ''} {r['source']['ey']:.2f}% / k)",
        f"- DividendYield={r['adjusted']['dy']:.2f}% ({a} close; Multpl{' est.' if r['estimated']['dy'] else ''} {r['source']['dy']:.2f}% / k; dividends through {_d(den['dividends_end'])})",
        f"- UST10Y_Nominal={rt['ust10y']:.2f}% ({_d(rt['ust10y_date'])}, FRED DGS10)" + (f"; same-day cross-check {xc['value']:.2f}% ({xc['date']}, {_host(xc.get('source_url'))}, not scored)" if xc and rt['ust10y_date'] != r['asof'] else ""),
        f"- UST10Y_Real={rt['real']:.2f}% ({_d(rt['real_date'])}, FRED DFII10)",
        f"- Breakeven10Y={rt['breakeven']:.2f}% ({_d(rt['breakeven_date'])}, FRED T10YIE); {rt['breakeven_same_day']:.2f}% ({_d(rt['breakeven_same_day_date'])})",
        f"- ERP_Nominal={r['nominal_erp']:+.2f}pp ({a} close EY vs {_d(rt['ust10y_date'])} 10Y); ERP_Tightness={c['erp_tightness']['value']:+.2f}pp, pct {c['erp_tightness']['percentile']:.1f}, N={c['erp_tightness']['N']}",
        f"- RealYieldSpread={r['real_yield_spread']:+.2f}pp ({a} close EY vs {_d(rt['real_date'])} DFII10; context only)",
        spy_line,
        _ctx_metric("QQQ_Forward_PE", ctx.get("qqq_forward_pe"), a),
        _ctx_metric("FactSet_Forward_12M_PE", ctx.get("factset_forward_12m_pe"), a),
        mc_line,
        f"- Institutional_10Y: {prov}; blended midpoint {inst['blended_midpoint']:.2f}%, risk {inst['risk']:.2f}",
        f"- Price_Reconciliation: official SPX {rc['official']:.2f} ({a}) / Multpl embedded {rc['embedded']:.2f} ({rc['stamp']}) -> k={rc['k']:.6f}, discrepancy {rc['discrepancy']*100:+.2f}% ({rc['classification'].split(' ')[0]})",
        f"- Components: CAPE {c['cape']['risk']:.2f}x{WEIGHTS['cape']:.2f} + PE {c['trailing_pe']['risk']:.2f}x{WEIGHTS['trailing_pe']:.2f} + P/S {c['price_to_sales']['risk']:.2f}x{WEIGHTS['price_to_sales']:.2f} + ERP {c['erp_tightness']['risk']:.2f}x{WEIGHTS['erp_tightness']:.2f} + Inst {c['institutional']['risk']:.2f}x{WEIGHTS['institutional']:.2f} = {r['unrounded']:.4f} -> {score}",
        "NOTES:",
        f"- Methodology unchanged ({METHODOLOGY_VERSION}: " + "/".join(f"{WEIGHTS[k]*100:.0f}" for k in WEIGHTS)
        + ", inclusive annual percentiles, fixed nominal forecast mapping); not a methodology revision.",
    ]
    g = r.get("governance")
    if g:
        lines.append(f"- Governance: {g['note']} (record: {g.get('source', 'source not supplied')}, {g.get('date', 'date not supplied')}).")

    pr = r.get("prior_run")
    if pr:
        d = score - pr["score"]
        reg = (f"within the {_regime(score)} regime" if _regime(score) == _regime(pr["score"])
               else f"from the {_regime(pr['score'])} to the {_regime(score)} regime")
        s = (f"- Prior official {pr['score']} ({pr['asof']} close; {pr.get('source', 'source not supplied')}) -> {score}, "
             f"change {d:+d}: {_change_class(d)} {reg}")
        if pr.get("weighted_deltas"):
            names = {"cape": "CAPE", "trailing_pe": "PE", "price_to_sales": "P/S",
                     "erp_tightness": "ERP tightness", "institutional": "Inst"}
            wd = sorted(pr["weighted_deltas"].items(), key=lambda kv: -abs(kv[1]))
            s += ("; weighted component moves vs the prior run's published inputs recomputed on today's history: "
                  + ", ".join(f"{names[k]} {v:+.2f}" for k, v in wd))
            inp = pr["inputs"]
            s += (f" (10Y {inp['ust10y']:.2f}% -> {rt['ust10y']:.2f}%, EY {inp['ey']:.2f}% -> {r['adjusted']['ey']:.2f}%)")
        lines.append(s + ".")

    # --- rounding edge (two-sided) ---
    lines.append(
        f"- Rounding edge: unrounded {edge['unrounded']:.4f} -> {score}; {edge['distance_up']:+.4f} to the "
        f"{edge['up_boundary']:.1f} boundary (-> {score + 1}), {-edge['distance_down']:+.4f} to {edge['down_boundary']:.1f} "
        f"(-> {score - 1}); nearest edge is {edge['nearest']} ({min(edge['distance_up'], edge['distance_down']):.4f} pts).")
    flips = [s_ for s_ in edge["scenarios"] if s_["flips"]]
    holds = [s_ for s_ in edge["scenarios"] if not s_["flips"]]
    lines.append("- Single-input alternatives (each alone): FLIPS: " + ("; ".join(_scen(s_) for s_ in flips) or "none")
                 + ". HOLDS: " + ("; ".join(_scen(s_) for s_ in holds) or "none") + ".")
    if edge["forecast_thresholds"]:
        def thr(t):
            # Printed bounds are rounded toward the safe side so the stated value really flips.
            u_ = (f"<= {_floor(t['flip_up_if_at_or_below'], 3):.3f}% -> {score + 1}" if t["flip_up_if_at_or_below"] is not None
                  else f"no midpoint reaches {score + 1}")
            d_ = (f"> {_ceil(t['flip_down_if_above'], 3):.3f}% -> {score - 1}" if t["flip_down_if_above"] is not None
                  else f"no midpoint reaches {score - 1}")
            return f"{t['provider']} (scored {t['midpoint']:.2f}%) {u_}, {d_}"
        lines.append("- Forecast-midpoint flip thresholds (one provider moved, others fixed): "
                     + "; ".join(thr(t) for t in edge["forecast_thresholds"]) + ".")
    steps = []
    for st in edge["percentile_steps"]:
        if st["threshold"] is None:
            steps.append(f"{st['metric']} {st['direction']}: no further step (already at the "
                         f"{'top' if st['direction'] == 'up' else 'bottom'})")
            continue
        up_ = st["direction"] == "up"
        op = ">=" if up_ else "<"
        rnd = _ceil if up_ else _floor
        eq = (f"10Y {rnd(st['ust10y_equiv'], 2):.2f}% ({st['ust10y_move_bp']:+.0f}bp)" if "ust10y_equiv" in st else "")
        if st.get("spx_equiv"):
            mv = st["spx_move_pct"]
            eq += ((" or " if eq else "") + f"SPX {st['spx_equiv']:.2f} ({mv:+.{1 if abs(mv) >= 0.1 else 3}f}%, "
                   "this ratio only; a real SPX move shifts every ratio)")
        steps.append(f"{st['metric']} {op} {rnd(st['threshold'], 2):.2f} [{eq}] -> {st['unrounded']:.4f} -> {st['score']}"
                     + (" FLIP" if st["flips"] else ""))
    lines.append("- Nearest percentile steps (ties included, each alone): " + "; ".join(steps) + ".")

    # --- conventions, from validation results ---
    jan1 = ob["current_year_jan1_rows_removed"]
    v = r.get("validation") or {}
    vtxt = []
    for d_, res in v.items():
        pub = dict((k, p) for k, p, _g, _ok in res["checks"])
        comp_erp = res["components"]["erp_tightness"]
        b = res["rejected_convention_B"]
        vtxt.append(f"{d_} published {pub.get('score')} vs {res['score']} here (ERP {pub.get('erp_tightness')} vs {comp_erp:.2f}; "
                    f"with the 08-14 basket {res['with_0814_basket']['score']}); convention B {b['score']} "
                    f"(ERP {b['components']['erp_tightness']:.2f}; 08-14 basket {b['score_with_0814_basket']})")
    lines.append(
        f"- Percentile convention: one obs per calendar year by row date through {r['asof'].year - 1} "
        f"(CAPE {fy['cape']}-{c['cape']['last_hist_year']}, PE {fy['pe']}-{c['trailing_pe']['last_hist_year']}, "
        f"P/S {ob['ps_first_row']:%b %d} {ob['ps_first_row'].year}-{ob['ps_last_row'].year}); Multpl 'Jan 1, {r['asof'].year}' rows "
        f"({', '.join(jan1) or 'none'}) are current-year values, removed as duplicates, and the dated current row is appended once"
        + ("; validation: " + "; ".join(vtxt) if vtxt else "")
        + f". Under rejected convention B today: {sens['rejected_convention_B']['unrounded']:.4f} -> {sens['rejected_convention_B']['score']}.")
    xr = ob["t10_crosscheck_years"]
    lines.append(
        f"- ERP alignment: each Jan 1 Y Multpl earnings yield paired with the January-Y mean of FRED DGS10"
        + (f"; that mean matches Multpl's Jan 1 10Y row in {xr[2] - len(ob['t10_crosscheck_mismatches'])} of {xr[2]} years checked ({xr[0]}-{xr[1]})"
           if xr else "; cross-check not possible (no overlapping years)")
        + (f", mismatches {ob['t10_crosscheck_mismatches']}" if ob["t10_crosscheck_mismatches"] else "")
        + f"; scored window {c['erp_tightness']['first_year']}-{c['erp_tightness']['last_hist_year']} plus current (N={c['erp_tightness']['N']}).")

    # --- price stamp, from facts ---
    om = rc.get("official_meta") or {}
    s = f"- Price-stamp: official SPX {rc['official']:.2f} ({om.get('date', a)}) from {om.get('source_label', 'source not supplied')}"
    if om.get("venue_timestamp"):
        t = dt.datetime.fromisoformat(om["venue_timestamp"])
        z = t.strftime("%z")
        s += f", stamped {t:%H:%M} UTC{z[:3]}:{z[3:]}"
    if not om.get("official_sp_dji") and om.get("substitute_reason"):
        s += f"; substitute because {om['substitute_reason']}"
    cc = om.get("cache_cross_check")
    if cc:
        s += f"; cache cross-check {cc['value']:.2f} ({cc['date']}, {cc['source']})"
        for eb in cc.get("excluded_bars", []):
            s += f", {eb['date']} bar excluded ({eb['reason']})"
    s += f". k={rc['k']:.6f} applied: CAPE/PE/PS x k, EY/DY / k."
    hist_k = [(p_["asof"], p_["official"] / p_["embedded"] if p_.get("embedded") else p_["k"], p_.get("source"))
              for p_ in om.get("prior_reconciliations", [])]
    if hist_k:
        discs = [(1.0 / k_ - 1.0) * 100.0 for _d_, k_, _s in hist_k] + [rc["discrepancy"] * 100.0]
        s += (" Prior runs: " + "; ".join(f"k {k_:.6f} ({d_}, {src})" for d_, k_, src in hist_k)
              + f"; Multpl's embedded price exceeded the official close by {min(discs):.2f}%-{max(discs):.2f}% across these runs and today.")
    lines.append(s)

    # --- stale P/S ---
    se = den["sales_end"]
    s = f"- Stale P/S denominator: 12m sales ending {_d(se)} ({den['sales_age_months']:.1f} months old"
    s += ", >12 months)." if r["material_stale"] else ")."
    if ob["sps_table_end"]:
        s += f" Multpl sales/share by-quarter table latest row {ob['sps_table_end']}."
    ps_search = (r["facts_meta"]["searches"] or {}).get("ps_sales_alternate")
    if ps_search:
        s += f" Alternate-source search {ps_search.get('date')} ({', '.join(ps_search.get('hosts', []))}): {ps_search.get('result')}."
    if c["price_to_sales"]["percentile"] >= 100.0 and se:
        s += (f" P/S stays at the 100th percentile unless TTM sales/share has grown "
              f">{sens['price_to_sales_breakeven_sales_growth']*100:.1f}% since {se:%b %Y}.")
    s += (f" Neutral-50 substitution would give {sens['price_to_sales_neutral_50']['unrounded']:.2f} -> "
          f"{sens['price_to_sales_neutral_50']['score']}.")
    if r["material_stale"]:
        s += " Confidence held in the 70-79 band for this."
    lines.append(s)

    if xc and rt["ust10y_date"] != r["asof"]:
        sd = sens["ust10y_same_day_cross_check"]
        lines.append(
            f"- Mixed rate dates: DGS10 {rt['ust10y']:.2f}% is {_d(rt['ust10y_date'])} ({ob['rate_lag_business_days']} business "
            f"day(s) before ASOF); same-day {xc['value']:.2f}% ({xc['date']}, {_host(xc.get('source_url'))}) is a cross-check "
            f"only; scoring on it would give ERP risk {sd['erp_tightness_risk']:.2f} and {sd['unrounded']:.4f} -> {sd['score']}.")
    elif rt["ust10y_date"] == r["asof"]:
        lines.append("- Rates same-day.")
    est = ob["estimated_current_rows"]
    lines.append(f"- Denominators: earnings through {_d(den['earnings_end'])}, dividends through {_d(den['dividends_end'])} "
                 f"(Multpl); current rows flagged as Multpl estimates: {', '.join(est) if est else 'none'}.")

    # --- forecasts, from facts ---
    for p in inst["providers"]:
        lines.append(f"- Forecast {p['provider']}: {p.get('source_quality') or 'source quality not supplied'} "
                     f"[{_host(p.get('source_url'))}; pub {p['publication_date']}, model {p['model_date']}, "
                     f"model age {p['age_months']:.1f} months"
                     + (f", next edition due by {p['next_edition_due']} ({p['raw']['cadence_months']}-month cadence)"
                        if (p.get("raw") or {}).get("cadence_months") else "") + "]"
                     + (f"; {p['conversion']}" if p.get("conversion") else "") + ".")
        raw = p.get("raw") or {}
        fb = p.get("fallback")
        if p.get("corroborated") is False:
            s = (f"- {p['provider']} {p['midpoint']:.1f}% rests on {raw.get('source_count', 'an unstated number of')} "
                 f"uncorroborated {raw.get('source_kind', 'secondary source')} ({_host(p.get('source_url'))}); "
                 f"{p['publication_date']} is that article's date, not a {p['provider']} publication date")
            if p.get("model_date"):
                s += f" (the article cites data as of {p['model_date']})"
            if fb:
                fs = [s_ for s_ in edge["scenarios"] if s_["change"].startswith(f"{p['provider']} {fb['value']:.1f}% (fallback")]
                s += (f". Corroborated fallback {fb['value']:.1f}% ({_host(fb.get('source_url'))}, pub {fb.get('publication_date')}"
                      + (f"; {fb['corroboration']}" if fb.get("corroboration") else "") + ")")
                if fs:
                    s += f" gives {fs[0]['unrounded']:.4f} -> {fs[0]['score']}" + (" (same score)" if fs[0]["score"] == score else " (different score)")
            lines.append(s + ".")
    for prov_, why in inst["excluded"]:
        lines.append(f"- Forecast excluded: {prov_}: {why}.")
    for f_ in inst["flags"]:
        lines.append(f"- Forecast flag: {f_}.")
    lines.append("- Valuation is a slow-moving pacing input, not a timing signal; high valuation never justifies taxable selling or an indefinite pause.")
    return "\n".join(lines)


CSV_FIELDS = ["asof_date", "score", "confidence", "cape_value", "cape_percentile", "cape_risk",
              "trailing_pe_value", "trailing_pe_percentile", "trailing_pe_risk", "price_sales_value",
              "price_sales_denominator_date", "price_sales_percentile", "price_sales_risk",
              "earnings_yield", "ust10y_nominal", "ust10y_real", "breakeven_10y", "nominal_erp",
              "erp_tightness", "erp_tightness_percentile", "erp_tightness_risk",
              "institutional_anchor_midpoint", "institutional_anchor_risk", "spy_fy1_pe",
              "factset_forward_pe", "qqq_forward_pe", "market_cap_to_gdp", "official_spx_close",
              "source_embedded_spx_price", "price_adjustment_factor", "stale_data_flags",
              "methodology_version"]


def csv_row(r):
    c, rt = r["components"], r["rates"]
    row = [r["asof"].isoformat(), r["score"], r["confidence"],
           f"{c['cape']['value']:.4f}", f"{c['cape']['percentile']:.2f}", f"{c['cape']['risk']:.2f}",
           f"{c['trailing_pe']['value']:.4f}", f"{c['trailing_pe']['percentile']:.2f}", f"{c['trailing_pe']['risk']:.2f}",
           f"{c['price_to_sales']['value']:.4f}", _d(r["denominators"]["sales_end"]),
           f"{c['price_to_sales']['percentile']:.2f}", f"{c['price_to_sales']['risk']:.2f}",
           f"{r['adjusted']['ey']:.4f}", f"{rt['ust10y']:.2f}", f"{rt['real']:.2f}", f"{rt['breakeven']:.2f}",
           f"{r['nominal_erp']:.4f}", f"{c['erp_tightness']['value']:.4f}",
           f"{c['erp_tightness']['percentile']:.2f}", f"{c['erp_tightness']['risk']:.2f}",
           f"{c['institutional']['detail']['blended_midpoint']:.4f}", f"{c['institutional']['risk']:.4f}",
           f"{r['ssga'].get('fy1_pe'):.2f}", "N/A", "N/A",
           str(r["context"].get("market_cap_to_gdp", {}).get("value", "N/A")),
           f"{r['reconciliation']['official']:.2f}", f"{r['reconciliation']['embedded']:.2f}",
           f"{r['reconciliation']['k']:.6f}", " | ".join(r["stale"] + r["flags"]), METHODOLOGY_VERSION]
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_FIELDS)
    w.writerow(row)
    return buf.getvalue()


# --------------------------------------------------------------------------------------
# Validation / self-test
# --------------------------------------------------------------------------------------
def validate(cache):
    """Feed agent-of-record published current values through today's annual history."""
    _rows, hist, _dgs10, mism = load_histories(cache, 2026)
    results, ok = {}, not mism
    for d, case in VALIDATION.items():
        comp, unr, sc = score_components(hist, case["current"], case["forecasts"], dt.date.fromisoformat(d))
        res = {"unrounded": unr, "score": sc, "components": {k: comp[k]["risk"] for k in WEIGHTS},
               "N": {k: comp[k].get("N") for k in WEIGHTS if k != "institutional"}, "checks": []}
        for k, pub in case["published"].items():
            if k == "score":
                good = sc == pub
                res["checks"].append((k, pub, sc, good))
            elif k == "unrounded":
                res["checks"].append((k, pub, round(unr, 2), None))  # informational
            else:
                good = abs(comp[k]["risk"] - pub) <= COMPONENT_TOL
                res["checks"].append((k, pub, round(comp[k]["risk"], 2), good))
            ok &= res["checks"][-1][3] is not False
        # Diagnostic only: the rejected convention (keep Multpl "Jan 1, <current year>" rows).
        _r, hb, _g, _m = load_histories(cache, 2026, conv_b=True)
        cb, ub, sb = score_components(hb, case["current"], case["forecasts"], dt.date.fromisoformat(d))
        alt_basket = VALIDATION["2026-08-14"]["forecasts"]
        cb2, ub2, sb2 = score_components(hb, case["current"], alt_basket, dt.date.fromisoformat(d))
        ca2, ua2, sa2 = score_components(hist, case["current"], alt_basket, dt.date.fromisoformat(d))
        res["rejected_convention_B"] = {"components": {k: cb[k]["risk"] for k in WEIGHTS},
                                        "unrounded": ub, "score": sb,
                                        "unrounded_with_0814_basket": ub2, "score_with_0814_basket": sb2}
        res["with_0814_basket"] = {"unrounded": ua2, "score": sa2}
        results[d] = res
    return ok, results, mism


def self_test(cache):
    # Unit checks that need no data.
    assert abs(forecast_to_risk(3.2) - 68.0) < 1e-9
    assert abs(forecast_to_risk(5.2) - 45.0) < 1e-9
    assert abs(forecast_to_risk(5.9) - 36.25) < 1e-9
    assert forecast_to_risk(9.0) == 10.0 and forecast_to_risk(-1.0) == 95.0
    assert inclusive_percentile([1, 2, 3], 2) == (75.0, 3, 4)
    k, disc, cls = reconcile(7785.76, 7851.30)
    assert abs(k - 0.99165) < 5e-6 and cls.startswith("adjust")
    assert reconcile(100.0, 100.05)[2].startswith("no_adjust")
    assert reconcile(100.0, 101.5)[2].startswith("reject")
    rows = [(dt.date(2026, 10, 29), 9.0, True), (dt.date(2026, 1, 1), 8.0, False),
            (dt.date(2025, 1, 1), 7.0, False), (dt.date(2024, 1, 1), None, False)]
    assert annual_history(rows, 2026) == [(dt.date(2025, 1, 1), 7.0)]
    # risk_to_forecast inverts the anchor mapping.
    for x in (0.5, 2.0, 3.131, 5.2, 7.9):
        assert abs(risk_to_forecast(forecast_to_risk(x)) - x) < 1e-9
    assert risk_to_forecast(95.0) is None and risk_to_forecast(10.0) is None
    # institutional_anchor staleness: >18 months is dropped only when a newer edition is due.
    asof = dt.date(2026, 10, 29)
    base = {"basis": "nominal", "low": 5.0, "high": 5.0}
    old_annual = dict(base, provider="A", publication_date="2025-01-15", model_date="2024-12-31")
    old_slow = dict(old_annual, provider="B", cadence_months=36)
    fresh = dict(base, provider="C", publication_date="2026-07-01", model_date="2026-06-30")
    ia = institutional_anchor([old_annual, old_slow, fresh], asof)
    assert [p["provider"] for p in ia["providers"]] == ["B", "C"], ia
    assert ia["excluded"][0][0] == "A" and "newer edition was due" in ia["excluded"][0][1]
    assert any("older than 18 months but kept" in f for f in ia["flags"])
    # Real-to-nominal only with a same-provider, horizon-compatible inflation assumption.
    real_ok = dict(base, provider="D", basis="real", low=2.0, high=2.0, publication_date="2026-06-01",
                   model_date="2026-05-31", inflation_assumption={"value": 2.5, "provider": "D", "horizon_years": 10})
    real_other = dict(real_ok, provider="E", inflation_assumption={"value": 2.5, "provider": "D"})
    real_none = dict(real_ok, provider="F", inflation_assumption=None)
    ia = institutional_anchor([real_ok, real_other, real_none, fresh], asof)
    got = {p["provider"]: p["midpoint"] for p in ia["providers"]}
    assert set(got) == {"D", "C"} and abs(got["D"] - real_to_nominal(2.0, 2.5)) < 1e-12
    assert abs(real_to_nominal(2.0, 2.5) - 4.55) < 1e-9
    assert {e[0] for e in ia["excluded"]} == {"E", "F"}
    # Neither rule fires on the live basket shape (RA/Vanguard/Schwab, all <= 12 months).
    live = [dict(f, low=f["low"], high=f["high"]) for f in VALIDATION["2026-09-17"]["forecasts"]]
    ia = institutional_anchor(live, asof)
    assert len(ia["providers"]) == 3 and not ia["excluded"]
    print("unit checks: PASS")
    if not cache:
        raise SystemExit("--self-test needs --cache DIR (Multpl tables + FRED DGS10) for the 08-14/09-17 validation")
    ok, results, mism = validate(cache)
    print_validation(results, mism)
    assert ok, "agent-of-record validation FAILED"
    print("validation vs agent of record: PASS")
    # Rounding-edge thresholds must actually flip when recomputed end to end.
    _rows, hist, _g, _m = load_histories(cache, 2026)
    for d, case in VALIDATION.items():
        asof_d, fc = dt.date.fromisoformat(d), case["forecasts"]
        comp, unr, sc = score_components(hist, case["current"], fc, asof_d)
        edge = rounding_edge(hist, case["current"], fc, asof_d, comp, unr, sc, 1.0)
        assert abs(edge["distance_up"] + edge["distance_down"] - 1.0) < 1e-9
        for t in edge["forecast_thresholds"]:
            i = next(j for j, f in enumerate(fc) if f["provider"] == t["provider"])
            for m, want in ((t["flip_up_if_at_or_below"], sc + 1), (t["flip_down_if_above"], sc - 1)):
                if m is None:
                    continue
                probe = m - 1e-6 if want > sc else m + 1e-6
                fc2 = [dict(f, low=probe, high=probe) if j == i else f for j, f in enumerate(fc)]
                assert score_components(hist, case["current"], fc2, asof_d)[2] == want, (d, t, want)
        for s in edge["scenarios"] + [p for p in edge["percentile_steps"] if p.get("threshold") is not None]:
            assert s["score"] == round_half_up(s["unrounded"]) and s["flips"] == (s["score"] != sc)
    print("rounding-edge thresholds: PASS")


def print_validation(results, mism):
    if mism:
        print("ERP alignment cross-check mismatches:", mism)
    for d, res in results.items():
        print(f"{d}: unrounded {res['unrounded']:.3f} -> {res['score']}  N={res['N']}")
        for k, pub, got, good in res["checks"]:
            tag = "info" if good is None else ("ok" if good else "FAIL")
            print(f"   {k:16s} published {pub:>7}  computed {got:>8}  [{tag}]")
        w = res["with_0814_basket"]
        print(f"   (chosen convention with the 08-14 forecast basket: {w['unrounded']:.3f} -> {w['score']})")
        b = res["rejected_convention_B"]
        print("   rejected convention B (keep Jan 1 current-year rows): "
              + ", ".join(f"{k} {v:.2f}" for k, v in b["components"].items())
              + f"; {b['unrounded']:.3f} -> {b['score']}; with 08-14 basket {b['unrounded_with_0814_basket']:.3f} -> {b['score_with_0814_basket']}")


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, dt.date):
        return o.isoformat()
    return o


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", metavar="DIR")
    ap.add_argument("--cache", metavar="DIR")
    ap.add_argument("--facts", metavar="JSON")
    ap.add_argument("--asof")
    ap.add_argument("--json", metavar="OUT")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.fetch:
        return 0 if fetch(a.fetch) else 1
    if a.self_test:
        self_test(a.cache)
        return 0
    if not (a.cache and a.facts):
        ap.error("--cache and --facts are required for a run")
    with open(a.facts) as fh:
        facts = json.load(fh)
    ok, results, mism = validate(a.cache)
    print("== validation vs agent of record ==")
    print_validation(results, mism)
    print("validation_pass:", ok)
    r = compute(a.cache, facts, a.asof, results)
    print("\n```\nSUPERVISOR PACKET (COPY/PASTE)")
    print(packet(r))
    print("```\n\n== CSV ==")
    print(csv_row(r))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(_jsonable({"result": r, "validation": results, "validation_pass": ok}), fh, indent=1)
    if not ok:
        print("VALIDATION FAILED: packet is not an official score until the agent-of-record reproduction passes")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
