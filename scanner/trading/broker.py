"""Alpaca trading client, hard-locked to the PAPER endpoint.

This module refuses to exist against a live account: construction raises
unless the base URL is the paper API. Do not remove that check.
"""
import asyncio
import datetime as dt
import os

import aiohttp

from ..config import Config

PAPER_MARKER = "paper-api"


class PaperOnlyError(RuntimeError):
    pass


def _filled_ts(leg):
    """Epoch seconds for a leg's fill time; None when absent or unparseable."""
    try:
        return dt.datetime.fromisoformat(
            str(leg.get("filled_at")).replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


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
    def market_payload(symbol, qty, side):
        return {"symbol": symbol, "qty": str(qty), "side": side,
                "type": "market", "time_in_force": "day"}

    @staticmethod
    def stop_payload(symbol, qty, stop_price, side="sell"):
        return {"symbol": symbol, "qty": str(qty), "side": side,
                "type": "stop", "time_in_force": "day",
                "stop_price": f"{stop_price:.2f}"}

    @staticmethod
    def oto_stop_payload(symbol, qty, stop_price, limit_price=None):
        """Entry + attached stop as ONE order.

        Submitting the buy and the stop separately trips Alpaca's wash-trade
        guard ("opposite side market/stop order exists"), which is why the
        entry must be a single complex order.
        """
        payload = {"symbol": symbol, "qty": str(qty), "side": "buy",
                   "type": "market", "time_in_force": "day",
                   "order_class": "oto",
                   "stop_loss": {"stop_price": f"{stop_price:.2f}"}}
        if limit_price:
            # Marketable limit: fills like a market order but refuses the
            # runaway prints that market orders eat on thin small caps.
            payload["type"] = "limit"
            payload["limit_price"] = f"{limit_price:.2f}"
        return payload

    @staticmethod
    def trailing_stop_payload(symbol, qty, trail_percent, side="sell"):
        return {"symbol": symbol, "qty": str(qty), "side": side,
                "type": "trailing_stop", "time_in_force": "day",
                "trail_percent": f"{trail_percent:g}"}

    # ---- fill parsing (pure, tested) ----

    @staticmethod
    def sell_legs(orders, after_ts=None):
        """(qty, price) for every filled sell leg, newer than `after_ts`.

        `after_ts` is not optional in spirit: /v2/orders answers newest-first
        across the whole account history, so a symbol traded on two different
        days would otherwise have yesterday's exits averaged into today's.
        The server-side `after` filter is by submission time, and a leg
        submitted with the entry can fill much later, so the fill time is
        checked here as well.
        """
        legs = []
        for order in orders or []:
            for leg in [order] + (order.get("legs") or []):
                if leg.get("side") != "sell" or not leg.get("filled_avg_price"):
                    continue
                filled_at = _filled_ts(leg)
                if after_ts and filled_at is not None and filled_at < after_ts:
                    continue
                legs.append((float(leg.get("filled_qty") or 0),
                             float(leg["filled_avg_price"])))
        return legs

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

    async def submit_market_sell(self, symbol, qty):
        return await self._request("POST", "/v2/orders",
                                   json=self.market_payload(symbol, qty, "sell"))

    async def submit_oto_stop(self, symbol, qty, stop_price, limit_price=None):
        return await self._request(
            "POST", "/v2/orders",
            json=self.oto_stop_payload(symbol, qty, stop_price, limit_price))

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

    async def order(self, order_id):
        """One order by id - is the entry still working, filled, or dead?"""
        return await self._request("GET", f"/v2/orders/{order_id}",
                                   params={"nested": "true"})

    async def closed_sell_legs(self, symbol, after_ts=None):
        """Filled sell legs for a symbol since `after_ts`. See sell_legs."""
        params = {"status": "closed", "symbols": symbol,
                  "limit": 50, "nested": "true"}
        if after_ts:
            params["after"] = dt.datetime.fromtimestamp(
                after_ts, dt.timezone.utc).isoformat()
        orders = await self._request("GET", "/v2/orders", params=params)
        return self.sell_legs(orders, after_ts)

    async def portfolio_history(self, period="1M", timeframe="1D"):
        return await self._request("GET", "/v2/account/portfolio/history",
                                   params={"period": period,
                                           "timeframe": timeframe})
