# Runner Trailing Exit + $1k Bankroll — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trade a realistic $1,000 paper account and replace the fixed 2R/3R exit with "bank half at +2R, let a runner trail 5% uncapped," while keeping the existing learning and requiring a news catalyst.

**Architecture:** Config-driven changes plus new pure helpers (strategy, broker payloads) and a rewrite of the bot's entry/exit orchestration. Entry is a single market buy protected by a managed stop; at +2R the bot cancels the stop, market-sells the bank half, and attaches a native Alpaca `trailing_stop` to the runner (survives the 12:15 session end; the 15:50 flatten job is the backstop). Realized R is journaled as the share-weighted average of the exit fills — no journal schema change.

**Tech Stack:** Python 3.12, asyncio/aiohttp, pytest (sync tests; async paths driven with `asyncio.run`). Alpaca paper REST. SQLite journal.

## Global Constraints

- **Paper only.** `Broker` must keep raising `PaperOnlyError` unless `trading_base` contains `paper-api`. Never remove that guard.
- **Free tiers only** — no new paid dependencies.
- **Bankroll $1,000**, risk **1%**, notional cap **25%**, daily-loss kill switch **3% (= −$30)**.
- **Scale-out `bot_scale_out_r = 2.0`**, runner trail **`bot_runner_trail_pct = 5.0`%**, stop **`bot_stop_pct = 3.0`% (= 1R)**.
- **News required for the bot** via `state.payload(now, require_news=True)` — do NOT change the global `hod_require_news` default (keeps the dashboard and hod/state tests intact).
- Learning (alert journaling + logistic ranker) stays unchanged; realized R is a share-weighted blend of exit fills.
- Every task ends green: `.venv\Scripts\python -m pytest -q`.

### Deviations from the spec (mechanism-only; behavior identical)

- Spec §2 flips `hod_require_news` globally; this plan scopes it to the bot via the existing `require_news` payload override (smaller blast radius).
- Spec §3 lists the two-order (bank bracket + runner oto) design as primary and single-entry as fallback; this plan makes **single-entry primary** to remove the unverified "concurrent orders on one symbol" dependency. Same observable behavior.

---

### Task 1: Config — $1,000 bankroll and new exit knobs

**Files:**
- Modify: `scanner/config.py` (paper-trading bot block, ~lines 54-73)
- Test: `tests/test_strategy.py` (sizing tests)

**Interfaces:**
- Produces: `Config.bot_bankroll = 1000.0`, `Config.bot_scale_out_r = 2.0`,
  `Config.bot_runner_trail_pct = 5.0`; removes `Config.bot_targets_r`.

- [ ] **Step 1: Update the failing sizing tests first**

In `tests/test_strategy.py`, replace `TestSizing` bodies to expect the $1,000 numbers:

```python
class TestSizing:
    def test_notional_cap_binds_with_default_config(self):
        # risk $10 at a 3% stop implies $666 notional -> capped at 25% of $1k = $250
        qty, stop = size_position(5.0, CFG)
        assert qty == 50                       # 250 / 5.00
        assert stop == pytest.approx(4.85)

    def test_uncapped_when_risk_is_small(self):
        cfg = replace(CFG, bot_risk_pct=0.5)   # risk $5 -> 16 sh, under the $250 cap
        qty, stop = size_position(10.0, cfg)
        assert qty == 16                       # 5 / 0.30
        assert stop == pytest.approx(9.70)

    def test_zero_when_price_exceeds_notional_cap(self):
        cfg = replace(CFG, bot_bankroll=10.0)
        qty, _ = size_position(5.0, cfg)
        assert qty == 0
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `.venv\Scripts\python -m pytest tests/test_strategy.py::TestSizing -q`
Expected: FAIL (asserts 50/16 vs current 250/83).

- [ ] **Step 3: Apply the config change**

In `scanner/config.py`, edit the bot block:

```python
    bot_bankroll: float = 1_000.0        # simulated account (paper balance ignored)
    bot_risk_pct: float = 1.0            # % of bankroll risked per trade
    bot_max_notional_pct: float = 25.0   # position size cap as % of bankroll
    bot_max_trades_per_day: int = 4
    bot_min_price: float = 2.0           # bot trades $2-$20 only (user request)
    bot_max_price: float = 20.0
    bot_stop_pct: float = 3.0            # stop distance from entry; defines R
    bot_scale_out_r: float = 2.0         # bank half here
    bot_runner_trail_pct: float = 5.0    # native trailing-stop width for the runner
    bot_time_stop_minutes: int = 20      # only applies before scale-out
