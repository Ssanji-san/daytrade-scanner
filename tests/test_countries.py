"""Chinese companies trade only on breaking news.

The country comes from SEC's company record: the business address, which
for a Cayman-incorporated holding company is where it actually operates.
"""
import asyncio
import datetime as dt
from dataclasses import replace

from scanner.config import Config
from scanner.countries import (CountryCache, fetch_country, is_chinese,
                               parse_country)

from .test_floats import _Resp, _Session

CFG = Config()


def submissions(business=None, mailing=None):
    return {"addresses": {"business": business or {},
                          "mailing": mailing or {}}}


class TestParse:
    def test_business_address_country(self):
        raw = submissions({"stateOrCountry": "F4",
                           "stateOrCountryDescription": "China"})
        assert parse_country(raw) == "China"

    def test_falls_back_to_the_mailing_address(self):
        raw = submissions({"stateOrCountry": None},
                          {"stateOrCountry": "K3",
                           "stateOrCountryDescription": "Hong Kong"})
        assert parse_country(raw) == "Hong Kong"

    def test_code_when_there_is_no_description(self):
        raw = submissions({"stateOrCountry": "NV"})
        assert parse_country(raw) == "NV"

    def test_none_when_no_address(self):
        assert parse_country(submissions()) is None
        assert parse_country({}) is None


class TestIsChinese:
    def test_china_hong_kong_and_macau(self):
        for name in ("China", "Hong Kong", "Macau", "Macao", "F4", "K3"):
            assert is_chinese(name) is True, name

    def test_elsewhere(self):
        for name in ("NV", "DE", "Israel", "Singapore", "Taiwan"):
            assert is_chinese(name) is False, name

    def test_unknown_is_unknown(self):
        assert is_chinese(None) is None


class TestFetch:
    def test_answered(self):
        raw = submissions({"stateOrCountryDescription": "China"})
        s = _Session([_Resp(200, raw)])
        assert asyncio.run(fetch_country(s, 1498576)) == ("China", True)
        assert "CIK0001498576" in s.asked[0]

    def test_a_404_is_an_answer(self):
        s = _Session([_Resp(404)])
        assert asyncio.run(fetch_country(s, 1)) == (None, True)

    def test_rate_limits_and_errors_are_not(self):
        for status in (403, 429, 500):
            s = _Session([_Resp(status)])
            assert asyncio.run(fetch_country(s, 1)) == (None, False)

        class Boom:
            def get(self, url, headers=None):
                raise OSError("reset")
        assert asyncio.run(fetch_country(Boom(), 1)) == (None, False)


def test_cache_roundtrip(tmp_path):
    cfg = replace(CFG, country_cache_path=str(tmp_path / "countries.json"))
    cache = CountryCache(cfg)
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
    assert cache.is_stale("SOS", now=now)
    cache.put("SOS", "China", now=now)
    assert CountryCache(cfg).get("SOS") == "China"
    assert not cache.is_stale("SOS", now=now)
    assert cache.is_stale("SOS", now + dt.timedelta(days=cfg.float_cache_days + 1))
