# Market Allocation Ops

Configuration and arithmetic for a rules-based cash deployment program: five independent analysis
modules publish 0–100 risk scores, a Supervisor blends them into one score, and that score sets the
pace of the next weekly contribution.

**Decision support only; not financial advice.**

## The idea

A large cash balance (~$8M) is deployed into diversified public markets on a weekly schedule at a
$250,000 baseline. The lever is *pace*, not participation — contributions continue in every regime,
but the size varies with measured risk. Slowing below 1.0x accumulates reserve capital for release
after a meaningful de-extension.

The 80% core runs through Schwab Intelligent Portfolios, which holds a mandatory cash slice, so the
calculator reports an effective multiplier alongside the nominal one.

| Overall score | Regime | Multiplier |
|---|---|---|
| 0 – <35 | Accelerate DCA | 1.25x – 1.50x |
| 35 – <60 | Baseline DCA | 1.00x |
| 60 – <80 | Slow DCA | 0.50x – 0.75x |
| 80 – 100 | Minimal DCA | 0.00x – 0.25x |

## Modules and weights

| Module | Weight | Measures |
|---|---|---|
| Quant Risk | 0.40 | Price extension vs. long-term trend and deviation bands |
| Macro and Policy | 0.20 | Inflation, labor, growth, curve, monetary policy |
| Liquidity and Credit | 0.20 | Credit spreads, funding, reserves, financial conditions |
| Sentiment and Technicals | 0.10 | Complacency, trend, breadth, leadership |
| Valuation and Expected Returns | 0.10 | Long-horizon valuation and expected-return headwinds |

All scores run 0–100 with higher meaning more risk and greater caution.

## Layout

```
CLAUDE.md                            operating instructions for the Supervisor
supervisor/master_handoff.yaml       authoritative spec — weights, formulas, rules, guardrails
supervisor/continuity_snapshot.yaml  last recorded run (prior-run comparison only)
supervisor/packet_template.md        module packet contract
tools/dca_calc.py                    deterministic score/band/dollar arithmetic
lp_bot/                              experimental cross-chain LP scanner and paper-trading
                                     position manager (see lp_bot/README.md)
```

## Usage

```
python3 tools/dca_calc.py --self-test

python3 tools/dca_calc.py --asof 2026-08-24 \
  --quant 97 --macro 59 --liquidity 15 --sentiment 63 --valuation 89 \
  --quant-conf 84 --macro-conf 80 --liquidity-conf 88 --sentiment-conf 84 --valuation-conf 83
```

The calculator mechanises only what is fully specified: the two weighted blends, the band mapping,
the slow-band multiplier convention, and the contribution split. Reserve release, sleeve tilts, and
multiplier selection inside the accelerate and minimal ranges remain judgement calls for the
Supervisor.
