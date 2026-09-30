"""App entrypoint: web server + background market loop.

    python -m scanner.main            # live (needs ALPACA_KEY / ALPACA_SECRET)
    python -m scanner.main --demo     # synthetic looping session, no keys needed
    python -m scanner.main --replay fixtures/recorded-session.json

Dashboard at http://127.0.0.1:8124
"""
import argparse
import asyncio
import datetime as dt
import hashlib
import json
import pathlib
import traceback

import aiohttp
from aiohttp import web

from .alpaca import AlpacaClient
from .calendar_feed import filter_events
from .config import DEFAULT, Config
from .countries import CountryCache, fetch_country
from .demo import build_demo_bot_status, build_demo_session
from .floats import FloatCache, fetch_shares, fetch_ticker_map
from .history import ET
from .state import MarketState
from .trading.bot import bot_loop
from .volume import sip_cutoff

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"
FLOAT_FETCHES_PER_CYCLE = 4
ASSETS = ("style.css", "app.js")


def stamp_assets(html, web_dir=WEB_DIR):
    """Version the asset URLs with a hash of their contents.

    Neither this server nor GitHub Pages sends cache headers, and a browser
    holding an old app.js keeps rendering claims the code no longer makes -
    a dashboard describing a strategy the bot has stopped trading is a bug
    this project has already shipped twice. A changed file changes its URL,
    so the stale copy cannot be reused.
    """
    for name in ASSETS:
        path = web_dir / name
        if not path.exists():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
        html = html.replace(f"static/{name}", f"static/{name}?v={digest}")
    return html


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- live loop

async def discover_news(client, state, news_seen, ticker_map, since, now):
    """Pull the market's headlines since `since` (epoch); returns the next.

    Every common stock tagged in one joins `news_seen` - the headline puts
    it on the candidate list, it still has to pass every gate to trade.
    """
    items = await client.market_news(
        dt.datetime.fromtimestamp(since, dt.timezone.utc).isoformat())
    # Advance past everything read, stocks or not, or the next call reads
    # the same ETF headlines again.
    since = max([since] + [i["ts"] for i in items])
    items = [i for i in items if i["symbol"] in ticker_map]
    state.set_news(now, items)
    for i in items:
        news_seen[i["symbol"]] = max(news_seen.get(i["symbol"], 0), i["ts"])
    return since


def news_candidates(news_seen, now, cfg: Config, prices=None):
    """Symbols whose newest headline is young enough to still be tracked.

    News finds every megacap with a headline, and each one costs snapshot
    and news calls against a 200-a-minute limit. A stock last seen at under
    half the band floor or over twice its ceiling is dropped; one never
    priced yet is kept until its first snapshot says otherwise.
    """
    horizon = now.timestamp() - cfg.news_candidate_minutes * 60
    ceiling = max(cfg.hod_observe_max_price or 0, cfg.hod_max_price)
    low, high = cfg.hod_min_price / 2, ceiling * 2
    prices = prices or {}

    def plausible(sym):
        price = prices.get(sym)
        return price is None or low <= price <= high

    return sorted(s for s, ts in news_seen.items()
                  if ts >= horizon and plausible(s))


def watchlist(candidates, now, cfg: Config, held=()):
    """The symbols to snapshot: seen within candidate_ttl_minutes, plus
    every symbol the bot holds. A held stock that has left the screener
    lists still needs its bars - the candle exit is read off them."""
    ttl = dt.timedelta(minutes=cfg.candidate_ttl_minutes)
    out = {s: t for s, t in candidates.items() if now - t < ttl}
    out.update({s: now for s in held})
    return out


async def refresh_sip(client, state, symbols, sip_until, now):
    """Read each symbol's real tape up to the free-data cutoff.

    Incremental: a symbol continues from where its last read stopped, and a
    new one starts at 04:00 ET, when the extended session opens. Every
    symbol asked about gets a reading, empty or not - an empty answer means
    nothing traded, which is not the same as not knowing.
    """
    cutoff = sip_cutoff(now)
    day_start = now.astimezone(ET).replace(hour=4, minute=0, second=0,
                                           microsecond=0)
    groups = {}
    for sym in symbols:
        groups.setdefault(sip_until.get(sym, day_start), []).append(sym)
    for start, syms in groups.items():
        rows = {}
        if start < cutoff:
            rows = await client.bars(syms, "1Min", start.isoformat(),
                                     end=cutoff.isoformat(), feed="sip")
        for sym in syms:
            state.add_sip(sym, rows.get(sym, []), until=cutoff)
            sip_until[sym] = cutoff


