from dataclasses import replace

import pytest

from scanner.config import Config
from scanner.trading.broker import Broker, PaperOnlyError

CFG = Config()


def test_refuses_non_paper_endpoint():
    live = replace(CFG, trading_base="https://api.alpaca.markets")
    with pytest.raises(PaperOnlyError):
        Broker(session=None, cfg=live)


def test_accepts_paper_endpoint():
    broker = Broker(session=None, cfg=CFG)
    assert "paper-api" in broker.base


def test_bracket_order_payload():
    broker = Broker(session=None, cfg=CFG)
    payload = broker.bracket_payload("HODX", qty=9, limit_price=None,
                                     stop_price=5.34, target_price=5.83)
    assert payload == {
        "symbol": "HODX", "qty": "9", "side": "buy", "type": "market",
        "time_in_force": "day", "order_class": "bracket",
        "take_profit": {"limit_price": "5.83"},
        "stop_loss": {"stop_price": "5.34"},
    }


def test_market_payload():
    broker = Broker(session=None, cfg=CFG)
    assert broker.market_payload("HODX", qty=50, side="buy") == {
        "symbol": "HODX", "qty": "50", "side": "buy",
        "type": "market", "time_in_force": "day",
    }


def test_stop_payload():
    broker = Broker(session=None, cfg=CFG)
    assert broker.stop_payload("HODX", qty=25, stop_price=4.85) == {
        "symbol": "HODX", "qty": "25", "side": "sell",
        "type": "stop", "time_in_force": "day", "stop_price": "4.85",
    }


def test_trailing_stop_payload():
    broker = Broker(session=None, cfg=CFG)
    assert broker.trailing_stop_payload("HODX", qty=25, trail_percent=5.0) == {
        "symbol": "HODX", "qty": "25", "side": "sell",
        "type": "trailing_stop", "time_in_force": "day", "trail_percent": "5",
    }
