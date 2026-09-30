"""Where a company operates, from SEC EDGAR, cached to disk.

Ross Cameron's caution: Chinese small caps that move without news are the
pump-and-dumps of this market. hod.scan lets one trade only with a fresh
catalyst, and needs to know which stocks those are.

The business address, not the place of incorporation: most of these
companies are Cayman or BVI holding companies whose business address is in
China or Hong Kong. EDGAR codes verified against live records: F4 China
(SOS, CPOP), K3 Hong Kong (MLCO, MSC). Macau is matched by name.
"""
from .config import Config
from .floats import SEC_HEADERS, FloatCache

CHINESE = {"china", "hong kong", "macau", "macao", "f4", "k3"}


def parse_country(submissions):
    """Business address country (mailing as fallback), or None."""
    addresses = (submissions or {}).get("addresses") or {}
    for kind in ("business", "mailing"):
        address = addresses.get(kind) or {}
        name = (address.get("stateOrCountryDescription")
                or address.get("stateOrCountry"))
        if name:
            return name
    return None


def is_chinese(country):
    """True / False, or None when the country is not known."""
    if not country:
        return None
    return country.strip().lower() in CHINESE


async def fetch_country(session, cik):
    """(country, answered), with the same meaning of `answered` as
    floats.fetch_shares: a 404 is SEC's answer, a 403/429/timeout is not."""
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    try:
        async with session.get(url, headers=SEC_HEADERS) as resp:
            if resp.status == 404:
                return None, True
            if resp.status != 200:
                return None, False
            return parse_country(await resp.json(content_type=None)), True
    except Exception:
        return None, False


class CountryCache(FloatCache):
    FIELD = "country"

    def __init__(self, cfg: Config):
        super().__init__(cfg, path=cfg.country_cache_path)
