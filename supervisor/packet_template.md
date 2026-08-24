# Standard Agent Packet Contract

Every module agent returns exactly one packet, inside a single plain-text triple-backtick code
block. Each bullet stays on one line.

Score semantics for all five modules: **0 = lowest risk; 100 = highest risk.** Higher means greater
caution and slower DCA.

`MODULE:` must be one of:
`QuantRisk`, `Macro and Policy`, `Liquidity and Credit`, `Sentiment and Technicals`,
`Valuation and Expected Returns`.

## Template

```
SUPERVISOR PACKET (COPY/PASTE)
MODULE: <one of the five official module names>
ASOF: <YYYY-MM-DD close; note mixed daily/weekly as-of dates here>
SCORE_0_100: <integer>
CONFIDENCE_0_100: <integer>
TOP_3_DRIVERS:
- driver 1
- driver 2
- driver 3
CHANGE_MY_MIND (TRIGGERS):
- trigger 1
- trigger 2
- trigger 3
KEY_METRICS:
- metric=value (date)
- metric=value (date)
NOTES:
- caveats, methodology changes, source lags, or data gaps
```

## Supervisor-side validation

On receipt, the Supervisor checks only that the packet is well formed and logs it. It does **not**
compute, synthesize, browse, or recommend until Jason types `ALL DONE`.

After `ALL DONE`:

1. All four required keys present in every packet: `MODULE`, `ASOF`, `SCORE_0_100`, `CONFIDENCE_0_100`.
2. All five official modules present. If one is missing, say so — never substitute an independently
   generated score.
3. Flag mixed as-of dates, source lags, and any disclosed methodology change.
4. Flag internally inconsistent figures and missing Quant risk levels.
5. Preserve official scores even when a data-quality issue is disclosed; reduce *narrative*
   confidence instead.
6. Never fabricate a missing metric.

## Quant packet — additional required content

The QuantRisk packet must carry, for each of SPX, NDX, SPY, QQQ, IWM, VIX: as-of, spot, `risk_raw`,
`risk_smooth`, four-week change in `risk_smooth`, `price_at_r0.2` / `r0.5` / `r0.8` / `r0.9`, the
percentage distance from spot to each level, and the history window and method.

Modern-window SPX and NDX readings are reported for comparable context; full-history index readings
are shown separately and are never mixed with ETF-era SPY/QQQ calibration. If a level is not
supplied, the Supervisor reports it as not supplied — it does not derive or invent it.
