"""All tunables in one place.

Thresholds follow Ross Cameron's stock-selection criteria (float, % up
today, volume, relative volume, news). Change values here, not in the
scanner logic.
"""
from dataclasses import dataclass, field


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
    hod_min_price: float = 1.0
    hod_max_price: float = 20.0
    # 50M, not Ross's 20M. Across 61 replayed sessions of the whole market
    # only 5 rows cleared all the gates at once, which is not enough trades
    # to learn from. These four numbers are loosened together and measured
    # by scripts/sweep.py; they are a starting point, not a claim.
    hod_max_float: float = 50_000_000   # shares (approximated by shares outstanding)
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
    hod_min_rvol: float = 3.0           # relative volume vs 30-day average
    hod_require_news: bool = False      # UI toggle; badge always shown
    # "Near the high", not "at the high". The entry is the pullback, and a
    # healthy flag pulls back 2-5% - a 1% gate rejected most of them and
    # only let the trade through after price had already run past the
    # signal, which is the chasing this strategy exists to avoid.
    hod_near_high_pct: float = 6.0      # within this % of the day high
    hod_rows: int = 20
    near_filter_max_failures: int = 1   # dimmed "about to qualify" section

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
    news_per_symbol: int = 3
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
    bot_bankroll: float = 1_000.0        # simulated account (paper balance ignored)
    # 5% risk against a 20% stop puts exactly $250 into each position
    # ($50 / (0.20 x price) shares), which is also the 25% notional cap - the
    # two formulas agree, so neither one silently overrides the other.
    bot_risk_pct: float = 5.0            # % of bankroll risked per trade
    bot_max_notional_pct: float = 25.0   # position size cap as % of bankroll
    bot_max_trades_per_day: int = 10
    bot_max_losses_per_day: int = 4      # the day's kill switch: 4 losers, stop
    # 4 x $250 is the whole account. Without this the bot could hold ten
    # positions at once against a $1,000 balance, since a 4-hour hold does
    # not turn over fast enough for the daily cap to bound exposure.
    bot_max_concurrent_positions: int = 4
    bot_min_price: float = 2.0           # bot trades $2-$20 only (user request)
    bot_max_price: float = 20.0
    # Min and max both at 20 collapses the band, so technical_stop returns a
    # flat 20% on every trade and skips anything wider. This deliberately
    # discards the technical stop - the pullback low Ross places the stop at -
    # in favour of a fixed percentage. Restore 1.0/6.0 to undo it.
    bot_stop_pct: float = 20.0           # fallback stop when no setup low exists
    bot_min_stop_pct: float = 20.0       # floor: never risk less than noise
    bot_max_stop_pct: float = 20.0       # skip setups whose stop is this far away
    bot_limit_slippage_pct: float = 0.3  # marketable limit above the ask
    bot_scale_out_r: float = 2.0         # bank half here
    bot_runner_trail_pct: float = 5.0    # native trailing-stop width for the runner
    # 4 hours, not 20 minutes. A 20% stop implies a +40% target, and almost
    # nothing moves 40% in 20 minutes - 186 of 414 replayed losses (45%) were
    # the clock expiring, not the stop being hit. Last entry 11:30 + 4h =
    # 15:30, still inside the 15:50 flatten.
    bot_time_stop_minutes: int = 240     # only applies before scale-out
    # The grading horizon must match the holding horizon, or the journal
    # labels a trade a loss while the bot is still holding it.
    bot_alert_window_minutes: int = 240
    # 09:30, not 09:35: the first five minutes are often the best move of the
    # day on a gapper, and the opening-range break lives in exactly that slot.
    bot_window_open: str = "09:30"       # ET; no entries before/after the window
    bot_window_close: str = "11:30"
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
    backtest_close_et: str = "12:15"    # session.py --until-et
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
    # Sample every mover, not only the ones that cleared the gates. Training
    # solely on rows the filters already surfaced means the model can only
    # rank within them - it never learns what a 2R move looks like in the
    # population it is not being shown. observed=2 marks these.
    backtest_sample_all: bool = False
    backtest_journal_path: str = "cache/backtest.db"
    backtest_cache_dir: str = "cache/backtest"
    bot_forward_marks_min: tuple = (5, 15, 30)   # forward-return checkpoints

    # --- data endpoints ---
    data_base: str = "https://data.alpaca.markets"
    feed: str = "iex"                   # snapshots/bars feed on the free plan
    sec_tickers_url: str = "https://www.sec.gov/files/company_tickers.json"
    float_cache_days: int = 7
    float_cache_path: str = "cache/floats.json"


DEFAULT = Config()