```

Delete the old `bot_targets_r: tuple = (2.0, 3.0)` line.

- [ ] **Step 4: Run the sizing tests (green) and full suite**

Run: `.venv\Scripts\python -m pytest tests/test_strategy.py::TestSizing -q`
Expected: PASS.
Run: `.venv\Scripts\python -m pytest -q`
Expected: `test_strategy.py::TestExits::test_two_and_three_r_levels` and any
`bot`/`broker` reference to `bot_targets_r` FAIL — those are fixed in Tasks 2-4.

- [ ] **Step 5: Commit**

```bash
git add scanner/config.py tests/test_strategy.py
git commit -m "feat(bot): $1000 bankroll + scale-out/trail config knobs"
```

---

### Task 2: strategy.py — scale-out exit levels + weighted-exit helper

**Files:**
- Modify: `scanner/trading/strategy.py:54-60` (`exit_levels`)
- Modify: `scanner/trading/strategy.py` (add `weighted_exit`)
- Test: `tests/test_strategy.py`

**Interfaces:**
- Produces: `exit_levels(entry, cfg) -> {"stop": float, "scale_out": float}`;
  `weighted_exit(legs: list[tuple[float, float]]) -> float | None`
  (legs are `(qty, price)`).
- Consumes: `Config.bot_stop_pct`, `Config.bot_scale_out_r` (Task 1).

- [ ] **Step 1: Write the failing tests**

Replace `TestExits` in `tests/test_strategy.py`:

```python
class TestExits:
    def test_stop_and_scale_out_levels(self):
        levels = exit_levels(10.0, CFG)          # 1R = 0.30
        assert levels["stop"] == pytest.approx(9.70)
        assert levels["scale_out"] == pytest.approx(10.60)   # +2R

    def test_split_qty(self):
        assert split_qty(9) == (5, 4)
        assert split_qty(250) == (125, 125)
        assert split_qty(1) == (1, 0)


class TestWeightedExit:
    def test_share_weighted_average(self):
        assert weighted_exit([(25, 5.30), (25, 5.60)]) == pytest.approx(5.45)

    def test_none_when_no_shares(self):
        assert weighted_exit([]) is None
```

Add `weighted_exit` to the import at the top of the file:

```python
from scanner.trading.strategy import (exit_levels, in_window, should_enter,
                                      size_position, split_qty, weighted_exit)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_strategy.py::TestExits tests/test_strategy.py::TestWeightedExit -q`
Expected: FAIL (`scale_out` KeyError; `weighted_exit` ImportError).

- [ ] **Step 3: Implement**

In `scanner/trading/strategy.py`, replace `exit_levels` and add `weighted_exit`:

```python
def exit_levels(entry_price, cfg: Config):
    """Stop at -1R; scale-out (bank half) at +scale_out_r R."""
    r = entry_price * cfg.bot_stop_pct / 100
    return {
        "stop": entry_price - r,
        "scale_out": entry_price + cfg.bot_scale_out_r * r,
    }


def weighted_exit(legs):
    """Share-weighted average exit price. legs: list of (qty, price)."""
    total_qty = sum(qty for qty, _ in legs)
    if not total_qty:
        return None
    return sum(qty * price for qty, price in legs) / total_qty
```

- [ ] **Step 4: Run tests (green)**

Run: `.venv\Scripts\python -m pytest tests/test_strategy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scanner/trading/strategy.py tests/test_strategy.py
git commit -m "feat(bot): scale-out exit levels + weighted-exit helper"
```

---

### Task 3: broker.py — market / stop / trailing-stop order builders

**Files:**
- Modify: `scanner/trading/broker.py` (add payload builders + submit wrappers)
- Test: `tests/test_broker.py`

**Interfaces:**
- Produces (pure builders): `market_payload(symbol, qty, side)`,
  `stop_payload(symbol, qty, stop_price, side="sell")`,
  `trailing_stop_payload(symbol, qty, trail_percent, side="sell")`.
- Produces (async): `submit_market_buy(symbol, qty)`,
  `submit_market_sell(symbol, qty)`,
  `submit_stop(symbol, qty, stop_price)`,
  `submit_trailing_stop(symbol, qty, trail_percent)`.

- [ ] **Step 1: Write the failing payload tests**

Append to `tests/test_broker.py`:

```python
def test_market_payload():
    broker = Broker(session=None, cfg=CFG)
    assert broker.market_payload("HODX", qty=50, side="buy") == {
        "symbol": "HODX", "qty": "50", "side": "buy",
        "type": "market", "time_in_force": "day",
    }


