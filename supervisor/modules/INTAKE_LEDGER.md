# Intake ledger — cycle of 2026-08-24

Jason sent five pastes. Two carried the same document, so only **four distinct modules**
arrived. Filed by the `module_name` each document declares about itself, not by the
label on the paste.

| # | Jason's label | Document's own `module_name` | Score | Conf | Module as-of | Filed as |
|---|---|---|---|---|---|---|
| 1 | "QUant packet" | Quant Risk | 97 | 84 | 2026-08-14 close | `quant_risk.yaml` |
| 2 | "macro packet" | Macro & Policy | 59 | 80 | 2026-08-13 close | `macro_policy.yaml` |
| 3 | "liquidity packet" | **Macro & Policy** (duplicate of #2) | 59 | 80 | 2026-08-13 close | — dropped as dup |
| 4 | "sentiment packet" | Liquidity/Credit | 17 | 88 | 2026-08-21 close | `liquidity_credit.yaml` |
| 5 | "valuation packet" | Sentiment & Technicals | 63 | 84 | 2026-08-14 close | `sentiment_technicals.yaml` |

**Valuation & Expected Returns arrived separately later on 2026-08-24**, completing the
cycle: score 89, confidence 83, as-of 2026-08-14 close, filed as
`valuation_expected_returns.yaml`. The run was computed only after all five were present;
no substituted or provisional score was ever published as official.

From paste #3 onward each paste carried the module one slot behind its label, so Valuation
fell off the end of the initial sequence and had to be requested.

## Corroborating evidence beyond the `module_name` line

Paste #3 contains the Macro signature content and none of the Liquidity content:
CPI/PCE anchors, SAHMCURRENT, DGS10/DGS2/DGS3MO, the 30/25/15/15/15 component weights,
and `score: 59`. It contains no HY OAS, no WRESBAL, no SOFR/IORB.

No paste in the *initial five* contains any Valuation signature metric — CAPE, Shiller,
price-to-sales, earnings yield, ERP, market-cap-to-GDP, or Research Affiliates —
except as passing cross-module boundary references ("Valuation owns ...").

Verify with:

```
grep -ril 'CAPE\|Shiller\|price.to.sales\|earnings yield\|Research Affiliates' supervisor/modules/
```

## Data-freshness flags for the run

Three of the four received modules carry values **identical to the prior Supervisor run**
recorded in `continuity_snapshot.yaml` (2026-08-14), and each says so about itself:

- Quant Risk 97 — "a continuation estimate, not a clean full-history recomputation";
  "Do not use the August 14 levels as a substitute for a fresh update after August 14."
- Macro & Policy 59 — "Run a fresh full update before treating 59 as operationally current."
- Sentiment & Technicals 63 — "inherited from the 2026-08-14 report ... not re-fetched
  or recalculated."

Only Liquidity/Credit refreshed: 15 → 17, as of 2026-08-21 close.

Consequence: 70% of the blend weight would be built on data 10–11 days old at the
2026-08-24 report date, and the prior-run comparison for those three modules would be
against themselves (zero change by construction). This must be disclosed conspicuously
in any report built on this cycle.


## Outcome

Official run computed 2026-08-24 with all five modules present:
OverallRiskScore **69.2**, WeightedConfidence **83.9**, Slow DCA, operational multiplier
**0.65x**, effective **0.6136x** after robo cash. Prior 68.8 -> +0.4, essentially unchanged,
and the entire move is Liquidity 15 -> 17 (0.20 x +2 = +0.40). The other four modules
contributed exactly zero change because they carry the prior run's values unchanged.

Also verified at report time: August CPI is Friday 2026-09-11, 8:30 a.m. ET. An earlier
concern that a Friday CPI looked atypical for BLS did not hold up; the packet's date is
correct. bls.gov, federalreserve.gov and schwab.com are all blocked by this environment's
egress proxy, so catalyst dates and Schwab figures are secondary-sourced only.
