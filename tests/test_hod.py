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


def test_news_optional_by_default_but_required_when_configured():
    no_news = make_state(has_news=False)
    qualified, near = scan([no_news], CFG)
    assert len(qualified) == 1 and qualified[0]["has_news"] is False

    strict = Config(hod_require_news=True)
    qualified, near = scan([no_news], strict)
    assert qualified == []
    assert near[0]["failed"] == ["news"]


def test_sorted_by_day_pct_and_capped():
    states = [make_state(symbol=f"S{i}", day_pct=10.0 + i) for i in range(30)]
    qualified, _ = scan(states, CFG)
    assert len(qualified) == CFG.hod_rows
    assert qualified[0]["symbol"] == "S29"
