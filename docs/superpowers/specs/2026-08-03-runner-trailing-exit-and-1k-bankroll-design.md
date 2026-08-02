# Design: $1,000 bankroll + scale-out/trailing-runner exit

**Date:** 2026-08-03
**Status:** Draft for review
**Component:** `daytrade-scanner` paper-trading bot (`scanner/trading/`, `scanner/config.py`)

## Context

The paper-trading bot currently sizes off a $5,000 simulated bankroll and exits
every trade with fixed brackets — half at +2R, half at +3R — so it **structurally
caps every winner at 3R**. Ross Cameron's real edge comes from the occasional
5R–20R runner; the current exit throws that upside away. The user wants a
realistic **$1,000** account and an exit that banks a base hit but lets a runner
run, while keeping the bot's existing learning intact.

The user also confirmed the **entry logic stays strict** — the hard Ross
five-criteria gate is the safer edge; softening it would surface too many
marginal names. News moves from an optional badge to a **required** gate.

## Goals

1. Trade a realistic **$1,000** bankroll (was $5,000).
2. Replace the 2R/3R cap with **scale-out + uncapped trailing runner** so big
   moves are captured.
3. Keep the entry strict (Ross gate) and **require a news catalyst**.
4. Keep the existing entry-ranking learning working; realized R still journaled.

## Non-goals (explicitly deferred)

- Adaptive exit learning (model choosing trail width / exit timing).
- Catalyst-type classification (detecting "new CEO / AI / acquisition" from
  headlines) — the news gate is *presence of a recent Benzinga headline*.
- Exploration sampling to de-bias the learner.

## Design

### 1. Bankroll & sizing (`config.py`)

| Config | Before | After |
|---|---|---|
| `bot_bankroll` | 5000.0 | **1000.0** |
| `bot_risk_pct` | 1.0 | 1.0 (unchanged) |
| `bot_max_notional_pct` | 25.0 | 25.0 (unchanged) |
| `bot_daily_loss_pct` | 3.0 | 3.0 → now **−$30/day** |

Sizing math is unchanged (`strategy.size_position`): risk 1% ($10), capped at
25% notional ($250). The notional cap usually binds, so real risk lands ~$7–10
per trade; up to 4 trades ≈ fully-invested $1,000. No cash overrun even in
realistic terms.

### 2. Entry — strict Ross gate, news required

No change to selection logic. Flip one flag:

| Config | Before | After |
|---|---|---|
| `hod_require_news` | False | **True** |

The bot reads `state.payload(now)["hod"]["qualified"]`, which already applies the
gate via `hod._criteria`. Setting `hod_require_news = True` adds news as a hard
check, so qualified names must have a recent per-symbol Benzinga headline. The
dashboard's "qualified/near" lists shift to match (the UI toggle still lets the
human view without-news). No bot-code change needed for the entry side.

### 3. Exit — bank a base hit, trail the runner

Replace fixed 2R/3R brackets. On entry, split shares in two:

- **Bank half** — a **bracket**: take-profit at **+2R**, stop at **−1R**. OCO, so
  when +2R fills the stop auto-cancels. This is the guaranteed base hit.
- **Runner half** — enters with a **−1R stop and no target** (Alpaca
  `order_class: "oto"` with a `stop_loss` leg). When price first tags **+2R**,
  the bot **cancels that −1R stop and submits a native `trailing_stop` at 5%**
  below the high-water mark. Uncapped — can ride to 5R, 10R, 20R.

**Why native trailing:** the cloud session process stops at 12:15 ET, but a
runner can stay open until the 15:50 flatten. A native Alpaca trailing order
lives on Alpaca's servers and keeps trailing for hours after our process exits.
The 15:50 `flatten` job is the final backstop.

**Edge cases:**
- Position too small to split (< 2 shares): trade as a single +2R bank, no runner.
- Runner first reaches +2R **after** 12:15 (no live process to swap the order):
  it simply keeps its −1R stop and misses the trail. Rare (momentum resolves
  early). Optional mitigation: extend the session `--until-et` to ~13:00.

**Implementation risk to validate first (spike):** confirm Alpaca paper accepts
a **bank bracket + a runner `oto` stop on the same symbol simultaneously** — both
create one aggregated long position with two protective sell orders. Multiple
reducing sell orders on a long are normally fine, but this must be verified
against the paper API before building on it. Fallback if rejected: enter the full
qty as **one** order with a single −1R stop, then at +2R sell `bank_qty` at market
(bank the base hit) and attach the 5% trailing stop to the remaining `runner_qty`
— same behaviour, one entry, protective orders managed after fill.