def test_stop_payload():
    broker = Broker(session=None, cfg=CFG)
    assert broker.stop_payload("HODX", qty=25, stop_price=4.85) == {
        "symbol": "HODX", "qty": "25", "side": "sell",
        "type": "stop", "time_in_force": "day", "stop_price": "4.85",
    }


def test_trailing_stop_payload():
    broker = Broker(session=None, cfg=CFG)
    assert broker.trailing_stop_payload("HODX", qty=25, trail_percent=5.0) == {
        "symbol": "HODX", "qty": "25", "side": "sell",
        "type": "trailing_stop", "time_in_force": "day", "trail_percent": "5",
    }
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_broker.py -q`
Expected: FAIL (`market_payload` not defined).

- [ ] **Step 3: Implement builders + submit wrappers**

In `scanner/trading/broker.py`, add after `bracket_payload`:

```python
    @staticmethod
    def market_payload(symbol, qty, side):
        return {"symbol": symbol, "qty": str(qty), "side": side,
                "type": "market", "time_in_force": "day"}

    @staticmethod
    def stop_payload(symbol, qty, stop_price, side="sell"):
        return {"symbol": symbol, "qty": str(qty), "side": side,
                "type": "stop", "time_in_force": "day",
                "stop_price": f"{stop_price:.2f}"}

    @staticmethod
    def trailing_stop_payload(symbol, qty, trail_percent, side="sell"):
        return {"symbol": symbol, "qty": str(qty), "side": side,
                "type": "trailing_stop", "time_in_force": "day",
                "trail_percent": f"{trail_percent:g}"}
```

And add these submit wrappers next to `submit_bracket`:

```python
    async def submit_market_buy(self, symbol, qty):
        return await self._request("POST", "/v2/orders",
                                   json=self.market_payload(symbol, qty, "buy"))

    async def submit_market_sell(self, symbol, qty):
        return await self._request("POST", "/v2/orders",
                                   json=self.market_payload(symbol, qty, "sell"))

    async def submit_stop(self, symbol, qty, stop_price):
        return await self._request("POST", "/v2/orders",
                                   json=self.stop_payload(symbol, qty, stop_price))

    async def submit_trailing_stop(self, symbol, qty, trail_percent):
        return await self._request(
            "POST", "/v2/orders",
            json=self.trailing_stop_payload(symbol, qty, trail_percent))
```

- [ ] **Step 4: Run tests (green)**

Run: `.venv\Scripts\python -m pytest tests/test_broker.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scanner/trading/broker.py tests/test_broker.py
git commit -m "feat(broker): market/stop/trailing-stop order builders"
```

---

### Task 4: bot.py — news-required candidates + single-entry `_enter`

**Files:**
- Modify: `scanner/trading/bot.py` (imports, `cycle` payload call, `_enter`)
- Create: `tests/test_bot_trading.py` (async orchestration tests + fakes)

**Interfaces:**
- Consumes: `exit_levels`, `split_qty` (strategy); `submit_market_buy`,
  `submit_stop` (broker); `Journal.record_trade_open`.
- Produces: `TradingBot.open_trades[symbol]` dict with keys
  `trade_id, stop_order_id, trailing_order_id, qty, bank_qty, runner_qty,
  entry, stop, scale_out, opened_ts, banked` — consumed by Task 5.

- [ ] **Step 1: Write the failing tests (with shared fakes)**

Create `tests/test_bot_trading.py`:

```python
"""Async orchestration tests for the trading bot (fake broker, real journal)."""
import asyncio
import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.trading.bot import TradingBot
from scanner.trading.journal import Journal

ET = ZoneInfo("America/New_York")
CFG = Config()


def et(hour, minute):
    return dt.datetime(2026, 7, 14, hour, minute, tzinfo=ET)


