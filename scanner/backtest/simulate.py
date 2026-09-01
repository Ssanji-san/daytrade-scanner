"""Simulate the trades the bot would have taken, not the alerts it saw.

The alert journal grades "did this symbol reach the target from the minute it
was first spotted". That is not a trade: the bot enters later, at the pullback
trigger, at a different price, and only for as many positions as the account
holds. Measuring one and reporting the other has been the quiet gap in every
number this backtest has produced.

This steps the same session bar by bar and runs the real entry path -
`choose_entries`, so the setup requirement, the risk band, position sizing
and every cap apply exactly as they do live - then manages the resulting
positions against real highs and lows.

Where a bar could have hit both the stop and the target, the stop is assumed
first. Intrabar order is unknowable from OHLC, and guessing in our favour is
how backtests come to promise what they cannot pay.

Spread and slippage are NOT modelled. On these prices they are the same size
as the edge, so treat every number here as an upper bound.
"""
from ..config import Config
from ..history import ET
from ..trading.bot import choose_entries
from ..trading.strategy import (exit_levels, is_doji, runner_trail_pct,
                                bank_split, split_qty, weighted_exit)


def _hhmm(text):
    hour, minute = text.split(":")
    return int(hour), int(minute)


def _at_or_past(now, hhmm):
    et = now.astimezone(ET)
    return (et.hour, et.minute) >= _hhmm(hhmm)


def _in_entry_window(now, cfg: Config):
    et = now.astimezone(ET)
    return (_hhmm(cfg.bot_window_open) <= (et.hour, et.minute)
            <= _hhmm(cfg.bot_window_close))


class Position:
    """One simulated open trade, with the same two legs the live bot uses."""

    def __init__(self, trade_id, pick, levels, ts, cfg=None):
        self.trade_id = trade_id
        self.symbol = pick["symbol"]
        self.entry = pick["price"]
        self.stop = levels["stop"]
        self.scale_out = levels["scale_out"]
        self.qty = pick["qty"]
        if cfg is not None and cfg.bot_runner_mode:
            self.bank_qty, self.runner_qty = bank_split(pick["qty"], cfg)
        else:
            self.bank_qty, self.runner_qty = split_qty(pick["qty"])
        self.opened_ts = ts
        self.banked = False
        self.trail_high = pick["price"]
        self.trail_pct = None     # set when the runner starts riding
        self.dojis = 0            # consecutive stalled bars
        self._risk = self.entry - levels["stop"]
        self.legs = []            # (qty, price) as each leg closes

    #: risk is fixed at entry - it must not follow the stop to break-even,
    #: or a scaled-out trade would report an infinite R multiple.
    @property
    def risk(self):
        return self._risk

    def close(self, qty, price):
        self.legs.append((qty, price))

    def exit_price(self):
        return weighted_exit(self.legs) or self.entry

    def r_multiple(self):
        return ((self.exit_price() - self.entry) / self.risk
                if self.risk else 0.0)


