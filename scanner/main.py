"""App entrypoint: web server + background market loop.

    python -m scanner.main            # live (needs ALPACA_KEY / ALPACA_SECRET)
    python -m scanner.main --demo     # synthetic looping session, no keys needed
    python -m scanner.main --replay fixtures/recorded-session.json

Dashboard at http://127.0.0.1:8124
"""
import argparse
import asyncio
import datetime as dt
import json
import pathlib
import traceback

import aiohttp
from aiohttp import web

from .alpaca import AlpacaClient
from .calendar_feed import filter_events
from .config import DEFAULT, Config
from .demo import build_demo_bot_status, build_demo_session
from .floats import FloatCache, fetch_shares, fetch_ticker_map
from .state import MarketState
from .trading.bot import bot_loop

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"
FLOAT_FETCHES_PER_CYCLE = 4


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- live loop

async def live_loop(app, cfg: Config):
    state: MarketState = app["ctx"]["state"]
    async with aiohttp.ClientSession() as session:
        client = AlpacaClient(session, cfg)
        float_cache = FloatCache(cfg)
        ticker_map = {}
        candidates = {}   # symbol -> last time it appeared on a screener list
        avg_volumes = {}
        last_news = last_calendar = 0.0

        try:
            ticker_map = await fetch_ticker_map(session, cfg)
        except Exception as exc:
            print(f"[warn] SEC ticker map unavailable, floats disabled: {exc}")

        while True:
            cycle_started = utcnow()
            try:
                movers, actives = await asyncio.gather(
                    client.movers(), client.most_actives())
                now = utcnow()
                for sym in movers + actives:
                    candidates[sym] = now
                ttl = dt.timedelta(minutes=cfg.candidate_ttl_minutes)
                candidates = {s: t for s, t in candidates.items() if now - t < ttl}

                snaps = await client.snapshots(list(candidates))

                new_syms = [s for s in snaps if s not in avg_volumes]
                if new_syms:
                    avg_volumes.update(await client.avg_volumes(new_syms))

                to_fetch = [s for s in snaps if float_cache.is_stale(s)
                            and s in ticker_map][:FLOAT_FETCHES_PER_CYCLE]
                for sym in to_fetch:
                    shares = await fetch_shares(session, ticker_map[sym])
                    float_cache.put(sym, shares)
                    await asyncio.sleep(0.15)   # stay polite with SEC

                for sym, data in snaps.items():
                    data["avg_volume"] = avg_volumes.get(sym)
                    data["float_shares"] = float_cache.get(sym)
                state.ingest(now, snaps)

                if now.timestamp() - last_news > cfg.news_poll_seconds:
                    state.set_news(now, await client.news(list(candidates)))
                    last_news = now.timestamp()
                if now.timestamp() - last_calendar > cfg.calendar_poll_seconds:
                    async with session.get(cfg.calendar_url) as resp:
                        if resp.status == 200:
                            events = await resp.json(content_type=None)
                            state.set_calendar(filter_events(events, cfg))
                    last_calendar = now.timestamp()
            except Exception:
                # keep serving last-good state; the dashboard shows the stale banner
                traceback.print_exc()

            elapsed = (utcnow() - cycle_started).total_seconds()
            await asyncio.sleep(max(0.5, cfg.poll_seconds - elapsed))


# ------------------------------------------------------- demo / replay loop

async def playback_loop(app, cfg: Config, session_data=None, regenerate=False):
    """Feed a recorded/synthetic session through the same pipeline, looping."""
    ctx = app["ctx"]
    while True:
        data = build_demo_session(cfg) if regenerate else session_data
        state = MarketState(cfg)
        frames = data["frames"]
        # Backfill everything except the tail instantly, then tick the last
        # frames in real time so the user sees the dashboard move.
        tail = min(10, len(frames))
        for frame in frames[:-tail]:
            now = dt.datetime.fromtimestamp(frame["ts"], dt.timezone.utc)
            state.ingest(now, frame["symbols"])
        state.set_news(now, data["news"])
        state.set_calendar(filter_events(data["calendar_events"], cfg))
        ctx["state"] = state
        if regenerate:   # demo mode also fakes the bot panel
            ctx["bot_status"] = build_demo_bot_status(cfg)
        for frame in frames[-tail:]:
            now = dt.datetime.fromtimestamp(frame["ts"], dt.timezone.utc)
            state.ingest(now, frame["symbols"])
            ctx["virtual_now"] = now
            await asyncio.sleep(2)
        await asyncio.sleep(2)
        if not regenerate:
            ctx["virtual_now"] = None  # restart the same recording


# ------------------------------------------------------------------- server

async def api_state(request):
    ctx = request.app["ctx"]
    state: MarketState = ctx["state"]
    now = ctx["virtual_now"] or utcnow()
    require_news = request.query.get("require_news")
    payload = state.payload(now, require_news=(require_news == "1")
                            if require_news is not None else None)
    payload["mode"] = request.app["mode"]
    payload["now"] = int(now.timestamp())
    payload["bot"] = ctx.get("bot_status")
    return web.json_response(payload)


async def index(request):
    return web.FileResponse(WEB_DIR / "index.html")


def build_app(cfg: Config, mode, runner_coros):
    app = web.Application()
    app["ctx"] = {"state": MarketState(cfg), "virtual_now": None,
                  "bot_status": None}
    app["mode"] = mode

    async def start_background(app):
        app["workers"] = [asyncio.create_task(coro(app))
                          for coro in runner_coros]

    async def stop_background(app):
        for worker in app["workers"]:
            worker.cancel()

    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_static("/static", WEB_DIR)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true",
                        help="synthetic looping session, no API keys needed")
    parser.add_argument("--replay", metavar="FILE",
                        help="replay a recorded session JSON")
    parser.add_argument("--bot", action="store_true",
                        help="run the paper-trading bot alongside the live scan")
    parser.add_argument("--port", type=int, default=DEFAULT.port)
    args = parser.parse_args()
    cfg = DEFAULT

    if args.demo:
        mode = "demo"
        runners = [lambda app: playback_loop(app, cfg, regenerate=True)]
    elif args.replay:
        data = json.loads(pathlib.Path(args.replay).read_text(encoding="utf-8"))
        mode = "replay"
        runners = [lambda app: playback_loop(app, cfg, session_data=data)]
    else:
        mode = "live"
        runners = [lambda app: live_loop(app, cfg)]
        if args.bot:
            runners.append(lambda app: bot_loop(app, cfg))
            print("[bot] paper-trading bot enabled "
                  f"(max {cfg.bot_max_trades_per_day} trades/day, "
                  f"${cfg.bot_bankroll:,.0f} simulated bankroll)")

    app = build_app(cfg, mode, runners)
    print(f"[{mode}] dashboard -> http://{cfg.host}:{args.port}")
    web.run_app(app, host=cfg.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
