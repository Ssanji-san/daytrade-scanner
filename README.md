# Day Trade Momentum Scanner

A free, local, Ross Cameron (Warrior Trading)-style momentum scanner.
Runs on your PC while you trade; dashboard at http://127.0.0.1:8124
refreshing every second. Data: Alpaca free API + SEC EDGAR + ForexFactory
calendar — no paid subscriptions.

## Panels

1. **Top Gainers** — biggest % movers over a rolling 5 / 10 / 15-minute
   window (toggle in the header). Candidates come from Alpaca's SIP-based
   screener (top 50 gainers + top 100 most active), tracked in memory.
2. **HOD Momentum** — $1–$20 stocks at/near their high of day, filtered on
   Ross Cameron's five stock-selection criteria (defaults in
   `scanner/config.py`):
   | Criterion | Default |
   |---|---|
   | Float | < 20M shares |
   | % up today | ≥ 10% |
   | Volume traded | ≥ 100k shares |
   | Relative volume | ≥ 5× |
   | News | badge always; "News required" toggle in the UI |

   Dimmed rows failed exactly **one** criterion (the chip says which) —
   they're what's about to qualify. Rows flash on new entries; enable
   Sound for a beep on new qualifiers.
3. **News** — ForexFactory economic calendar (red/orange impact only) +
   live Benzinga headlines for the symbols on your scanners.

## Run it

```
.venv\Scripts\python -m scanner.main --demo     # synthetic data, no keys needed
.venv\Scripts\python -m scanner.main            # live (market hours)
```

Live mode needs your Alpaca keys as environment variables:

```
set ALPACA_KEY=your_key
set ALPACA_SECRET=your_secret
.venv\Scripts\python -m scanner.main
```

Never put keys in files inside this repo. `.env` is gitignored as a
safety net, but environment variables are the intended path.

First-time setup: `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`

## Tests

```
.venv\Scripts\python -m pytest
```

## Honest limitations

- **Quotes are IEX** (Alpaca free plan): thin small-caps can print
  slightly stale prices and understated volume. The gainer/most-active
  *lists* themselves are full SIP, so you won't miss the movers.
  Relative volume compares IEX to IEX, so the ratio stays meaningful.
- **Float ≈ shares outstanding** (SEC EDGAR, cached weekly). True float
  needs paid data; treat the ≈ column as an upper bound.
- **Premarket** coverage is best-effort: Alpaca's movers list resets at
  the open.
- This finds *candidates*, not trades. It doesn't validate entries, risk,
  or any strategy. Not financial advice.
