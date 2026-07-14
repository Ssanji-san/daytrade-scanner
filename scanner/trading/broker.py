"""Alpaca trading client, hard-locked to the PAPER endpoint.

This module refuses to exist against a live account: construction raises
unless the base URL is the paper API. Do not remove that check.
"""
import asyncio
import os

from ..config import Config

PAPER_MARKER = "paper-api"


class PaperOnlyError(RuntimeError):
    pass


class Broker:
    def __init__(self, session, cfg: Config, key=None, secret=None):
        if PAPER_MARKER not in cfg.trading_base:
            raise PaperOnlyError(
                f"trading_base must be the paper endpoint, got {cfg.trading_base!r}. "
                "This bot only trades on paper accounts.")
        self.session = session
        self.base = cfg.trading_base
        self.headers = {
            "APCA-API-KEY-ID": key or os.environ.get("ALPACA_KEY", ""),
            "APCA-API-SECRET-KEY": secret or os.environ.get("ALPACA_SECRET", ""),
        }

    # ---- payload builders (pure, tested) ----

    @staticmethod
    def bracket_payload(symbol, qty, limit_price, stop_price, target_price):
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "limit" if limit_price else "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{target_price:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
        }
        if limit_price:
            payload["limit_price"] = f"{limit_price:.2f}"
        return payload

    # ---- HTTP (thin) ----

    async def _request(self, method, path, json=None, params=None):
        url = self.base + path
        for attempt in range(4):
            async with self.session.request(method, url, json=json,
                                            params=params,
                                            headers=self.headers) as resp:
                if resp.status == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                if resp.status == 204:
                    return None
                return await resp.json()
        raise RuntimeError(f"rate limited after retries: {path}")

    async def account(self):
        return await self._request("GET", "/v2/account")

    async def positions(self):
        return await self._request("GET", "/v2/positions")

    async def open_orders(self):
        return await self._request("GET", "/v2/orders", params={"status": "open"})

    async def order(self, order_id):
        return await self._request("GET", f"/v2/orders/{order_id}")

    async def submit_bracket(self, symbol, qty, stop_price, target_price,
                             limit_price=None):
        return await self._request("POST", "/v2/orders",
                                   json=self.bracket_payload(
                                       symbol, qty, limit_price,
                                       stop_price, target_price))

    async def close_position(self, symbol):
        return await self._request("DELETE", f"/v2/positions/{symbol}")

    async def cancel_order(self, order_id):
        return await self._request("DELETE", f"/v2/orders/{order_id}")

    async def portfolio_history(self, period="1M", timeframe="1D"):
        return await self._request("GET", "/v2/account/portfolio/history",
                                   params={"period": period,
                                           "timeframe": timeframe})
