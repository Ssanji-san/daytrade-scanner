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
    assert row["dist_from_hod"] == pytest.approx(100 * (5.55 - 5.50) / 5.55)


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
    ({"day_volume": 40_000}, "volume"),
    ({"price": 5.00, "day_high": 5.55}, "hod"),  # ~10% off the high
])
def test_single_failure_lands_in_near_list(override, expected_fail):
    qualified, near = scan([make_state(**override)], CFG)
    assert qualified == []
    assert len(near) == 1
    assert near[0]["failed"] == [expected_fail]


def test_unknown_float_counts_as_failure():
    qualified, near = scan([make_state(float_shares=None)], CFG)
    assert qualified == []
    assert near[0]["failed"] == ["float"]


def test_two_failures_excluded_entirely():
    qualified, near = scan([make_state(rvol=1.0, day_pct=3.0)], CFG)
    assert qualified == [] and near == []


def test_news_optional_by_default():
    no_news = make_state(has_news=False, catalyst=None)
    qualified, near = scan([no_news], CFG)
    assert len(qualified) == 1 and qualified[0]["has_news"] is False


def test_news_check_is_about_the_catalyst_not_just_a_headline():
    """When news is required it means a *reason*, not any press release."""
    strict = Config(hod_require_news=True)
    assert len(scan([make_state()], strict)[0]) == 1     # fresh FDA approval

    qualified, near = scan([make_state(has_news=False, catalyst=None)], strict)
    assert qualified == [] and near[0]["failed"] == ["news"]

    # A share offering is a reason to stay OUT however good it looks.
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
    pulling_back = make_state(price=5.38, day_high=5.50,
                              setup={"setup": "micro_pullback", "stop": 5.35})
    qualified, near = scan([pulling_back], Config(hod_require_news=True))
    assert [r["symbol"] for r in qualified] == ["TEST"]
    assert near == []
    assert qualified[0]["dist_from_hod"] == pytest.approx(2.18, abs=0.05)
