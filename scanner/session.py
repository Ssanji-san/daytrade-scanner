"""Headless cloud session: run the live scanner + bot with no web server.

    python -m scanner.session --until-et 12:15

Made for GitHub Actions: starts before the open, journals alerts, lets the
bot trade its window, writes docs/data/status.json for the Pages
dashboard, and exits at the ET cutoff. Bracket orders live on Alpaca's
servers, so exits keep working after this process ends; the flatten
workflow (scanner.flatten) is the 15:50 ET safety net.
"""
import argparse
import asyncio
import datetime as dt
import json
import pathlib
import traceback

from .config import DEFAULT, Config
from .main import live_loop
from .state import MarketState
from .trading.bot import bot_loop
from .trading.strategy import ET

STATUS_PATH = pathlib.Path("docs/data/status.json")
WRITE_SECONDS = 30


async def status_writer(ctx, cfg: Config):
    """Publish the dashboard snapshot every WRITE_SECONDS, come what may.

    One unhandled exception used to kill this task outright - report_death
    printed a line and the session then ran for hours behind a dashboard
    frozen at whatever it last wrote. The scanner and bot loops already
    survive their own errors; this one has to as well.
    """
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            payload = ctx["state"].payload(now)
            payload["mode"] = "cloud"
            payload["now"] = int(now.timestamp())
            payload["bot"] = ctx.get("bot_status")
            STATUS_PATH.write_text(json.dumps(payload, separators=(",", ":")),
                                   encoding="utf-8")
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(WRITE_SECONDS)


async def run_session(cfg: Config, until_et: str, with_bot=True):
    hour, minute = (int(p) for p in until_et.split(":"))
    now_et = dt.datetime.now(ET)
    cutoff = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cutoff <= now_et:
        print(f"[session] cutoff {until_et} ET already passed - nothing to do")
        return
    seconds = (cutoff - now_et).total_seconds()
    print(f"[session] running until {until_et} ET ({seconds/60:.0f} min)")

    app = {"ctx": {"state": MarketState(cfg), "virtual_now": None,
                   "bot_status": None}}
    tasks = [asyncio.create_task(live_loop(app, cfg), name="scanner"),
             asyncio.create_task(status_writer(app["ctx"], cfg), name="status")]
    if with_bot:
        tasks.append(asyncio.create_task(bot_loop(app, cfg), name="bot"))

    def report_death(task):
        if not task.cancelled() and task.exception():
            print(f"[session] TASK DIED: {task.get_name()}: {task.exception()!r}")
    for task in tasks:
        task.add_done_callback(report_death)
    try:
        await asyncio.sleep(seconds)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    print("[session] done")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--until-et", default="12:15",
                        help="stop at this ET time (HH:MM)")
    parser.add_argument("--no-bot", action="store_true",
                        help="scanner + status only, no trading")
    args = parser.parse_args()
    asyncio.run(run_session(DEFAULT, args.until_et, with_bot=not args.no_bot))


if __name__ == "__main__":
    main()
