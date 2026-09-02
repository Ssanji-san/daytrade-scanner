import pytest

from scanner.config import Config
from scanner.hod import scan

from .fixtures import make_state

CFG = Config()


def test_qualifying_stock_passes_all_criteria():
    qualified, near = scan([make_state()], CFG)
    assert len(qualified) == 1 and near == []
    row = qualified[0]
    assert row["failed"] == []
    assert row["dist_from_hod"] == pytest.approx(100 * (3.03 - 3.00) / 3.03)


def test_price_band_is_a_hard_gate():
    # A $50 stock is out of scope entirely - not even a near miss
    qualified, near = scan([make_state(price=50.0, day_high=50.5)], CFG)
    assert qualified == [] and near == []
    qualified, near = scan([make_state(price=0.80, day_high=0.81)], CFG)
    assert qualified == [] and near == []


@pytest.mark.parametrize("override,expected_fail", [
    ({"rvol": 2.0}, "rvol"),
    ({"float_shares": 90_000_000}, "float"),
    ({"day_pct": 4.0}, "pct_up"),
    ({"price": 5.00, "day_high": 5.55}, "hod"),  # ~10% off the high
])
def test_one_missing_demand_pillar_still_qualifies(override, expected_fail):
    """Four of five, Ross's own bar - and the miss is still recorded.

    The row is bought, but `failed` keeps the name, so the dashboard says
    which pillar was weak and the model trains on it.
    """
    qualified, near = scan([make_state(**override)], CFG)
    assert [r["symbol"] for r in qualified] == ["TEST"]
    assert near == []
    assert qualified[0]["failed"] == [expected_fail]


@pytest.mark.parametrize("override,expected_fail", [
    ({"price": 7.00, "day_high": 7.07}, "price"),
    ({"above_vwap": False}, "vwap"),
    ({"avg_volume": 300}, "liquidity"),
])
def test_a_disqualifying_failure_is_never_voted_down(override, expected_fail):
    """Perfect on every demand pillar and still not buyable.

    These three are not "how much demand is there" but "may I buy this"
    and "can I get back out". One of them is enough on its own.
    """
    strict = Config(require_vwap=True)
    qualified, near = scan([make_state(**override)], strict)
    assert qualified == []
    assert near[0]["failed"] == [expected_fail]


def test_unknown_float_counts_as_failure():
    """Unknown is not a pass. It is the one pillar this row is missing, so
    it can carry it - but paired with a second miss the row drops out."""
    qualified, near = scan([make_state(float_shares=None)], CFG)
    assert qualified[0]["failed"] == ["float"]

    qualified, near = scan([make_state(float_shares=None, rvol=2.0)], CFG)
    assert qualified == []
    assert sorted(near[0]["failed"]) == ["float", "rvol"]


def test_two_failures_are_shown_as_near_misses():
    """The near list is the only place the dashboard says WHY nothing
    qualified, and with these gates almost nothing misses by exactly one."""
    qualified, near = scan([make_state(rvol=1.0, day_pct=3.0)], CFG)
    assert qualified == []
    assert sorted(near[0]["failed"]) == ["pct_up", "rvol"]


def test_three_failures_are_still_excluded():
    qualified, near = scan([make_state(rvol=1.0, day_pct=3.0,
                                       float_shares=None)], CFG)
    assert qualified == [] and near == []


def test_news_optional_by_default():
    no_news = make_state(has_news=False, catalyst=None)
    qualified, near = scan([no_news], CFG)
    assert len(qualified) == 1 and qualified[0]["has_news"] is False


def test_news_check_is_about_the_catalyst_not_just_a_headline():
    """When news is required it means a *reason*, not any press release."""
    strict = Config(hod_require_news=True)
    assert len(scan([make_state()], strict)[0]) == 1     # fresh FDA approval

    # No catalyst is a pillar this stock lacks - one miss, so it survives.
    qualified, near = scan([make_state(has_news=False, catalyst=None)], strict)
    assert qualified[0]["failed"] == ["news"]

    # A share offering is different in kind: not a missing reason but a
    # reason to stay OUT, however good the rest looks. It disqualifies.
    dilution = make_state(catalyst={"category": "offering", "weight": 0.05,
                                    "score": 0.05, "age_minutes": 5.0,
                                    "veto": True,
                                    "headline": "prices public offering"})
    qualified, near = scan([dilution], strict)
    assert qualified == [] and near[0]["failed"] == ["news"]


def test_sorted_by_day_pct_and_capped():
    states = [make_state(symbol=f"S{i}", day_pct=10.0 + i) for i in range(30)]
    qualified, _ = scan(states, CFG)
    assert len(qualified) == CFG.hod_rows
    assert qualified[0]["symbol"] == "S29"


def test_a_pullback_entry_is_tradable_not_stranded_in_the_near_list():
    """The HOD gate must not cancel out the pullback entry.

    The entry fires on a pullback, so price sits a few percent under the
    day high by definition. A gate tight enough to reject that makes the
    whole strategy unreachable: the setup fires and the row lands in the
    'near' list, which the bot never trades.
    """
    pulling_back = make_state(price=2.94, day_high=3.03,
                              setup={"setup": "micro_pullback", "stop": 2.90})
    qualified, near = scan([pulling_back], Config(hod_require_news=True))
    assert [r["symbol"] for r in qualified] == ["TEST"]
    assert near == []
    assert qualified[0]["dist_from_hod"] == pytest.approx(2.97, abs=0.05)


