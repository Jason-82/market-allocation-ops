#!/usr/bin/env python3
"""Deterministic arithmetic for the Market Allocation Ops Supervisor.

Mechanises only the steps that are fully specified in supervisor/master_handoff.yaml:
the weighted score blend, the weighted confidence blend, the DCA action band, the
slow-band multiplier convention, and the contribution dollar split.

It does NOT decide anything judgemental — reserve release, sleeve tilts, and the
multiplier inside the accelerate/minimal ranges stay with the Supervisor.

Usage:
    python3 tools/dca_calc.py --quant 97 --macro 59 --liquidity 15 \
        --sentiment 63 --valuation 89 \
        --quant-conf 84 --macro-conf 80 --liquidity-conf 88 \
        --sentiment-conf 84 --valuation-conf 83 --asof 2026-08-14
    python3 tools/dca_calc.py --self-test

Stdlib only. Decision support only; not financial advice.
"""

from __future__ import annotations

import argparse
import sys
from decimal import ROUND_HALF_UP, Decimal

# Official Supervisor weights. Must stay in sync with master_handoff.yaml.
WEIGHTS = {
    "quant": Decimal("0.40"),
    "macro": Decimal("0.20"),
    "liquidity": Decimal("0.20"),
    "sentiment": Decimal("0.10"),
    "valuation": Decimal("0.10"),
}

WEEKLY_BASELINE = Decimal("250000")

# The 2026-08-14 run predates the 2026-08-24 rescale and used a $312,500 baseline.
# The self-test reconciles against it at its own baseline; do not "fix" this to the
# current value or the historical reconciliation silently breaks.
HISTORICAL_BASELINE_2026_08_14 = Decimal("312500")
CORE_SHARE = Decimal("0.80")
SLEEVE_SHARE = Decimal("0.20")

CENT = Decimal("0.01")

# Schwab Intelligent Portfolios holds a mandatory cash allocation that cannot be
# switched off. Jason reports ~7% on an aggressive profile. Only the core is affected.
# Measured 2026-08-24 across the four confirmed Schwab Intelligent Portfolios accounts,
# market-value weighted. Schwab sizes the cash allocation to the account's risk profile, so
# this is NOT a fixed 7% -- it ranged 5.69% (most aggressive) to 7.38% across Jason's accounts.
# Pass --robo-cash to model a specific destination account.
ROBO_CASH_FRACTION = Decimal("0.0677")
ROBO_CASH_BY_ACCOUNT = {
    "trust1": Decimal("0.0738"),      # Jason Trust 1 IP ...821
    "trust2": Decimal("0.0710"),      # Jason Trust 2 IP ...422
    "roth": Decimal("0.0640"),        # Jason Roth IRA IP ...589
    "individual": Decimal("0.0569"),  # Jason Individual ...547 -- most aggressive profile
    "blended": Decimal("0.0677"),
}


def blend(values: dict[str, Decimal]) -> Decimal:
    """Weighted blend, unrounded. Raises if a module is missing."""
    missing = sorted(set(WEIGHTS) - set(values))
    if missing:
        raise ValueError(
            "missing module input(s): "
            + ", ".join(missing)
            + " — never substitute an independently generated score"
        )
    return sum((WEIGHTS[k] * values[k] for k in WEIGHTS), Decimal(0))


def action_band(score: Decimal) -> tuple[str, str]:
    """Map an OverallRiskScore to (regime, multiplier range) per the default bands."""
    if score < 0 or score > 100:
        raise ValueError(f"OverallRiskScore {score} outside 0-100")
    if score < 35:
        return "Accelerate DCA", "1.25x to 1.50x"
    if score < 60:
        return "Baseline DCA", "1.00x"
    if score < 80:
        return "Slow DCA", "0.50x to 0.75x"
    return "Minimal DCA", "0.00x to 0.25x"


def raw_multiplier(score: Decimal) -> Decimal | None:
    """Raw multiplier where a formula exists; None where the band needs judgement.

    Only the slow band (60 <= score < 80) has a settled convention:
        0.75 - (score - 60) * 0.0125, i.e. 0.75 at 60 and 0.50 at 80.
    The formula is never extended past its domain.
    """
    if score < 35:
        return None  # accelerate band: pick within 1.25x-1.50x
    if score < 60:
        return Decimal("1.00")
    if score < 80:
        return Decimal("0.75") - (score - Decimal("60")) * Decimal("0.0125")
    return None  # minimal band: pick within 0.00x-0.25x


