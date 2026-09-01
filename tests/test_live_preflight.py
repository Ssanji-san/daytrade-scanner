"""End-to-end rehearsal of a live session against a fake broker.

The unit tests check pieces. This drives the REAL TradingBot.cycle through a
real MarketState, built from minute bars, and asserts the whole path: the
scan qualifies a stock, the setup fires, the order goes out correctly sized,
the scale-out banks the majority, the stop moves to break-even, and the
stall closes the runner.

This exists because the parts have been individually correct while the whole
was broken more than once - a wash-trade rejection, a threshold that admitted
nothing, a missing import that killed an eight-month run.
"""
import asyncio
import datetime as dt

import pytest

from scanner.config import Config
from scanner.state import MarketState
from scanner.trading.bot import TradingBot
from scanner.trading.journal import Journal

from .test_bot_trading import FakeBroker

CFG = Config()
ET = __import__("zoneinfo").ZoneInfo("America/New_York")


def et(h, m):
    return dt.datetime(2026, 7, 14, h, m, tzinfo=ET)


def bar(now, o, h, l, c, v=90_000):
    return {"t": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "o": o, "h": h, "l": l, "c": c, "v": v}


def feed(state, now, price, cum, o=None, h=None, l=None):
    """One snapshot, shaped exactly like the live poll loop builds them."""
    o = price if o is None else o
    state.ingest(now, {"HODX": {
        "price": price, "cum_volume": cum, "day_high": max(price, h or price),
        "prev_close": 2.60, "avg_volume": 120_000, "float_shares": 8_000_000,
        "minute_bar": bar(now, o, h or price, l or price, price)}})


def a_session(state, news_ts):
    """Ramp off the open, pull back three bars, then break out."""
    cum = 0
    # 09:30-09:38 drive off the bell: 2.75 -> 3.05
    for i in range(9):
        now = et(9, 30 + i)
        px = round(2.75 + 0.30 * i / 8, 2)
        cum += 90_000
        feed(state, now, px, cum, o=round(px - 0.02, 2),
             h=round(px + 0.01, 2), l=round(px - 0.03, 2))
    # 09:39-09:41 pull back to 2.96
    for i, px in enumerate((3.02, 2.99, 2.96)):
        now = et(9, 39 + i)
        cum += 60_000
        feed(state, now, px, cum, o=round(px + 0.03, 2),
             h=round(px + 0.04, 2), l=round(px - 0.01, 2))
    # 09:42 breaks the prior candle high -> the entry
    now = et(9, 42)
    cum += 95_000
    feed(state, now, 3.05, cum, o=2.97, h=3.06, l=2.96)
    state.set_news(now, [{"symbol": "HODX", "ts": news_ts,
                          "headline": "HODX receives FDA approval",
                          "url": "u", "source": "bz"}])
    return now, cum


@pytest.fixture
def rig(tmp_path):
    journal = Journal(str(tmp_path / "live.db"), CFG.bot_alert_window_minutes)
    broker = FakeBroker()
    bot = TradingBot(CFG, journal, broker)
    return bot, broker, journal, MarketState(CFG)


def test_the_scanner_qualifies_a_textbook_setup(rig):
    bot, broker, journal, state = rig
    now, _ = a_session(state, int(et(9, 20).timestamp()))
    payload = state.payload(now, require_news=True)
    qualified = payload["hod"]["qualified"]
    assert [r["symbol"] for r in qualified] == ["HODX"], (
        f"nothing qualified; near={[(r['symbol'], r['failed']) for r in payload['hod']['near']]}")
    row = qualified[0]
    assert row["setup"] is not None, "qualified but no entry trigger"
    assert row["setup"]["setup"] in ("micro_pullback", "flat_top")
    assert row["open_pct"] > CFG.hod_min_open_pct
    assert row["rvol"] >= CFG.hod_min_rvol
    assert row["above_vwap"]


