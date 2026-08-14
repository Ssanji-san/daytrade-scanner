"""Alpaca trading client, hard-locked to the PAPER endpoint.

This module refuses to exist against a live account: construction raises
unless the base URL is the paper API. Do not remove that check.
"""
import asyncio
import os

import aiohttp

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

    @staticmethod
    def market_payload(symbol, qty, side):
        return {"symbol": symbol, "qty": str(qty), "side": side,
                "type": "market", "time_in_force": "day"}

    @staticmethod
    def stop_payload(symbol, qty, stop_price, side="sell"):
        return {"symbol": symbol, "qty": str(qty), "side": side,
                "type": "stop", "time_in_force": "day",
                "stop_price": f"{stop_price:.2f}"}

    @staticmethod
    def oto_stop_payload(symbol, qty, stop_price):
        """Entry + attached stop as ONE order.

        Submitting the buy and the stop separately trips Alpaca's wash-trade
        guard ("opposite side market/stop order exists"), which is why the
        entry must be a single complex order.
        """
        return {"symbol": symbol, "qty": str(qty), "side": "buy",
                "type": "market", "time_in_force": "day",
                "order_class": "oto",
                "stop_loss": {"stop_price": f"{stop_price:.2f}"}}

    @staticmethod
    def trailing_stop_payload(symbol, qty, trail_percent, side="sell"):
        return {"symbol": symbol, "qty": str(qty), "side": side,
                "type": "trailing_stop", "time_in_force": "day",
                "trail_percent": f"{trail_percent:g}"}

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
                if resp.status >= 400:
                    # Alpaca explains rejections in the body; raise_for_status
                    # would discard it and leave us guessing at a bare 422.
                    body = (await resp.text())[:400]
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=resp.status,
                        message=f"{resp.reason}: {body}", headers=resp.headers)
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

    async def submit_market_buy(self, symbol, qty):
        return await self._request("POST", "/v2/orders",
                                   json=self.market_payload(symbol, qty, "buy"))

    async def submit_market_sell(self, symbol, qty):
        return await self._request("POST", "/v2/orders",
                                   json=self.market_payload(symbol, qty, "sell"))

    async def submit_oto_stop(self, symbol, qty, stop_price):
        return await self._request("POST", "/v2/orders",
                                   json=self.oto_stop_payload(symbol, qty,
                                                              stop_price))

    async def cancel_orders_for(self, symbol):
        """Cancel every open order on a symbol, attached stop legs included."""
        orders = await self._request(
            "GET", "/v2/orders", params={"status": "open", "symbols": symbol})
        for order in orders or []:
            try:
                await self.cancel_order(order["id"])
            except aiohttp.ClientResponseError:
                pass   # already filled or cancelled

    async def submit_stop(self, symbol, qty, stop_price):
        return await self._request("POST", "/v2/orders",
                                   json=self.stop_payload(symbol, qty, stop_price))

    async def submit_trailing_stop(self, symbol, qty, trail_percent):
        return await self._request(
            "POST", "/v2/orders",
            json=self.trailing_stop_payload(symbol, qty, trail_percent))

    async def close_position(self, symbol):
        return await self._request("DELETE", f"/v2/positions/{symbol}")

    async def cancel_order(self, order_id):
        return await self._request("DELETE", f"/v2/orders/{order_id}")

    async def portfolio_history(self, period="1M", timeframe="1D"):
        return await self._request("GET", "/v2/account/portfolio/history",
                                   params={"period": period,
                                           "timeframe": timeframe})