def to_operational(mult: Decimal) -> Decimal:
    """Round a raw multiplier to the nearest 0.05x."""
    steps = (mult / Decimal("0.05")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (steps * Decimal("0.05")).quantize(Decimal("0.01"))


def contribution_math(mult: Decimal, baseline: Decimal = WEEKLY_BASELINE) -> dict[str, Decimal]:
    total = (baseline * mult).quantize(CENT, rounding=ROUND_HALF_UP)
    core = (total * CORE_SHARE).quantize(CENT, rounding=ROUND_HALF_UP)
    sleeve = total - core  # residual, so core + sleeve reconciles exactly to total
    return {
        "weekly_baseline": baseline.quantize(CENT),
        "total_deploy": total,
        "core_amount": core,
        "sleeve_amount": sleeve,
        "incremental_holdback": (baseline - total).quantize(CENT),
    }


def effective_deployment(
    total_deploy: Decimal, core_amount: Decimal, robo_cash: Decimal = ROBO_CASH_FRACTION
) -> dict[str, Decimal]:
    """Portion of a contribution that actually reaches the market.

    The core sits in Schwab Intelligent Portfolios, which parks a mandatory slice in
    Schwab Bank Sweep. Reporting the nominal multiplier alone overstates the real pace.
    """
    parked = (core_amount * robo_cash).quantize(CENT, rounding=ROUND_HALF_UP)
    return {"cash_parked_in_core": parked, "market_deploy": total_deploy - parked}


def sleeve_lines(sleeve_amount: Decimal, weights: dict[str, Decimal]) -> dict[str, Decimal]:
    """Split the sleeve into per-ticker dollars that sum exactly to sleeve_amount.

    Largest-remainder allocation: cent-level drift lands on the largest line rather
    than leaving the lines short of the sleeve total.
    """
    total_w = sum(weights.values(), Decimal(0))
    if total_w != Decimal("1"):
        raise ValueError(f"sleeve weights sum to {total_w}, expected exactly 1")
    lines = {t: (sleeve_amount * w).quantize(CENT, rounding=ROUND_HALF_UP) for t, w in weights.items()}
    drift = sleeve_amount - sum(lines.values(), Decimal(0))
    if drift:
        biggest = max(weights, key=lambda t: weights[t])
        lines[biggest] += drift
    return lines


def compute(
    scores: dict[str, Decimal],
    confs: dict[str, Decimal] | None = None,
    baseline: Decimal = WEEKLY_BASELINE,
    robo_cash: Decimal = ROBO_CASH_FRACTION,
) -> dict:
    overall = blend(scores)
    regime, mult_range = action_band(overall)
    raw = raw_multiplier(overall)
    out = {
        "scores": dict(scores),
        "contributions": {k: (WEIGHTS[k] * scores[k]) for k in WEIGHTS},
        "overall_risk_score_exact": overall,
        "overall_risk_score": overall.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
        "regime": regime,
        "multiplier_range": mult_range,
        "raw_multiplier": raw,
    }
    if confs is not None:
        wc = blend(confs)
        out["weighted_confidence_exact"] = wc
        out["weighted_confidence"] = wc.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if raw is not None:
        op = to_operational(raw)
        out["operational_multiplier"] = op
        out.update(contribution_math(op, baseline))
        out.update(effective_deployment(out["total_deploy"], out["core_amount"], robo_cash))
        out["robo_cash_rate"] = robo_cash
        out["effective_multiplier"] = (out["market_deploy"] / baseline).quantize(Decimal("0.0001"))
    return out


def render(res: dict, asof: str | None) -> str:
    lines = []
    if asof:
        lines.append(f"ASOF: {asof}")
    for k in WEIGHTS:
        lines.append(f"  {k:<10} weight={WEIGHTS[k]}  weighted_contribution={res['contributions'][k]}")
    lines.append(f"OverallRiskScore: {res['overall_risk_score']}  (exact {res['overall_risk_score_exact']})")
    if "weighted_confidence" in res:
        lines.append(f"WeightedConfidence: {res['weighted_confidence']}  (exact {res['weighted_confidence_exact']})")
    lines.append(f"DCA regime: {res['regime']}  (band {res['multiplier_range']})")
    if res["raw_multiplier"] is None:
        lines.append(
            "Raw multiplier: no settled formula in this band — Supervisor selects within the band range."
        )
        return "\n".join(lines)
    lines.append(f"Raw multiplier: {res['raw_multiplier']}")
    lines.append(f"Operational multiplier: {res['operational_multiplier']}x")
    lines.append(f"Weekly baseline: ${res['weekly_baseline']:,}")
    lines.append(f"Total deploy: ${res['total_deploy']:,}")
    lines.append(f"  Core (80%): ${res['core_amount']:,}")
    lines.append(f"  Sleeve (20%, manual): ${res['sleeve_amount']:,}")
    lines.append(f"Incremental holdback: ${res['incremental_holdback']:,}")
    lines.append(
        f"  less mandatory robo cash ({res['robo_cash_rate']:.2%} of core): ${res['cash_parked_in_core']:,}"
    )
    lines.append(
        f"  reaching the market: ${res['market_deploy']:,}"
        f"  -> effective multiplier {res['effective_multiplier']}x"
    )
    lines.append("")
    lines.append(
        "asof,quant,macro,liquidity,sentiment,valuation,overall,regime,raw_mult,op_mult,"
        "baseline,deploy,core,sleeve,holdback"
    )
    lines.append(
        ",".join(
            [
                asof or "N/A",
                *(str(res["scores"][m]) for m in WEIGHTS),
                str(res["overall_risk_score"]),
                res["regime"],
                str(res["raw_multiplier"].quantize(Decimal("0.0001"))),
                str(res["operational_multiplier"]),
                str(res["weekly_baseline"]),
                str(res["total_deploy"]),
                str(res["core_amount"]),
                str(res["sleeve_amount"]),
                str(res["incremental_holdback"]),
            ]
        )
    )
    return "\n".join(lines)


def self_test() -> int:
    """Reproduce the 2026-08-14 Supervisor run recorded in the continuity snapshot."""
    d = Decimal
    scores = {"quant": d(97), "macro": d(59), "liquidity": d(15), "sentiment": d(63), "valuation": d(89)}
    confs = {"quant": d(84), "macro": d(80), "liquidity": d(88), "sentiment": d(84), "valuation": d(83)}
    res = compute(scores, confs, HISTORICAL_BASELINE_2026_08_14)

    checks = [
        ("overall_risk_score", res["overall_risk_score"], d("68.8")),
        ("weighted_confidence", res["weighted_confidence"], d("83.9")),
        ("regime", res["regime"], "Slow DCA"),
        ("raw_multiplier", res["raw_multiplier"], d("0.64")),
        ("operational_multiplier", res["operational_multiplier"], d("0.65")),
        ("total_deploy", res["total_deploy"], d("203125.00")),
        ("core_amount", res["core_amount"], d("162500.00")),
        ("sleeve_amount", res["sleeve_amount"], d("40625.00")),
        ("incremental_holdback", res["incremental_holdback"], d("109375.00")),
    ]

    # Band edges and the slow-band endpoints.
    checks += [
        ("band@34.9", action_band(d("34.9"))[0], "Accelerate DCA"),
        ("band@35", action_band(d("35"))[0], "Baseline DCA"),
        ("band@59.9", action_band(d("59.9"))[0], "Baseline DCA"),
        ("band@60", action_band(d("60"))[0], "Slow DCA"),
        ("band@79.9", action_band(d("79.9"))[0], "Slow DCA"),
        ("band@80", action_band(d("80"))[0], "Minimal DCA"),
        ("raw@60", raw_multiplier(d("60")), d("0.75")),
        ("raw@79.9999", to_operational(raw_multiplier(d("79.9999"))), d("0.50")),
        ("minimal has no formula", raw_multiplier(d("85")), None),
        ("accelerate has no formula", raw_multiplier(d("20")), None),
    ]

    # Sleeve lines reconcile exactly to the sleeve amount.
    normal = {
        "QQQM": d("0.225"), "QUAL": d("0.100"), "DFAC": d("0.100"), "DUHP": d("0.100"),
        "SPMO": d("0.050"), "DISV": d("0.150"), "DFIV": d("0.150"), "DFEV": d("0.125"),
    }
    lines = sleeve_lines(res["sleeve_amount"], normal)
    checks.append(("sleeve lines sum", sum(lines.values(), d(0)), res["sleeve_amount"]))
    checks.append(("core+sleeve == deploy", res["core_amount"] + res["sleeve_amount"], res["total_deploy"]))
    checks.append(("deploy+holdback == baseline", res["total_deploy"] + res["incremental_holdback"], d("312500.00")))

    # Current-baseline arithmetic at the same 0.65x, so a rescale regression is caught.
    cur = compute(scores, confs)
    checks += [
        ("current baseline", cur["weekly_baseline"], d("250000.00")),
        ("current deploy @0.65x", cur["total_deploy"], d("162500.00")),
        ("current core", cur["core_amount"], d("130000.00")),
        ("current sleeve", cur["sleeve_amount"], d("32500.00")),
        ("current holdback", cur["incremental_holdback"], d("87500.00")),
        ("robo cash parked", cur["cash_parked_in_core"], d("8801.00")),
        ("market deploy", cur["market_deploy"], d("153699.00")),
        ("effective mult", cur["effective_multiplier"], d("0.6148")),
    ]

    failed = 0
    for name, got, want in checks:
        ok = got == want
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--asof")
    p.add_argument("--baseline", default=str(WEEKLY_BASELINE))
    p.add_argument("--robo-cash", default=None,
                   help="Robo cash fraction, or an account key: "
                        + ", ".join(ROBO_CASH_BY_ACCOUNT))
    for m in WEIGHTS:
        p.add_argument(f"--{m}", type=str, help=f"{m} module score 0-100")
        p.add_argument(f"--{m}-conf", type=str, help=f"{m} module confidence 0-100")
    a = p.parse_args(argv)

    if a.self_test:
        return self_test()

    raw_scores = {m: getattr(a, m) for m in WEIGHTS}
    if any(v is None for v in raw_scores.values()):
        p.error("all five module scores are required (or use --self-test)")
    scores = {m: Decimal(v) for m, v in raw_scores.items()}

    raw_confs = {m: getattr(a, f"{m}_conf") for m in WEIGHTS}
    confs = {m: Decimal(v) for m, v in raw_confs.items()} if all(v is not None for v in raw_confs.values()) else None

    rc = ROBO_CASH_FRACTION
    if a.robo_cash:
        rc = ROBO_CASH_BY_ACCOUNT.get(a.robo_cash, None) or Decimal(a.robo_cash)
    res = compute(scores, confs, Decimal(a.baseline), rc)
    print(render(res, a.asof))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