class FakeBroker:
    def __init__(self):
        self.orders = []          # submitted payload-ish dicts (with id)
        self.cancelled = []
        self._positions = []      # list of {"symbol","current_price"}
        self.closed_orders = []   # returned for the closed-orders query
        self._id = 0

    def _new(self, **kw):
        self._id += 1
        kw["id"] = f"o{self._id}"
        self.orders.append(kw)
        return {"id": kw["id"]}

    async def account(self):
        return {"equity": "100000"}

    async def positions(self):
        return list(self._positions)

    async def submit_market_buy(self, symbol, qty):
        return self._new(side="buy", type="market", symbol=symbol, qty=qty)

    async def submit_market_sell(self, symbol, qty):
        return self._new(side="sell", type="market", symbol=symbol, qty=qty)

    async def submit_stop(self, symbol, qty, stop_price):
        return self._new(side="sell", type="stop", symbol=symbol, qty=qty,
                         stop_price=stop_price)

    async def submit_trailing_stop(self, symbol, qty, trail_percent):
        return self._new(side="sell", type="trailing_stop", symbol=symbol,
                         qty=qty, trail_percent=trail_percent)

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    async def close_position(self, symbol):
        self._positions = [p for p in self._positions if p["symbol"] != symbol]

    async def portfolio_history(self, *a, **k):
        return {"timestamp": [], "equity": []}

    async def _request(self, method, path, params=None, json=None):
        if params and params.get("status") == "closed":
            return self.closed_orders
        return None


class FakeState:
    def __init__(self, latest):
        self.latest = latest


def make_bot(tmp_path, **cfg_overrides):
    cfg = replace(CFG, **cfg_overrides) if cfg_overrides else CFG
    journal = Journal(str(tmp_path / "j.db"))
    broker = FakeBroker()
    return TradingBot(cfg, journal, broker), broker, journal


def a_pick(price=5.0, qty=50):
    return {"symbol": "HODX", "price": price, "qty": qty, "stop": 4.85,
            "score": 0.8, "features": {"rvol": 8.0}}


def test_enter_places_buy_and_full_size_stop(tmp_path):
    bot, broker, journal = make_bot(tmp_path)
    asyncio.run(bot._enter(a_pick(price=5.0, qty=50), ts=1_700_000_000))

    kinds = {(o["side"], o["type"]) for o in broker.orders}
    assert ("buy", "market") in kinds
    assert ("sell", "stop") in kinds
    stop = next(o for o in broker.orders if o["type"] == "stop")
    assert stop["qty"] == 50 and stop["stop_price"] == pytest.approx(4.85)

    trade = bot.open_trades["HODX"]
    assert trade["bank_qty"] + trade["runner_qty"] == 50
    assert trade["scale_out"] == pytest.approx(5.30)   # 5.00 + 2 * 0.15
    assert trade["banked"] is False
    assert journal.trades_today("2023-11-14")  # record_trade_open persisted
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_bot_trading.py -q`
Expected: FAIL (`_enter` still builds brackets / uses `targets`).

- [ ] **Step 3: Rewrite imports, `cycle` payload call, and `_enter`**

In `scanner/trading/bot.py`, update the strategy import to drop `exit_levels`'
old target usage and keep what we need:

```python
from .model import train, scorer_from_weights
from .strategy import (ET, exit_levels, should_enter, size_position,
                       split_qty, weighted_exit, _parse_hhmm)
```

In `cycle`, require news for the bot's candidate list:

```python
        payload = state.payload(now, require_news=True)
        qualified = payload["hod"]["qualified"]
```

Replace `_enter` entirely:

```python
    async def _enter(self, pick, ts):
        entry = pick["price"]
        levels = exit_levels(entry, self.cfg)
        total_qty = pick["qty"]
        bank_qty, runner_qty = split_qty(total_qty)

        await self.broker.submit_market_buy(pick["symbol"], total_qty)
        stop = await self.broker.submit_stop(
            pick["symbol"], total_qty, levels["stop"])

        trade_id = self.journal.record_trade_open(
            ts, pick["symbol"], qty=total_qty, entry=entry,
            stop=levels["stop"], targets=[levels["scale_out"]],
            features=pick["features"])
        self.open_trades[pick["symbol"]] = {
            "trade_id": trade_id, "stop_order_id": stop["id"],
            "trailing_order_id": None, "qty": total_qty,
            "bank_qty": bank_qty, "runner_qty": runner_qty,
            "entry": entry, "stop": levels["stop"],
            "scale_out": levels["scale_out"], "opened_ts": ts,
            "banked": False}
        print(f"[bot] ENTER {pick['symbol']} x{total_qty} @~{entry:.2f} "
              f"stop {levels['stop']:.2f} scale-out {levels['scale_out']:.2f}")
