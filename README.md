# Day Trade Momentum Scanner

A free, local, Ross Cameron (Warrior Trading)-style momentum scanner.
Runs on your PC while you trade; dashboard at http://127.0.0.1:8124
refreshing every second. Data: Alpaca free API + SEC EDGAR + ForexFactory
calendar — no paid subscriptions.

## Panels

1. **Top Gainers** — biggest % movers over a rolling 5 / 10 / 15-minute
   window (toggle in the header). Candidates come from three places:
   Alpaca's SIP-based screener (top 50 gainers + top 100 most active), a
   sweep of every common stock once a minute that picks up anything in the
   price band up 10% or more, and breaking news. The screener lists reset
   at the bell and show only the top 50; the sweep sees the rest, and sees
   pre-market movers from IEX's 08:00 open.
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
   | Volume today | ≥ 500K shares, on the real tape (see below) |
   | 30-day average volume | ≥ 10k shares |
   | VWAP | price must be above it |
   | News | a scored catalyst, with dilution vetoed |
   | Chinese companies | only on **breaking** news (under 60 min old) |

   The free live feed is IEX only, a few percent of the tape, so the 500K
   floor is not counted on it. The real consolidated (SIP) tape is free on
   Alpaca once it is 15 minutes old: volume today is SIP up to 16 minutes
   ago plus IEX since, which can only under-count (`scanner/volume.py`).
   Right after news breaks, that lag can hold an entry back a few minutes.

   A company's country comes from its SEC record (the business address,
   since most are Cayman holding companies operating in China or Hong
   Kong). The bot needs a catalyst for every stock, but for everything else
   a day-old one still counts; a Chinese stock's has to be breaking. A
   country not yet known is treated as Chinese.

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

It trades Ross Cameron's first pullback, and sells the way he does: "I
will not sell just because I'm up 20 cents."

- $1–$5 symbols only, entries **08:00–10:00 ET** (pre-market from IEX's
  08:00 open through the first half hour), max 10 trades/day, never the
  same symbol twice in a day
- **Breaking news finds the stocks**: every 10 seconds the bot reads the
  whole market's headlines, and any stock with fresh news is watched for
  four hours. Before the bell this is the only way to see the day's movers
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
- Exits: **no target and no clock**. The 5% stop, or the first of Ross's
  chart exit indicators on a completed candle: a red candle closing under
  the prior candle's low, a topping tail (upper wick at least twice the
  body and half the candle), or a close under VWAP. Everything flattened
  15:50 ET; the day ends after 4 losing trades. On Jan–Aug 2026 the same
  55 entries lost −0.22R each with the old +20c scalp and −0.04R with these
  exits - better, and still not a proven edge. `bot_exit_mode = "scalp"`
  brings back the +20c target, 65% banked, trailing runner, stall and time
  stops.
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

  Alerts are still graded against +20c: that label is what the model
  learns from, and it asks whether the move had legs, not how the bot sold.

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
- When the session ends — at the cutoff, on a crash, or because the bot
  died — a **protect** step checks every open position has a stop at the
  broker, and places one if not. Regular-hours stops already live at
  Alpaca; a pre-market position's stop is the bot itself, so a session that
  dies holding one would otherwise leave it unguarded until 15:50. Before
  the bell it first tries to sell with an extended-hours limit.
- **flatten** runs near 15:50 ET as a safety net: reconciles fills and
  closes anything still open. (Broker-held stops and trailing stops work
  with no process running.)
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

### Pre-market: found through breaking news

cron-job.org starts the session at 07:30 ET; entries open at **08:00**, when
IEX's pre-market begins. Pre-market was switched off on 2026-09-30 because
the free data could not see the day's movers:

- **Alpaca's free movers list resets at the bell**, so before 09:30 it
  offered ETFs, megacaps and yesterday's runners. The bot now reads the
  whole market's news feed instead: a stock with a fresh headline is
  watched and scanned like any other.
- **A stock with no bar for today yet showed yesterday's numbers.** Fixed:
  such a stock is measured from yesterday's close, with no volume carried
  over and yesterday's last minute bar left out.

Setting `bot_window_open = "09:30"` switches pre-market trading off again.

Before 09:30 Alpaca accepts only extended-hours **limit** orders — no stop,
no OTO, no market order — so pre-market positions run differently:

- Entry is a limit at the ask **+10c** (Ross's offset), using the ask only
  when it sits within 3% of the last trade; sized so a fill at the top of
  the limit still risks $50. The 10c offset is pre-market only.
- **The bot runs the stop itself**, as extended-hours limit sells that are
  re-priced 10c lower every 5 seconds until they fill. A position whose tape
  hasn't printed for 30 seconds is closed — the stop can't see a price that
  isn't trading.
- At **09:30** the stop is handed to Alpaca as a real stop order, unless
  the price is already through it, in which case the position is closed.

Measured honestly: the Jan–Aug 2026 replay of an 08:00–10:00 window lost
about −0.26R a trade, and the replay assumes stops fill at the stop price,
which a thin pre-market book won't. The replay rebuilds each day from the
whole market's bars, so it sees every gapper; the live bot now finds them
only when they have news.

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
- **Premarket** coverage depends on news: Alpaca's movers list resets at
  the open, so a pre-market mover without a headline stays invisible.
- **Candle exits sell at the candle's close in the backtest.** Live, the
  bot sees a candle as completed only once the next one starts, then sells
  at market - a little later, at whatever price that is.
- **Spread and slippage are not modelled at all.** On $1-5 low-float names
  the round trip can be a full percent or more, and the measured edge has
  been the same order of magnitude - so a backtest that clears break-even
  is not evidence that live trading would.
- This finds *candidates*, not trades. It doesn't validate entries, risk,
  or any strategy. Not financial advice.
