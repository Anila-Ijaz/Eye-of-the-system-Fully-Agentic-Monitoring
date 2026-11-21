"""Run the demo.

    python -m demo                  # live dashboard: http://127.0.0.1:8050
    python -m demo --headless       # same scripted run in the terminal + scorecard
    python -m demo --manual         # dashboard without the script: inject from the page
    python -m demo --llm            # also use the LLM (needs LLM_BASE_URL, LLM_MODEL, LLM_API_KEY)
"""

from __future__ import annotations

import argparse
import sys
import time

from runtime.runner import AutonomousMonitor
from simulator.stream import LiveMarketplace

from .script import TIMELINE, describe_timeline, save_run, score, scorecard_text

SHOW = {"alert", "issue", "investigate", "triage", "inject", "metric", "watch", "system"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Eye of the System: autonomous monitoring demo.")
    ap.add_argument("--headless", action="store_true", help="run in the terminal and print a scorecard")
    ap.add_argument("--manual", action="store_true", help="no scripted timeline (inject from the dashboard)")
    ap.add_argument("--days", type=int, default=6, help="simulated live days (default 6)")
    ap.add_argument("--history-days", type=int, default=28)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--speed", type=float, default=0.0, help="dashboard: extra seconds between hours (default 0)")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--out", default="data/generated/demo_run", help="where reports and the scorecard go")
    ap.add_argument("--llm", action="store_true", help="LLM chooser + narrative (falls back to rules on any error)")
    ap.add_argument("--quiet", action="store_true", help="headless: only alerts and the scorecard")
    ap.add_argument("--no-browser", action="store_true", help="dashboard: don't open a browser tab")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("building the marketplace (history + live period)...", flush=True)
    live = LiveMarketplace(history_days=args.history_days, live_days=args.days, seed=args.seed)
    script = None if args.manual else TIMELINE
    if script:
        print("scripted timeline (the monitor is not told any of this):")
        for line in describe_timeline(script):
            print("   " + line)

    if args.headless:
        show = {"alert"} if args.quiet else SHOW

        def printer(item):
            if item.kind in show and (item.level != "info" or item.kind in ("inject", "metric", "system", "alert")):
                print(f"{item.time[5:16]}  {item.kind:11} {item.text}", flush=True)

        mon = AutonomousMonitor(live, use_llm=args.llm, on_feed=printer)
        t0 = time.perf_counter()
        mon.run(script=script)
        sc = score(mon)
        out = save_run(mon, args.out, sc)
        print(scorecard_text(sc))
        print(f"\nfinished {mon.state()['hours_done']} simulated hours in {time.perf_counter() - t0:.0f}s; "
              f"reports, alerts and the scorecard are in {out}")
        return 0

    from .server import serve

    serve(live, script=script, port=args.port, delay=args.speed, use_llm=args.llm, out_dir=args.out,
          open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