```

- [ ] **Step 4: Run tests (green)**

Run: `.venv\Scripts\python -m pytest tests/test_bot_trading.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scanner/trading/bot.py tests/test_bot_trading.py
git commit -m "feat(bot): news-required candidates + single-entry with managed stop"
```

---

### Task 5: bot.py — scale-out → trailing, conditional time-stop, blended close

**Files:**
- Modify: `scanner/trading/bot.py` (`_manage_open`, `_closed_exit_price`, add `_flatten_trade`; delete the old bracket-leg exit reader)
- Test: `tests/test_bot_trading.py`

**Interfaces:**
- Consumes: `open_trades[symbol]` from Task 4; `submit_market_sell`,
  `submit_trailing_stop`, `cancel_order`, `close_position`, `_request`
  (broker); `Journal.record_trade_close`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bot_trading.py`:

```python
def _open_a_trade(bot, ts, price=5.0, qty=50):
    asyncio.run(bot._enter(a_pick(price=price, qty=qty), ts=ts))
    return bot.open_trades["HODX"]


def test_scale_out_banks_half_and_starts_trailing(tmp_path):
    bot, broker, _ = make_bot(tmp_path)
    trade = _open_a_trade(bot, ts=int(et(10, 0).timestamp()))
    broker._positions = [{"symbol": "HODX", "current_price": 5.30}]
    state = FakeState({"HODX": {"price": 5.30}})            # at +2R

    asyncio.run(bot._manage_open(state, now=et(10, 5),
                                 ts=int(et(10, 5).timestamp())))

    assert trade["stop_order_id"] in broker.cancelled       # -1R stop pulled
    sells = [o for o in broker.orders if o["type"] == "market" and o["side"] == "sell"]
    assert sells and sells[0]["qty"] == trade["bank_qty"]    # banked half
    trail = [o for o in broker.orders if o["type"] == "trailing_stop"]
    assert trail and trail[0]["qty"] == trade["runner_qty"]
    assert trade["banked"] is True


def test_time_stop_cuts_a_stalled_trade_before_scale_out(tmp_path):
    bot, broker, journal = make_bot(tmp_path)
    open_ts = int(et(10, 0).timestamp())
    _open_a_trade(bot, ts=open_ts)
    broker._positions = [{"symbol": "HODX", "current_price": 5.05}]  # below +2R
    state = FakeState({"HODX": {"price": 5.05}})
    late = et(10, 21)                                        # 21 min later

    asyncio.run(bot._manage_open(state, now=late, ts=int(late.timestamp())))

    assert "HODX" not in bot.open_trades
    closed = journal.recent_trades(1)[0]
    assert closed["exit_reason"] == "time_stop"


def test_runner_stays_open_past_time_stop(tmp_path):
    bot, broker, _ = make_bot(tmp_path)
    open_ts = int(et(10, 0).timestamp())
    trade = _open_a_trade(bot, ts=open_ts)
    trade["banked"] = True                                  # already a runner
    broker._positions = [{"symbol": "HODX", "current_price": 6.0}]
    state = FakeState({"HODX": {"price": 6.0}})
    late = et(10, 40)

    asyncio.run(bot._manage_open(state, now=late, ts=int(late.timestamp())))

    assert "HODX" in bot.open_trades                        # not time-stopped


def test_close_records_blended_r_from_sell_fills(tmp_path):
    bot, broker, journal = make_bot(tmp_path)
    _open_a_trade(bot, ts=int(et(10, 0).timestamp()))
    bot.open_trades["HODX"]["banked"] = True
    broker._positions = []                                  # fully closed
    broker.closed_orders = [
        {"side": "sell", "filled_qty": "25", "filled_avg_price": "5.30", "legs": []},
        {"side": "sell", "filled_qty": "25", "filled_avg_price": "5.60", "legs": []},
    ]
    state = FakeState({})

    asyncio.run(bot._manage_open(state, now=et(11, 0),
                                 ts=int(et(11, 0).timestamp())))

    trade = journal.recent_trades(1)[0]
    assert trade["exit_price"] == pytest.approx(5.45)       # weighted average
    assert trade["r_multiple"] == pytest.approx(3.0)        # (5.45-5.0)/0.15
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_bot_trading.py -q`
Expected: FAIL (`_manage_open` still uses bracket logic).

