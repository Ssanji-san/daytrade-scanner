"""Replay historical sessions through the live scanner pipeline.

Live sessions produce a few graded setups a day, so every strategy question
takes weeks to answer. The history is free - Alpaca's Basic plan serves bars
and news back to 2016 - so replaying it produces the same kind of data far
faster.

Two rules keep this honest:

* **Point in time.** Nothing timestamped after the simulated moment may reach
  the state. A backtest that peeks looks brilliant and loses money live.
* **Same pipeline.** Bars are pushed through `MarketState.ingest`, exactly
  the entry point the live loop uses, so the replay cannot quietly diverge
  from what the bot really does.

Results are graded on *alerts*, not trades - "did this setup reach the target
the bot trades for before its stop", which is what the model already learns
from. That means no fills to simulate, and none of the slippage fiction that
makes most backtests useless. `--trades` additionally runs the real entry and
exit path through scanner.backtest.simulate for a P&L figure.
"""
