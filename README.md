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
.venv\Scripts\python -m scanner.main --bot      # live + paper-trading bot
```

## The paper-trading bot (`--bot`)

Trades HOD-momentum alerts on your **Alpaca paper account** — it is
hard-locked to `paper-api.alpaca.markets` and refuses to start against
anything else. Rules (all tunable in `scanner/config.py`):

- $2–$20 symbols only, entries 9:35–11:30 ET, **max 4 trades/day**,
  never the same symbol twice in a day
- Sizes off a simulated **$5,000 bankroll** (1% = $50 risk per trade),
  regardless of the paper account's fake $100k
- Exits: stop −3% (= 1R); shares split into two bracket orders — half
  takes profit at **+2R**, half at **+3R**; 20-min time stop; everything
  flattened 15:50 ET; kill switch stops entries at −3% day PnL
- **Learning**: every qualified alert (taken or not) is journaled to
  `cache/journal.db` and tracked for 30 minutes (did it reach +2R before
  −1R?). A small logistic-regression model retrains on those outcomes and
  ranks tomorrow's alerts; until 40 labeled alerts exist, a transparent
  rvol+news heuristic does the ranking. The dashboard's Bot panel shows
  win rate, expectancy (in R), model accuracy, and the paper equity curve
  so you can see whether it's actually improving.

Live mode needs your Alpaca keys as environment variables:

```
set ALPACA_KEY=your_key
set ALPACA_SECRET=your_secret
.venv\Scripts\python -m scanner.main
```

Never put keys in files inside this repo. `.env` is gitignored as a
safety net, but environment variables are the intended path.

First-time setup: `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`

## Running it in the cloud (GitHub Actions)

You don't need your PC on: two workflows run the whole thing on GitHub's
servers every weekday.

- **trading-session** starts before the open, scans + trades until
  12:15 ET, and pushes the journal + a status snapshot every ~10 min.
- **flatten** runs near 15:50 ET as a safety net: reconciles fills and
  closes anything still open. (Bracket stops/targets live on Alpaca's
  servers, so exits work even with no process running.)
- **GitHub Pages** (from `/docs`) serves the same dashboard, readable
  from your phone; it updates each time the session pushes (~10 min lag).

One-time setup:

1. Create a **public** GitHub repo (public = unlimited free Actions
   minutes; only paper trades and code are published, never keys) and
   push this project to it.
2. Repo → Settings → Secrets and variables → Actions → add secrets
   `ALPACA_KEY` and `ALPACA_SECRET` (your **paper** keys).
3. Settings → Pages → deploy from branch `main`, folder `/docs`.
4. Actions tab → enable workflows. Test with "Run workflow" on
   `trading-session` during market hours.

### Premarket observation

The session may be started before the bell (cron-job.org fires it at 07:30
ET). No separate mode is needed: the bot's entry window is 09:30-11:30 ET, so
premarket it scans and journals but **cannot** place an order. Those rows land
in the alert journal as observation-only data, which is what a premarket
strategy would have to be trained on - the bot has never seen that regime, so
it is being recorded before anything is built on it.

Note the two different news sources: the red/orange **economic calendar**
(ForexFactory) is macro - CPI, FOMC - and moves the whole market. Per-stock
catalysts come from **Benzinga** headlines and are what `scanner/catalyst.py`
scores. A premarket catalyst strategy runs on the Benzinga path.

Notes: GitHub cron can start a few minutes late (fine — the bot's entry
window is enforced in ET regardless). The trade journal
(`cache/journal.db`) is committed by the workflows so learning persists
between days — avoid running `--bot` locally on days the cloud session
trades, or the journals will fight.

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
- **Backtested scalp results are an upper bound.** The simulator fills the
  +20c target off the bar HIGH; a live session can only compare the last
  polled price, and cannot see the high of a minute still in progress. A
  wick that tags the target and retreats inside the same minute pays in the
  backtest and does not pay live. Both functions carry a comment saying so.
- **Spread and slippage are not modelled at all.** On $1-5 low-float names
  the round trip can be a full percent or more, and the measured edge has
  been the same order of magnitude - so a backtest that clears break-even
  is not evidence that live trading would.
- This finds *candidates*, not trades. It doesn't validate entries, risk,
  or any strategy. Not financial advice.