- [ ] **Step 3: Rewrite `_manage_open`, `_closed_exit_price`; add `_flatten_trade`**

In `scanner/trading/bot.py`, replace `_manage_open` and `_closed_exit_price`
with:

```python
    async def _manage_open(self, state, now, ts):
        if not self.open_trades:
            return
        positions = {p["symbol"]: p for p in await self.broker.positions()}
        flatten = _past(now, self.cfg.bot_flatten_time)
        for symbol, trade in list(self.open_trades.items()):
            pos = positions.get(symbol)
            if pos is None:
                exit_price = await self._closed_exit_price(symbol, trade)
                reason = "trailing" if trade["banked"] else "stop"
                self.journal.record_trade_close(trade["trade_id"], ts,
                                                exit_price, reason)
                del self.open_trades[symbol]
                print(f"[bot] CLOSED {symbol} @~{exit_price:.2f} ({reason})")
                continue

            latest = state.latest.get(symbol)
            price = (latest["price"] if latest
                     else float(pos.get("current_price") or trade["entry"]))

            if flatten:
                await self._flatten_trade(symbol, trade, ts, pos, "flatten")
                continue

            if not trade["banked"] and price >= trade["scale_out"]:
                await self.broker.cancel_order(trade["stop_order_id"])
                if trade["runner_qty"] >= 1:
                    await self.broker.submit_market_sell(symbol, trade["bank_qty"])
                    tr = await self.broker.submit_trailing_stop(
                        symbol, trade["runner_qty"], self.cfg.bot_runner_trail_pct)
                    trade["trailing_order_id"] = tr["id"]
                else:
                    await self.broker.submit_market_sell(symbol, trade["qty"])
                trade["banked"] = True
                print(f"[bot] SCALE-OUT {symbol}: banked {trade['bank_qty']} "
                      f"@~{price:.2f}, runner {trade['runner_qty']} trailing "
                      f"{self.cfg.bot_runner_trail_pct:g}%")
                continue

            age_min = (ts - trade["opened_ts"]) / 60
            if not trade["banked"] and age_min >= self.cfg.bot_time_stop_minutes:
                await self._flatten_trade(symbol, trade, ts, pos, "time_stop")

    async def _flatten_trade(self, symbol, trade, ts, pos, reason):
        for order_id in (trade["stop_order_id"], trade["trailing_order_id"]):
            if order_id:
                try:
                    await self.broker.cancel_order(order_id)
                except aiohttp.ClientResponseError:
                    pass
        await self.broker.close_position(symbol)
        fallback = float(pos.get("current_price") or trade["entry"])
        exit_price = await self._closed_exit_price(symbol, trade, fallback)
        self.journal.record_trade_close(trade["trade_id"], ts, exit_price, reason)
        del self.open_trades[symbol]
        print(f"[bot] CLOSED {symbol} @~{exit_price:.2f} ({reason})")

    async def _closed_exit_price(self, symbol, trade, fallback=None):
        """Share-weighted average of all closed sell fills for the symbol."""
        orders = await self.broker._request(
            "GET", "/v2/orders",
            params={"status": "closed", "symbols": symbol,
                    "limit": 50, "nested": "true"})
        legs = []
        for order in orders or []:
            for leg in [order] + (order.get("legs") or []):
                if leg.get("side") == "sell" and leg.get("filled_avg_price"):
                    legs.append((float(leg.get("filled_qty") or 0),
                                 float(leg["filled_avg_price"])))
        avg = weighted_exit(legs)
        if avg is not None:
            return avg
        return fallback if fallback is not None else trade["entry"]
```

- [ ] **Step 4: Run the trading tests, then the full suite**

Run: `.venv\Scripts\python -m pytest tests/test_bot_trading.py -q`
Expected: PASS.
Run: `.venv\Scripts\python -m pytest -q`
Expected: PASS (all green — confirms nothing else references the old exit path).

- [ ] **Step 5: Commit**

```bash
git add scanner/trading/bot.py tests/test_bot_trading.py
git commit -m "feat(bot): scale-out to trailing runner, conditional time-stop, blended close"
```

---

### Task 6: Demo runner scenario + end-to-end verification

**Files:**
- Modify: `scanner/demo.py` (make a demo symbol climb past +2R so the Bot panel shows a scale-out + runner)
- Verify: dashboard Bot panel, full suite

**Interfaces:**
- Consumes: the finished bot from Tasks 1-5. No new public interface.