async def live_loop(app, cfg: Config):
    state: MarketState = app["ctx"]["state"]
    async with aiohttp.ClientSession() as session:
        client = AlpacaClient(session, cfg)
        float_cache = FloatCache(cfg)
        country_cache = CountryCache(cfg)
        ticker_map = {}
        candidates = {}   # symbol -> last time it appeared on a screener list
        avg_volumes = {}
        last_news = last_calendar = last_discovery = last_sip = 0.0
        news_seen = {}    # symbol -> newest headline epoch, whole market
        news_since = int(utcnow().timestamp() - cfg.news_candidate_minutes * 60)
        sip_until = {}    # symbol -> how far its real tape has been read
        last_price = {}   # symbol -> last snapshot price, to prune news
        ceiling = max(cfg.hod_observe_max_price or 0, cfg.hod_max_price)

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
                if now.timestamp() - last_discovery > cfg.news_discovery_seconds:
                    try:
                        news_since = await discover_news(
                            client, state, news_seen, ticker_map, news_since,
                            now)
                    except Exception as exc:     # never cost the scan a cycle
                        print(f"[warn] news discovery failed: {exc}")
                    last_discovery = now.timestamp()
                for sym in news_candidates(news_seen, now, cfg, last_price):
                    candidates[sym] = now
                candidates = watchlist(candidates, now, cfg,
                                       app["ctx"].get("held", ()))

                snaps = await client.snapshots(list(candidates))
                last_price.update({s: d.get("price") for s, d in snaps.items()})

                new_syms = [s for s in snaps if s not in avg_volumes]
                if new_syms:
                    avg_volumes.update(await client.avg_volumes(new_syms))

                # SEC lookups and the real tape only for stocks the scan can
                # show: news discovery brings in every megacap with a headline.
                in_band = [s for s, d in snaps.items()
                           if cfg.hod_min_price <= (d.get("price") or 0)
                           <= ceiling]
                to_fetch = [s for s in in_band if float_cache.is_stale(s)
                            and s in ticker_map][:FLOAT_FETCHES_PER_CYCLE]
                for sym in to_fetch:
                    shares, answered = await fetch_shares(
                        session, ticker_map[sym])
                    float_cache.put(sym, shares, answered=answered)
                    await asyncio.sleep(0.15)   # stay polite with SEC

                # Where each company operates: Chinese stocks need breaking
                # news (hod.py). Same budget and pacing as the floats.
                to_locate = [s for s in in_band if country_cache.is_stale(s)
                             and s in ticker_map][:FLOAT_FETCHES_PER_CYCLE]
                for sym in to_locate:
                    country, answered = await fetch_country(
                        session, ticker_map[sym])
                    country_cache.put(sym, country, answered=answered)
                    await asyncio.sleep(0.15)

                for sym, data in snaps.items():
                    data["avg_volume"] = avg_volumes.get(sym)
                    data["float_shares"] = float_cache.get(sym)
                    data["country"] = country_cache.get(sym)
                state.ingest(now, snaps)
                if now.timestamp() - last_sip >= cfg.sip_poll_seconds:
                    await refresh_sip(client, state, in_band, sip_until, now)
                    last_sip = now.timestamp()

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

def _ingest_frame(state, now, symbols):
    """A recording carries no SIP tape, so its own cumulative volume stands
    in for it: the whole tape in the demo, and in a recorded live session
    the IEX count - a floor, like everything real_volume reports."""
    state.ingest(now, symbols)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for sym, data in symbols.items():
        state.add_sip(sym, [{"t": start.isoformat(),
                             "v": data.get("cum_volume") or 0}], until=now)


async def playback_loop(app, cfg: Config, session_data=None, regenerate=False):
    """Feed a recorded/synthetic session through the same pipeline, looping."""
    ctx = app["ctx"]
    while True:
        data = build_demo_session(cfg) if regenerate else session_data
        state = MarketState(cfg)
        frames = data["frames"]
        # Backfill everything except the tail instantly, then tick the last
        # frames in real time so the user sees the dashboard move.
        if not frames:
            print("[playback] recording has no frames")
            return
        # A recording of ten frames or fewer leaves nothing to backfill, and
        # `now` then has to come from the data rather than the loop below.
        now = dt.datetime.fromtimestamp(frames[0]["ts"], dt.timezone.utc)
        tail = min(10, len(frames))
        for frame in frames[:-tail]:
            now = dt.datetime.fromtimestamp(frame["ts"], dt.timezone.utc)
            _ingest_frame(state, now, frame["symbols"])
        state.set_news(now, data["news"])
        state.set_calendar(filter_events(data["calendar_events"], cfg))
        ctx["state"] = state
        if regenerate:   # demo mode also fakes the bot panel
            ctx["bot_status"] = build_demo_bot_status(cfg)
        for frame in frames[-tail:]:
            now = dt.datetime.fromtimestamp(frame["ts"], dt.timezone.utc)
            _ingest_frame(state, now, frame["symbols"])
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
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    # The page always revalidates; the assets it points at are versioned by
    # content hash, so they can be cached hard without ever going stale.
    return web.Response(text=stamp_assets(html), content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


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