### 4. Time stop — discipline without capping winners

`bot_time_stop_minutes` stays 20 but becomes **conditional**: it only fires while
a trade **has not yet reached +2R**. A trade that stalls below +2R for 20 min is
cut (feeds the "didn't work" learning signal). Once it becomes a runner
(trailing), the time stop no longer applies — the 5% trail decides.

### 5. Exit config summary (`config.py`)

| Config | Before | After |
|---|---|---|
| `bot_targets_r` | (2.0, 3.0) | **removed** |
| `bot_scale_out_r` | — | **2.0** (new: bank half here) |
| `bot_runner_trail_pct` | — | **5.0** (new: native trail width) |
| `bot_time_stop_minutes` | 20 | 20 (now conditional on pre-scale-out) |

### 6. Broker additions (`trading/broker.py`)

Two new **pure payload builders** (unit-tested, matching existing
`bracket_payload` style) plus thin submit wrappers:

- `oto_stop_payload(symbol, qty, limit_price, stop_price)` →
  `order_class: "oto"`, `stop_loss: {stop_price}`, side buy, tif day.
- `trailing_stop_payload(symbol, qty, trail_percent)` →
  `type: "trailing_stop"`, `trail_percent`, side **sell**, tif day.

`cancel_order`, `positions`, `order`, `close_position` already exist.

### 7. Bot orchestration (`trading/bot.py`)

`_enter` restructure: size → split (`split_qty`) → submit bank bracket + runner
oto-stop → journal `record_trade_open` → track in `open_trades` with
`{bank_order_id, runner_entry_order_id, runner_stop_order_id, bank_qty,
runner_qty, entry, initial_stop, opened_ts, trailing: False}`.

`_manage_open` restructure, per open trade each cycle:
1. **Trailing swap:** if `not trailing` and `price >= entry + scale_out_r*R`:
   cancel runner −1R stop, submit `trailing_stop(runner_qty, trail_pct)`, set
   `trailing = True`.
2. **Time stop:** if `not trailing` and `age >= time_stop`: cancel all orders,
   market-close, record exit `time_stop`.
3. **Closed detection:** when the symbol's position is flat, gather fills from the
   bank leg (its +2R take-profit or −1R stop) and the runner leg (trailing-stop
   or −1R stop), compute a **share-weighted exit** and a **blended R**, and
   `record_trade_close` once.
4. **Flatten (15:50):** cancel all, close, record exit (handled here + by the
   `flatten` job as backstop).

### 8. Journaling & learning (mostly unchanged)

- Alert journaling/labeling and the logistic-regression entry ranker are
  **unchanged** — still journal + rank Ross-qualified alerts on the
  +2R-before-−1R label. This preserves the learning the user cares about.
- Realized R per trade becomes a **share-weighted blend** of the two exits:
  `pnl = bank_qty*(bank_exit-entry) + runner_qty*(runner_exit-entry)`;
  `r = pnl / (total_qty * (entry - initial_stop))`. One trade row per symbol;
  win-rate / expectancy / equity stats keep working with no schema change.

## Testing (TDD — tests first)

- **strategy.py (pure):** `reached_scale_out(entry, price, cfg)`,
  conditional time-stop gate, blended-R helper, updated `exit_levels`
  (stop −1R + scale-out +2R; runner has no fixed target).
- **broker.py (pure):** `oto_stop_payload`, `trailing_stop_payload` JSON shape.
- **bot.py (fake broker):** drive a price path entry → +2R (assert stop cancelled
  + trailing submitted) → trail up → stop-out (assert one blended-R trade close);
  and a stall path (assert time-stop cut before +2R). Reuse existing fake-broker
  test patterns (`tests/test_broker.py`).
- **demo mode:** add a runner scenario to `scanner/demo.py` so a scale-out +
  trailing exit is visible on the Bot panel before it goes near the market.

## Verification / rollout

1. `.venv\Scripts\python -m pytest` — all green.
2. `python -m scanner.main --demo` — watch the Bot panel show a base-hit +
   runner + trailing exit end-to-end.
3. Push to GitHub (redeploy; Pages + workflows already live).
4. Monday's paper session auto-runs; review the dashboard + journal: win rate,
   expectancy in R, and whether any runners exceeded 3R.

## Open questions

None blocking. Deferred items listed under Non-goals; the session-extension
mitigation (§3) can be decided at implementation time.