def test_volume_floor_is_off_by_default_and_applies_when_set():
    """rvol carries the liquidity test; the absolute floor is opt-in.

    An absolute share count on the IEX feed measures a fraction of real
    volume, so it is disabled (0) unless deliberately configured.
    """
    thin = make_state(day_volume=1_000)
    assert len(scan([thin], CFG)[0]) == 1          # no volume gate by default

    strict = Config(hod_min_volume=25_000)
    assert scan([thin], strict)[0][0]["failed"] == ["volume"]

    # ...and once it is set it counts like any other pillar: on its own the
    # row survives, alongside a second miss it does not.
    qualified, near = scan([make_state(day_volume=1_000, rvol=2.0)], strict)
    assert qualified == []
    assert sorted(near[0]["failed"]) == ["rvol", "volume"]


def test_an_instrument_that_barely_trades_is_rejected():
    """A huge % move on a few hundred shares is a quote, not an opportunity.

    Preferred shares and other dead tickers can print big percentage moves
    on almost no volume, and rvol looks enormous against a near-zero
    baseline, so the baseline itself has to be the gate.
    """
    dead = make_state(avg_volume=300, day_volume=1_295)
    qualified, near = scan([dead], CFG)
    assert qualified == []
    assert near[0]["failed"] == ["liquidity"]


def test_a_real_small_cap_still_passes():
    assert len(scan([make_state(avg_volume=400_000)], CFG)[0]) == 1


def test_opening_drive_gate_is_switchable():
    """0 disables it, the way hod_min_volume is disabled."""
    from dataclasses import replace
    drifted = make_state(open_pct=1.0)          # barely moved since the bell
    q, near = scan([drifted], CFG)
    assert q[0]["failed"] == ["open_drive"]     # counted, not fatal on its own
    q, near = scan([make_state(open_pct=1.0, rvol=2.0)], CFG)
    assert q == [] and "open_drive" in near[0]["failed"]

    off = replace(CFG, hod_min_open_pct=0.0)
    q, _ = scan([drifted], off)
    assert [r["symbol"] for r in q] == ["TEST"]
    assert q[0]["failed"] == []                 # the check is gone entirely


def test_a_missing_open_counts_as_a_failure():
    """Unknown is not a pass - the same rule the float check follows."""
    q, near = scan([make_state(open_pct=None)], CFG)
    assert q[0]["failed"] == ["open_drive"]
    q, near = scan([make_state(open_pct=None, day_pct=4.0)], CFG)
    assert q == [] and "open_drive" in near[0]["failed"]


class TestObservationBand:
    """$5-10 movers are graded for learning but can never be bought."""

    def test_a_seven_dollar_mover_is_observed_not_qualified(self):
        watched = make_state(price=7.00, day_high=7.10, vwap=6.50,
                             setup={"setup": "micro_pullback", "stop": 6.80})
        qualified, near = scan([watched], CFG)
        assert qualified == []                     # never buyable
        assert [r["symbol"] for r in near] == ["TEST"]
        assert near[0]["failed"] == ["price"]      # and it says why

    def test_above_the_observation_ceiling_is_invisible(self):
        assert scan([make_state(price=25.0, day_high=25.1)], CFG) == ([], [])

    def test_in_band_rows_are_unaffected(self):
        qualified, near = scan([make_state()], CFG)
        assert [r["symbol"] for r in qualified] == ["TEST"]
        assert near == []

    def test_zero_means_observe_only_what_can_be_traded(self):
        from dataclasses import replace
        cfg = replace(CFG, hod_observe_max_price=0.0)
        assert scan([make_state(price=7.00, day_high=7.10)], cfg) == ([], [])


def test_an_observed_row_can_never_reach_the_bot():
    """Two independent guards, because this one must not fail open.

    hod.scan leaves it out of `qualified`, and should_enter rejects the
    price band again. Trading a $7 stock on a $1-5 strategy would be a
    silent breach of the risk model, not a missed opportunity.
    """
    from scanner.trading.strategy import should_enter
    import datetime as dt
    from zoneinfo import ZoneInfo
    et = dt.datetime(2026, 7, 14, 9, 45, tzinfo=ZoneInfo("America/New_York"))
    take, reasons = should_enter("TEST", price=7.00, score=0.9, trades_today=0,
                                 traded_symbols=set(), day_pnl=0.0, now=et,
                                 cfg=CFG, score_threshold=0.0)
    assert not take and "price" in reasons


class TestQualifyingBar:
    """How many demand pillars a row may miss - and what it may never miss."""

    def test_the_bar_is_configurable_back_to_all_or_nothing(self):
        from dataclasses import replace
        strict = replace(CFG, hod_max_failures_to_qualify=0)
        qualified, near = scan([make_state(rvol=2.0)], strict)
        assert qualified == [] and near[0]["failed"] == ["rvol"]

    def test_two_misses_are_still_a_near_miss(self):
        """The near list has to keep something to show, or the dashboard
        goes blank and reads as a broken scanner."""
        qualified, near = scan([make_state(rvol=2.0, day_pct=4.0)], CFG)
        assert qualified == []
        assert sorted(near[0]["failed"]) == ["pct_up", "rvol"]

    def test_a_disqualifier_outvotes_a_perfect_row(self):
        """Not a count: one hard no beats four passing pillars."""
        strict = Config(require_vwap=True)
        qualified, near = scan([make_state(above_vwap=False)], strict)
        assert qualified == [] and near[0]["failed"] == ["vwap"]

    def test_a_disqualifier_is_not_forgiven_by_the_budget(self):
        """Even as the row's ONLY failure, with the budget unspent."""
        dead = make_state(avg_volume=300, day_volume=1_295)
        qualified, near = scan([dead], CFG)
        assert qualified == []
        assert near[0]["failed"] == ["liquidity"]