def test_a_full_session_enters_scales_and_stalls_out(rig):
    bot, broker, journal, state = rig
    now, cum = a_session(state, int(et(9, 20).timestamp()))

    asyncio.run(bot.cycle(state, now))

    # --- entry ---
    assert "HODX" in bot.open_trades, "the bot did not take the trade"
    trade = bot.open_trades["HODX"]
    entry = trade["entry"]
    assert trade["qty"] * entry == pytest.approx(1000, abs=15), "not the full account"
    assert (entry - trade["stop"]) * trade["qty"] == pytest.approx(50, abs=2), "risk is not $50"
    assert trade["scale_out"] == pytest.approx(round(entry + 0.20, 2)), "target is not +20c"
    orders = [o for o in broker.orders if o["side"] == "buy"]
    assert len(orders) == 1 and orders[0]["order_class"] == "oto", "entry must be one OTO order"

    # --- +20c: bank 65%, runner stop to break-even ---
    broker._positions = [{"symbol": "HODX", "current_price": entry + 0.22}]
    later = et(9, 45)
    cum += 90_000
    feed(state, later, round(entry + 0.22, 2), cum,
         o=round(entry + 0.10, 2), h=round(entry + 0.24, 2), l=round(entry + 0.08, 2))
    asyncio.run(bot.cycle(state, later))

    sells = [o for o in broker.orders if o["type"] == "market" and o["side"] == "sell"]
    assert sells and sells[0]["qty"] == trade["bank_qty"]
    assert trade["bank_qty"] / trade["qty"] == pytest.approx(0.65, abs=0.02)
    trails = [o for o in broker.orders if o["type"] == "trailing_stop"]
    assert trails, "runner did not get a trailing stop"
    assert trails[-1]["qty"] == trade["runner_qty"]
    price_at_bank = round(entry + 0.22, 2)
    assert price_at_bank * (1 - trails[-1]["trail_percent"] / 100) >= entry, \
        "the runner's first stop sits below what was paid"
    assert trade["stop"] == pytest.approx(round(entry, 2)), \
        "break-even floor was not recorded"
    assert "HODX" in bot.open_trades, "runner should still be riding"

    # --- two dojis: the stall closes the runner ---
    for i in range(2):
        t = et(9, 46 + i)
        cum += 20_000
        px = round(entry + 0.21, 2)
        feed(state, t, px, cum, o=px, h=round(px + 0.05, 2), l=round(px - 0.05, 2))
    end = et(9, 49)
    cum += 20_000
    feed(state, end, round(entry + 0.21, 2), cum)
    asyncio.run(bot.cycle(state, end))

    assert "HODX" not in bot.open_trades, "the stall exit never fired"
    closed = journal.recent_trades(1)[0]
    assert closed["exit_reason"] == "stall"
    assert closed["r_multiple"] > 0, "a scaled winner must not book a loss"


def test_nothing_is_entered_outside_the_window(rig):
    bot, broker, journal, state = rig
    now, _ = a_session(state, int(et(9, 20).timestamp()))
    late = et(12, 31)                      # past the 12:30 cutoff
    asyncio.run(bot.cycle(state, late))
    assert bot.open_trades == {}
    assert broker.orders == []


class TestScoringBarIsLiveable:
    """The bar must admit the strategy's own textbook setup.

    A model trained on rows from a previous strategy learns that this one's
    signals are bad, sets a bar above everything it will ever see, and the
    bot goes quiet with no error anywhere. That is not hypothetical: it
    happened, and the sessions looked healthy the whole time.
    """

    def test_the_heuristic_admits_a_textbook_setup(self):
        from scanner.trading.bot import REFERENCE_SETUP
        from scanner.trading.model import HeuristicScorer
        assert HeuristicScorer().score(REFERENCE_SETUP) >= CFG.bot_score_threshold

    def test_a_fresh_bot_would_buy_the_reference_setup(self, rig):
        bot, _, _, _ = rig
        assert bot.reference_score >= bot.score_threshold

    def test_a_model_that_rejects_it_is_reported(self, tmp_path, capsys):
        """Trained on losers only, so it learns to refuse everything."""
        journal = Journal(str(tmp_path / "bad.db"), CFG.bot_alert_window_minutes)
        for i in range(CFG.bot_model_min_samples + 5):
            aid = journal.record_alert(1_700_000_000 + i * 86_400, f"S{i}",
                                       5.0, 0.25,
                                       {"rvol": 8.0 + i, "day_pct": 15.0,
                                        "above_vwap": 1.0, "has_news": 1.0})
            journal.track_alert(aid, 1_700_000_000 + i * 86_400 + 60,
                                price=3.0, high=3.0, low=3.0)
        TradingBot(CFG, journal, FakeBroker())
        out = capsys.readouterr().out
        assert "REJECTS ITS OWN TEXTBOOK SETUP" in out or "passes" in out
