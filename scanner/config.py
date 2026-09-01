"""All tunables in one place.

Thresholds follow Ross Cameron's stock-selection criteria (float, % up
today, volume, relative volume, news). Change values here, not in the
scanner logic.
"""
from dataclasses import dataclass


@dataclass
class Config:
    # --- poll loop ---
    poll_seconds: float = 3.0          # movers/actives/snapshots cycle
    news_poll_seconds: float = 30.0
    calendar_poll_seconds: float = 600.0
    movers_top: int = 50
    actives_top: int = 100
    candidate_ttl_minutes: int = 30    # keep tracking a symbol this long after it leaves the lists

    # --- scanner 1: rolling top gainers ---
    gainer_windows: tuple = (5, 10, 15)   # minutes
    gainer_rows: int = 20

    # --- scanner 2: HOD momentum (Ross Cameron's five criteria) ---
    hod_min_price: float = 1.0          # $1-$5, matching the bot's band
    hod_max_price: float = 5.0
    # Watched but never bought. Rows priced above hod_max_price and up to
    # here are graded into the training set with "price" among their failed
    # criteria, so the model learns what a $5-10 mover does without a cent
    # being risked on one. 0 means observe only what can be traded.
    hod_observe_max_price: float = 10.0
    # 50M, not Ross's 20M. Across 61 replayed sessions of the whole market
    # only 5 rows cleared all the gates at once, which is not enough trades
    # to learn from. These four numbers are loosened together and measured
    # by scripts/sweep.py; they are a starting point, not a claim.
    hod_max_float: float = 20_000_000   # shares (approximated by shares outstanding)
    hod_min_pct_up: float = 10.0        # % up vs previous close
    # Disabled (0 = no check). An absolute share count measures the wrong
    # thing here: Ross's "100k traded" assumes the consolidated tape, but
    # Alpaca's free feed only shows IEX's slice of it, so the number is a
    # fraction of what the stock really traded and varies with how much of
    # the flow happened to route to IEX. Over six sessions it was the sole
    # blocker on 19 setups (4 of which went on to hit +2R) while only two
    # rows qualified in total. rvol carries the liquidity test instead - it
    # compares IEX to IEX, so the feed's share cancels out of the ratio.
    hod_min_volume: int = 0            # cumulative IEX shares today
    # Baseline liquidity: does this thing trade AT ALL on a normal day?
    # Dropping the daily floor let dead instruments through - WVVIP, a
    # preferred share, printed 0-1,295 shares a DAY yet showed a huge
    # percentage move, and rvol looks enormous against a near-zero
    # baseline. An average is not distorted by one quiet session, so it
    # is the honest way to say "there is someone on the other side".
    hod_min_avg_volume: int = 10_000    # 30-day average IEX shares
    # Back to Ross's 5x. It was dropped to 3x to manufacture more trades
    # and bought almost nothing - the strategy still only fired once every
    # ten days - while admitting materially weaker action. Lower it again
    # only with a measurement showing what the weaker rows are worth.
    hod_min_rvol: float = 5.0           # relative volume vs 30-day average
    # % gained since the 9:30 bell, not since yesterday's close. 0 disables.
    # This is the opening drive: the stock being bought right now, rather
    # than one that gapped overnight and has drifted since.
    hod_min_open_pct: float = 5.0
    hod_require_news: bool = False      # UI toggle; badge always shown
    # "Near the high", not "at the high". The entry is the pullback, and a
    # healthy flag pulls back 2-5% - a 1% gate rejected most of them and
    # only let the trade through after price had already run past the
    # signal, which is the chasing this strategy exists to avoid.
    hod_near_high_pct: float = 6.0      # within this % of the day high
    hod_rows: int = 20
    # 2, not 1. The near list is the only thing on the dashboard that says
    # WHY nothing qualified, and with gates this tight almost nothing misses
    # by exactly one - a 769% mover failing on float and news vanished
    # entirely, leaving a blank panel that reads as a broken scanner. Near
    # rows are graded for learning and never traded, so widening this
    # changes what is seen, not what is bought.
    near_filter_max_failures: int = 2   # dimmed "about to qualify" section

    # --- entry setups (Ross Cameron micro-pullback / flat top) ---
    # A gapper at the open has no session bars behind it, so it gets its own
    # trigger: the first few minutes form a range, then the break of that
    # range's high is the entry and its low is the stop.
    orb_minutes: int = 5                 # opening range = first N min from 9:30
    gap_min_pct: float = 10.0            # gap-up size that makes it a gapper
    setup_lookback_bars: int = 10        # 1-min bars scanned for the swing high
    setup_max_pullback_bars: int = 3     # 1-3 red candles, then the break
    setup_min_pullback_pct: float = 0.4  # below this it's noise, not a pullback
    setup_max_pullback_pct: float = 8.0  # above this the move has broken down
    setup_flat_top_tolerance_pct: float = 0.3   # highs within this = flat top
    require_vwap: bool = True            # never long below VWAP

    # --- relative volume ---
    rvol_baseline_days: int = 30
    # Linear time-of-day adjustment floor: before this fraction of the
    # session has elapsed, treat elapsed as this to avoid absurd rvol at the open.
    rvol_min_session_fraction: float = 0.05

    # --- news / catalyst quality ---
    news_max_age_hours: float = 24.0    # headline this recent => catalyst badge
    catalyst_min_score: float = 0.15    # below this the "news" is not a reason
    catalyst_fresh_minutes: float = 60.0   # full weight while it is breaking
    catalyst_veto: tuple = ("offering",)   # dilution kills the runner

    # --- calendar ---
    calendar_impacts: tuple = ("High", "Medium")  # red / orange
    calendar_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8124

    # --- paper-trading bot ---
    # The broker refuses to start unless trading_base contains "paper-api";
    # this bot must never touch a live account.
    trading_base: str = "https://paper-api.alpaca.markets"
    # The account to size off when there is no live reading: the backtest's
    # balance, and the live fallback until /v2/account answers. The real
    # account replaces it - see strategy.bankroll_from. This is NOT what one
    # position is worth; that is bot_position_dollars below.
    bot_bankroll: float = 2_500.0
    # What ONE position is worth. The account decides how MANY of these fit,
    # so a $2,473 balance holds $1,000 + $1,000 + $473 rather than putting
    # everything into a single trade. Risk and notional are the same lever at
    # these settings: 5% risk against a 5% stop is 100% of the unit, so the
    # two formulas agree and neither silently overrides the other - $50 at
    # risk per position, at any share price.
    bot_position_dollars: float = 1_000.0
    bot_risk_pct: float = 5.0            # % of the unit risked per position
    bot_max_notional_pct: float = 100.0  # position size cap as % of the unit
    # The last slice of an account takes whatever is left, but not below
    # this: a $60 position at the 20c target grosses about $2.40 on a $1-5
    # name and the round trip eats it.
    bot_min_position_dollars: float = 150.0
    # A hard ceiling in dollars, whatever the account grows to. Position
    # size tracks the balance up to here and then stops.
    bot_max_notional_dollars: float = 15_000.0
    bot_max_trades_per_day: int = 10
    # How long an accepted entry may sit unfilled before it is pulled. The
    # setup that justified the price is stale by then, and an order left
    # working can fill into a different market entirely.
    bot_entry_timeout_seconds: int = 120
    # --- scalping ---
    # Take profit at a fixed distance in cents rather than a multiple of
    # risk. Worth knowing what that implies: with a 5% stop the same 20c is
    # a 4:1 reward on a $1 stock and 0.8:1 on a $5 one, because $1,000 buys
    # five times as many shares down there. The price band is deliberately
    # narrow for that reason, and results are reported by price bucket.
    bot_scalp_mode: bool = True
    bot_scalp_target_cents: float = 0.20
    # Sell this share of the position at the target and let the rest run,
    # governed by the stall exit below. 0 takes the whole thing off.
    bot_scalp_scale_out_pct: float = 65.0
    # After banking, the runner rides a TRAILING stop that ratchets up
    # behind the high water mark, capped so it can never start below the
    # entry - the trade can no longer lose, which is the point of scaling out
    # at all, and a runner that keeps going now keeps more of it. False falls
    # back to a fixed stop at break-even. Read by both the live bot and the
    # simulator; when only one of them read it the two silently diverged.
    bot_scalp_runner_trail: bool = True
    # A doji is a bar that opens and closes in the same place - buyers and
    # sellers balanced. Two in a row is the stall to get out on. One-minute
    # bars are the finest the free feed carries, so a 5-10 second stutter is
    # not observable and cannot be honestly backtested.
    bot_doji_exit_bars: int = 2
    bot_doji_body_pct: float = 20.0      # body <= this % of the bar's range
    bot_max_losses_per_day: int = 4      # the day's kill switch: 4 losers, stop
    # A ceiling on top of the capital constraint, not instead of it: the
    # account already limits how many $1,000 units fit ($2,473 buys three).
    # This bounds exposure if the balance grows - raise it deliberately
    # rather than letting position count climb on its own.
    bot_max_concurrent_positions: int = 5
    bot_min_price: float = 1.0           # scalping band, $1-$5
    bot_max_price: float = 5.0
    # Min and max both at 20 collapses the band, so technical_stop returns a
    # flat 20% on every trade and skips anything wider. This deliberately
    # discards the technical stop - the pullback low Ross places the stop at -
    # in favour of a fixed percentage. Restore 1.0/6.0 to undo it.
    # Flat 5%: the stop is a fixed slice of the money at work, not the
    # setup low. On $1,000 that is exactly $50, at any share price.
    bot_stop_pct: float = 5.0            # fallback stop when no setup low exists
    bot_min_stop_pct: float = 5.0        # floor: never risk less than noise
    bot_max_stop_pct: float = 5.0        # skip setups whose stop is this far away
    bot_limit_slippage_pct: float = 0.3  # marketable limit above the ask
    bot_scale_out_r: float = 2.0         # bank half here
    bot_runner_trail_pct: float = 5.0    # native trailing-stop width for the runner
    # Scalping: in and out. Last entry 12:30 + 10m = 12:40, long before the
    # 15:50 flatten. Note this fires far more often than the +20c target -
    # both live scalps so far ended on the stall or the stop, neither on
    # the target.
    bot_time_stop_minutes: int = 10
    # The grading horizon must match the holding horizon, or the journal
    # labels a trade a loss while the bot is still holding it.
    bot_alert_window_minutes: int = 10
    # 09:30, not 09:35: the first five minutes are often the best move of the
    # day on a gapper, and the opening-range break lives in exactly that slot.
    bot_window_open: str = "09:30"       # ET; no entries before/after the window
    # Three hours, not one. A single hour fires roughly once every ten
    # sessions; Ross takes several trades a day off this setup, and the
    # window was the only lever that adds trades without relaxing a
    # criterion. Three and not four because the runner is the ceiling:
    # cron-job.org starts the session at 07:30 ET and GitHub kills a job at
    # six hours, so a 12:30 close (session to 12:45 = 5h15m) fits and a
    # 13:30 close does not. Watch open_pct: it measures from the 09:30
    # bell, so a midday row "up 5% since the open" may be riding a move
    # hours old. If the late entries are the losing ones, bring this back.
    bot_window_close: str = "12:30"
    bot_flatten_time: str = "15:50"      # ET; close everything before the bell
    # 0 = disabled. The day now ends on a loss COUNT
    # (bot_max_losses_per_day), not a dollar figure. Worth knowing: a count
    # bounds how many losers close, not how much is open - with 4 concurrent
    # slots the bot can still hold 3 positions when the 4th loss trips the
    # cap. Set this above 0 to restore a hard dollar floor.
    bot_daily_loss_pct: float = 0.0      # kill switch: stop entering for the day
    # Fixed bar for the hand-written heuristic only. A TRAINED model emits
    # a calibrated probability, and with roughly a quarter of setups
    # reaching +2R its scores sit far below 0.55 - keeping that bar made
    # the smarter model stop trading entirely. Once trained, the bar comes
    # from the model's own distribution instead (top quartile of what it
    # has seen), so it re-calibrates as the model improves.
    bot_score_threshold: float = 0.55
    bot_score_percentile: float = 75.0
    bot_model_min_samples: int = 40      # below this, heuristic scoring
    learn_from_near_misses: bool = True  # grade near-miss rows too (never
                                         # traded) so the model sees what
                                         # separates a winner from an
                                         # almost-winner
    bot_journal_path: str = "cache/journal.db"
    # The replay must cover the same hours the live session does. Training
    # on afternoons the bot never trades would teach it a market it does not
    # see - the same train/serve skew that argues against SIP training data.
    backtest_open_et: str = "07:30"     # cron-job.org starts the session here
    backtest_close_et: str = "12:30"    # session.py --until-et
    # Simulated results live in their own journal. Mixing them into the live
    # one would let a biased replay quietly poison what the bot has learned
    # from real sessions, with no way to tell the two apart afterwards.
    # The live near-list keeps rows failing one criterion, which is right
    # for a dashboard. A sweep needs the ones failing two or three as well:
    # otherwise it can never see what loosening a pair of gates would admit,
    # and would only ever explore what the current settings already allow.
    backtest_near_failures: int = 3
    # Ross requires a catalyst, and it is the single most restrictive gate.
    # Turning it off measures how many more setups exist without one - and
    # whether they are worth taking, which is a different question.
    backtest_require_news: bool = True
    backtest_journal_path: str = "cache/backtest.db"
    backtest_cache_dir: str = "cache/backtest"

    # --- data endpoints ---
    data_base: str = "https://data.alpaca.markets"
    feed: str = "iex"                   # snapshots/bars feed on the free plan
    sec_tickers_url: str = "https://www.sec.gov/files/company_tickers.json"
    float_cache_days: int = 7
    # A float we could not fetch is retried within the hour, not written off
    # for a week. Unknown float is an automatic rejection, so caching a
    # timeout as "no float" silently removes the stock from the strategy.
    float_retry_minutes: int = 60
    float_cache_path: str = "cache/floats.json"


DEFAULT = Config()
