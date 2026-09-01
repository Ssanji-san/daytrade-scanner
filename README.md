# Day Trade Momentum Scanner

A free, local, Ross Cameron (Warrior Trading)-style momentum scanner.
Runs on your PC while you trade; dashboard at http://127.0.0.1:8124
refreshing every second. Data: Alpaca free API + SEC EDGAR + ForexFactory
calendar — no paid subscriptions.

## Panels

1. **Top Gainers** — biggest % movers over a rolling 5 / 10 / 15-minute
   window (toggle in the header). Candidates come from Alpaca's SIP-based
   screener (top 50 gainers + top 100 most active), tracked in memory.
2. **HOD Momentum** — $1–$5 stocks at/near their high of day, filtered on
   Ross Cameron's stock-selection criteria (defaults in `scanner/config.py`;
   the panel header states the gates actually in force):
   | Criterion | Default |
   |---|---|
   | Price | $1–$5 (watched to $10, never bought above $5) |
   | Float | < 20M shares |
   | % up today | ≥ 10% |
   | % up since the 9:30 bell | ≥ 5% |
   | Relative volume | ≥ 5× |
   | 30-day average volume | ≥ 10k shares |
   | VWAP | price must be above it |
   | News | a scored catalyst, with dilution vetoed |

   The absolute daily-volume floor is **disabled** on purpose: the free feed
   is IEX only, a slice of the consolidated tape, so a raw share count means
   something different for every stock. Relative volume carries the
   liquidity test instead — it compares IEX to IEX, so the feed's share
   cancels out of the ratio.

   Dimmed rows failed one or two criteria (the chip says which) — they're
   what's about to qualify, and they are graded for learning but never
   traded. Rows flash on new entries; enable Sound for a beep on new
   qualifiers.
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

It trades Ross Cameron's cents-on-the-dollar scalp: take the 20c, bank
most of it, let the rest ride.

- $1–$5 symbols only, entries **09:30–12:30 ET**, max 10 trades/day, never
  the same symbol twice in a day
- Entry is the **pullback, not the high**: one to three red candles off a
  swing high, then a break of the prior candle's high — or, for a gapper
  with no flag yet, a break of the first five minutes' range. No setup, no
  trade; buying at the high is the chasing this exists to avoid.
- Positions are **$1,000 units**, each risking 5% against the flat 5% stop —
  $50, at any share price. The live account balance decides how *many* fit,
  up to 5 at once: $2,473.74 opens $1,000 + $1,000 + $473, and a leftover
  slice under $150 is skipped as not worth the spread. Growth buys more
  slots rather than fatter trades, so one bad name never costs more than it
  did yesterday.
- Exits: **+20c target with 65% banked there**; the remaining 35% rides a
  trailing stop capped so it can never come back below what was paid; out
  on two doji bars (the move has stalled); a 10-minute time stop on a
  position that hasn't paid yet; everything flattened 15:50 ET; the day
  ends after 4 losing trades
- Entry and stop go out as **one atomic OTO order** — submitted separately
  they trip Alpaca's wash-trade guard and every entry is refused
- **Learning**: every qualified alert (taken or not) is journaled to
  `cache/journal.db` and tracked for 10 minutes — did it reach +20c before
  its stop? A small logistic-regression model retrains on those outcomes
  and ranks tomorrow's alerts; until 40 labeled alerts exist, a transparent
  rvol+catalyst heuristic does the ranking. Rows that miss by one or two
  criteria are graded too, and never traded. The dashboard's Bot panel
  shows win rate, expectancy (in R), model accuracy, and the paper equity
  curve so you can see whether it's actually improving.

  Worth knowing what a fixed-cent target implies: against a 5% stop, 20c is
  a 4:1 reward on a $1 stock and 0.8:1 on a $5 one, because the same money
  buys five times as many shares down there. That is why the price band is
  narrow, and why `scripts/backtest.py --trades` reports by price bucket.

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
  12:45 ET, and pushes the journal + a status snapshot every ~10 min.
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
ET). No separate mode is needed: the bot's entry window is 09:30-12:30 ET, so
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