class Simulator:
    """Runs one session's trading and writes the trades to the journal."""

    def __init__(self, cfg: Config, journal, day, scorer, score_bar=0.0):
        self.cfg = cfg
        self.journal = journal
        self.day = day
        self.scorer = scorer
        self.score_bar = score_bar
        self.open = {}            # symbol -> Position
        self.traded = set()       # one entry per symbol per session
        self.closed = 0
        self.losses = 0

    # ------------------------------------------------------------ exits

    def _finish(self, pos, ts, reason):
        self.journal.record_trade_close(pos.trade_id, ts, pos.exit_price(),
                                        reason)
        del self.open[pos.symbol]
        self.closed += 1
        if pos.r_multiple() < 0:
            self.losses += 1

    def manage(self, now, ts, symbol_bars):
        flatten = _at_or_past(now, self.cfg.bot_flatten_time)
        for symbol, pos in list(self.open.items()):
            bar = symbol_bars.get(symbol)
            if bar is None:
                if flatten:            # no print to close on; use the entry
                    pos.close(pos.qty - sum(q for q, _ in pos.legs), pos.entry)
                    self._finish(pos, ts, "flatten")
                continue
            high, low, close = bar.get("h"), bar.get("l"), bar["c"]
            high = max(high or close, close)
            low = min(low or close, close)

            if flatten:
                pos.close(pos.qty - sum(q for q, _ in pos.legs), close)
                self._finish(pos, ts, "flatten")
                continue

            if self.cfg.bot_runner_mode:
                self._scalp(pos, ts, bar, high, low, close)
                continue

            if not pos.banked:
                # Stop before target: a bar spanning both is assumed to have
                # taken the stop first.
                if low <= pos.stop:
                    pos.close(pos.qty, pos.stop)
                    self._finish(pos, ts, "stop")
                elif high >= pos.scale_out:
                    if pos.runner_qty >= 1:
                        pos.close(pos.bank_qty, pos.scale_out)
                        pos.banked = True
                        pos.trail_high = high
                    else:
                        pos.close(pos.qty, pos.scale_out)
                        self._finish(pos, ts, "target")
                elif (ts - pos.opened_ts) / 60 >= self.cfg.bot_time_stop_minutes:
                    pos.close(pos.qty, close)
                    self._finish(pos, ts, "time_stop")
                continue

            # Runner: trail behind the high water mark.
            pos.trail_high = max(pos.trail_high, high)
            trail = pos.trail_high * (1 - self.cfg.bot_runner_trail_pct / 100)
            if low <= trail:
                pos.close(pos.runner_qty, trail)
                self._finish(pos, ts, "trailing")

    def _scalp(self, pos, ts, bar, high, low, close):
        """Fixed-cent target, flat stop, then the runner rides a trail."""
        cfg = self.cfg
        pos.dojis = pos.dojis + 1 if is_doji(bar, cfg) else 0
        remaining = pos.qty - sum(q for q, _ in pos.legs)

        if pos.banked:
            # The runner rides a stop that ratchets up behind the high water
            # mark and can never cross back under the entry. This mirrors
            # TradingBot._protect_runner: if the two disagree, the backtest
            # measures a strategy the bot does not trade.
            #
            # The mark takes this bar's high before the low is tested against
            # it. Intrabar order is unknowable, but this is the honest side of
            # the guess: if the low really came first the trade was never
            # stopped at all and this exits early, and if the high came first
            # the fill is where the stop actually sat.
            pos.trail_high = max(pos.trail_high, high)
            stop = pos.stop
            if pos.trail_pct:
                stop = max(stop, round(pos.trail_high
                                       * (1 - pos.trail_pct / 100), 2))
            if low <= stop:
                pos.close(remaining, stop)
                self._finish(pos, ts,
                             "trailing" if pos.trail_pct else "breakeven")
                return
        else:
            # Stop before target on a bar that spans both.
            if low <= pos.stop:
                pos.close(remaining, pos.stop)
                self._finish(pos, ts, "stop")
                return

            # KNOWN OPTIMISM, and the reason every scalp figure here is an
            # upper bound: this fills the target off the bar HIGH, while the
            # live bot (TradingBot._manage_runner) can only compare the last
            # polled price. A wick that tags +20c and retreats inside the same
            # minute pays here and does not pay live. It is not fixable by
            # making the two agree - a live session cannot see the high of a
            # minute that has not finished - so it is written down instead.
            if high >= pos.scale_out:
                if pos.runner_qty >= 1:
                    pos.close(pos.bank_qty, pos.scale_out)
                    pos.banked = True
                    # The trade can no longer lose. That is the whole reason
                    # to take part of it off here.
                    pos.stop = pos.entry
                    pos.trail_high = max(pos.trail_high, high)
                    pos.trail_pct = (
                        runner_trail_pct(pos.entry, pos.scale_out, cfg)
                        if cfg.bot_runner_uses_trail else None)
                    return
                pos.close(remaining, pos.scale_out)
                self._finish(pos, ts, "target")
                return

        # Stalling out: buyers and sellers balanced for N bars running. This
        # applies to the runner too - when the move stops, get out.
        if pos.dojis >= cfg.bot_doji_exit_bars:
            pos.close(remaining, close)
            self._finish(pos, ts, "stall")
            return

        # The clock is only for a position that has not paid yet; a banked
        # runner rides behind its trail.
        if (not pos.banked
                and (ts - pos.opened_ts) / 60 >= cfg.bot_time_stop_minutes):
            pos.close(remaining, close)
            self._finish(pos, ts, "time_stop")

    # ----------------------------------------------------------- entries

    def enter(self, now, ts, qualified_rows):
        if not _in_entry_window(now, self.cfg):
            return
        if _at_or_past(now, self.cfg.bot_flatten_time):
            return
        # The same capital constraint the live bot works under, or the
        # backtest would model one position while the bot trades several.
        # The account does NOT compound across the run - see bankroll_from -
        # so R multiples stay comparable from the start of the sample to the
        # end.
        deployed = sum(p.qty * p.entry for p in self.open.values())
        picks = choose_entries(
            qualified_rows, self.scorer,
            trades_today=self.closed + len(self.open),
            traded_symbols=set(self.traded),
            day_pnl=0.0, now=now, cfg=self.cfg,
            score_threshold=self.score_bar,
            losses_today=self.losses,
            open_positions=len(self.open),
            budget=max(0.0, self.cfg.bot_bankroll - deployed))
        for pick in picks:
            levels = exit_levels(pick["price"], self.cfg,
                                 stop_price=pick.get("stop"))
            if levels["stop"] >= pick["price"]:
                continue
            target = levels["scale_out"]
            trade_id = self.journal.record_trade_open(
                ts, pick["symbol"], qty=pick["qty"], entry=pick["price"],
                stop=levels["stop"], targets=[target],
                features=pick["features"], setup=pick.get("setup"))
            self.open[pick["symbol"]] = Position(trade_id, pick, levels, ts,
                                                 self.cfg)
            self.traded.add(pick["symbol"])

    def close_out(self, ts, last_bars):
        """Anything still open at the end of the data closes on its last print."""
        for symbol, pos in list(self.open.items()):
            bar = last_bars.get(symbol) or {}
            price = bar.get("c") or pos.entry
            pos.close(pos.qty - sum(q for q, _ in pos.legs), price)
            self._finish(pos, ts, "flatten")