- [ ] **Step 1: Inspect the demo generator**

Run: `.venv\Scripts\python -c "import scanner.demo as d; print([n for n in dir(d) if not n.startswith('__')])"`
Read `scanner/demo.py` to find where synthetic prices are produced.

- [ ] **Step 2: Ensure one demo symbol runs to ~+8% (past +2R = +6%)**

Adjust the demo price path so at least one qualified symbol (with a news badge,
since the bot now requires news) rises ~8% intraday, so the bot banks at +2R and
starts a trailing runner. Keep the existing demo API; only tune the price/news
curve. (If `scanner/demo.py` already produces a >6% mover with news, no code
change — note that and continue.)

- [ ] **Step 3: Run the full test suite**

Run: `.venv\Scripts\python -m pytest -q`
Expected: PASS (including `tests/test_demo.py`).

- [ ] **Step 4: Manually drive the demo and watch the Bot panel**

Run: `.venv\Scripts\python -m scanner.main --demo --bot`
(If `--demo` doesn't accept `--bot`, run `python -m scanner.main --demo` and
confirm the Bot panel renders; the demo bot path is exercised by tests.)
Open http://127.0.0.1:8124 → **Paper Trading Bot** panel. Confirm you see an
ENTER, then a **SCALE-OUT** log line and a runner trade, and the Learning list /
equity render without errors. Stop with Ctrl-C.

- [ ] **Step 5: Commit**

```bash
git add scanner/demo.py
git commit -m "test(bot): demo scenario exercises scale-out + trailing runner"
```

---

### Task 7: Merge + redeploy + cloud verification

**Files:** none (release task).

- [ ] **Step 1: Final full suite on the branch**

Run: `.venv\Scripts\python -m pytest -q`
Expected: PASS (all).

- [ ] **Step 2: Merge the feature branch to main**

```bash
git checkout main
git merge --no-ff runner-trailing-exit -m "feat: $1k bankroll + scale-out/trailing-runner exit"
```

- [ ] **Step 3: Push (redeploy)**

```bash
git push origin main
```

Confirm on GitHub that Actions is green and Pages redeploys.

- [ ] **Step 4: Cloud smoke — validate live order types during market hours**

This is the only check that exercises real Alpaca order acceptance for the new
`stop` / `trailing_stop` / market-sell flow (local tests use a fake broker).
On the next market open (or via **Actions → trading-session → Run workflow**
during 9:35-11:30 ET), watch the run logs for an `ENTER` and, on a mover, a
`SCALE-OUT` line with no Alpaca 4xx errors. Then check the **journal / Bot panel**
for a recorded trade with a sane R multiple. If Alpaca rejects an order type,
capture the error from the logs before iterating.

- [ ] **Step 5: Update project memory**

Note in memory (`daytrade-scanner-project.md`) that the exit is now scale-out +
trailing runner on a $1,000 bankroll with news required, deployed on `main`.

---

## Self-Review

**Spec coverage:**
- $1,000 bankroll + sizing → Task 1. ✓
- Entry unchanged + news required → Task 4 (`require_news=True`). ✓
- Scale-out half at +2R → Tasks 2 (levels) + 5 (bank market-sell). ✓
- Runner trails 5% native, survives 12:15 → Tasks 3 (builder) + 5 (attach trailing); flatten.py already reconciles late closes (unchanged). ✓
- Time stop only before +2R → Task 5. ✓
- Learning unchanged; blended realized R → Task 2 (`weighted_exit`) + 5 (passed to existing `record_trade_close`, no schema change). ✓
- Non-goals (adaptive exits, catalyst classification, exploration) → not implemented. ✓

**Placeholder scan:** no TBD/TODO; every code step shows complete code; every run step shows the command + expected result.

**Type consistency:** `exit_levels` returns `{"stop","scale_out"}` (Task 2) and is consumed with those keys in Task 4/5. `open_trades[symbol]` keys defined in Task 4 are exactly those read in Task 5. Broker method names (`submit_market_buy/sell`, `submit_stop`, `submit_trailing_stop`) match between Tasks 3 and 4/5. `weighted_exit(legs)` signature consistent between Tasks 2 and 5.

**Known approximation (documented):** during a live session, `_flatten_trade`'s exit price may fall back to `current_price` if the liquidation fill isn't yet visible in closed orders; the `flatten` job's reconcile corrects the journal for any trade that closes after the session ends. Acceptable per spec §8.
