"""Catalyst quality: the size of the reason behind the move."""
import pytest

from scanner.catalyst import classify, freshness, is_tradable, score_news
from scanner.config import Config

CFG = Config()
NOW = 1_700_000_000


def item(headline, minutes_ago=5):
    return {"symbol": "HODX", "headline": headline,
            "ts": NOW - int(minutes_ago * 60)}


class TestClassify:
    @pytest.mark.parametrize("headline,expected", [
        ("XYZ receives FDA approval for lead candidate", "fda"),
        ("XYZ to be acquired by BigCo in $2B deal", "merger"),
        ("XYZ awarded $40M Department of Defense contract", "contract"),
        ("XYZ beats on earnings, raises guidance", "earnings"),
        ("XYZ announces AI partnership with chipmaker", "hype"),
        ("XYZ uplists to Nasdaq", "uplisting"),
        ("XYZ announces pricing of public offering", "offering"),
        ("XYZ announces $50M registered direct offering", "offering"),
        ("XYZ appoints new chief marketing officer", "other"),
    ])
    def test_reads_the_reason(self, headline, expected):
        assert classify(headline)[0] == expected

    def test_dilution_wins_over_upbeat_wording(self):
        # Offering headlines are usually dressed up; dilution must still win.
        assert classify("XYZ announces record revenue and pricing of "
                        "public offering")[0] == "offering"


class TestFreshness:
    def test_breaking_news_keeps_full_weight(self):
        assert freshness(10, CFG) == 1.0
        assert freshness(CFG.catalyst_fresh_minutes, CFG) == 1.0

    def test_old_news_decays_but_never_to_zero(self):
        old = freshness(CFG.news_max_age_hours * 60, CFG)
        assert 0.2 < old < 0.5
        assert freshness(120, CFG) > freshness(600, CFG)


class TestScoreNews:
    def test_picks_the_biggest_reason_not_the_newest(self):
        best = score_news([item("XYZ names new CFO", 1),
                           item("XYZ receives FDA approval", 30)], NOW, CFG)
        assert best["category"] == "fda"
        assert best["score"] == pytest.approx(1.0)

    def test_fresh_beats_stale_for_the_same_kind_of_news(self):
        fresh = score_news([item("FDA approval", 5)], NOW, CFG)["score"]
        stale = score_news([item("FDA approval", 900)], NOW, CFG)["score"]
        assert fresh > stale

    def test_offering_vetoes_even_alongside_good_news(self):
        best = score_news([item("XYZ receives FDA approval", 20),
                           item("XYZ prices $30M public offering", 15)],
                          NOW, CFG)
        assert best["veto"] is True
        assert best["score"] <= 0.10
        assert not is_tradable(best, CFG)

    def test_ignores_news_outside_the_window(self):
        assert score_news([item("FDA approval", 60 * 48)], NOW, CFG) is None

    def test_no_news_is_no_catalyst(self):
        assert score_news([], NOW, CFG) is None
        assert not is_tradable(None, CFG)


class TestIsTradable:
    def test_real_catalyst_passes(self):
        assert is_tradable(score_news([item("FDA approval", 10)], NOW, CFG), CFG)

    def test_weak_stale_filler_fails(self):
        weak = score_news([item("XYZ publishes shareholder letter", 900)],
                          NOW, CFG)
        assert weak["score"] < CFG.catalyst_min_score
        assert not is_tradable(weak, CFG)
