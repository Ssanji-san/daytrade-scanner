"""Can the free feed fill a 10-second chart? Measured, not assumed."""
import asyncio
import datetime as dt

from scanner.alpaca import AlpacaClient
from scanner.config import Config
from scripts.trade_density import window_fill

T0 = dt.datetime(2026, 8, 12, 13, 30, tzinfo=dt.timezone.utc)


def at(seconds):
    return (T0 + dt.timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z")


def test_share_of_windows_with_a_trade():
    # 60 seconds = six 10s windows; trades land in windows 0, 0, 2 and 5.
    trades = [{"t": at(1)}, {"t": at(9)}, {"t": at(25)}, {"t": at(59)}]
    fill, per_window = window_fill(trades, T0, T0 + dt.timedelta(seconds=60))
    assert fill == 3 / 6
    assert per_window == 4 / 6


def test_trades_outside_the_span_are_ignored():
    trades = [{"t": at(-5)}, {"t": at(60)}, {"t": at(3)}]
    fill, _ = window_fill(trades, T0, T0 + dt.timedelta(seconds=60))
    assert fill == 1 / 6


def test_nanosecond_stamps_parse():
    trades = [{"t": "2026-08-12T13:30:05.123456789Z"}]
    assert window_fill(trades, T0, T0 + dt.timedelta(seconds=10))[0] == 1.0


def test_client_pages_through_trades():
    pages = [{"trades": {"AAA": [{"t": at(1)}]}, "next_page_token": "p"},
             {"trades": {"AAA": [{"t": at(2)}], "BBB": [{"t": at(3)}]}}]
    calls = []

    class Resp:
        status = 200

        def __init__(self, body):
            self.body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            return self.body

    class Session:
        def get(self, url, params=None, headers=None):
            calls.append(dict(params))
            return Resp(pages[len(calls) - 1])

    client = AlpacaClient(Session(), Config())
    out = asyncio.run(client.trades(["AAA", "BBB"], "s", "e", feed="sip"))
    assert {k: len(v) for k, v in out.items()} == {"AAA": 2, "BBB": 1}
    assert calls[0]["feed"] == "sip" and calls[1]["page_token"] == "p"
