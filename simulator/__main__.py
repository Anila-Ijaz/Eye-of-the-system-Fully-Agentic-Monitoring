"""Command line: python -m simulator --days 28 --out data/generated/baseline"""

from __future__ import annotations

import argparse
import time

from .config import SimConfig
from .generator import generate


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Generate baseline marketplace data (no faults).")
    ap.add_argument("--start", default=SimConfig.start, help="first day, YYYY-MM-DD (UTC)")
    ap.add_argument("--days", type=int, default=SimConfig.days)
    ap.add_argument("--seed", type=int, default=SimConfig.seed)
    ap.add_argument("--out", default="data/generated/baseline")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    result = generate(SimConfig(start=args.start, days=args.days, seed=args.seed))
    out = result.write(args.out)
    h = result.marketplace_hourly
    daily = h.groupby(h["timestamp"].dt.floor("D"))[["sessions", "orders", "revenue"]].sum()
    print(f"wrote {len(h):,} rows to {out} in {time.perf_counter() - t0:.1f}s")
    print(f"per day: {daily['sessions'].mean():,.0f} sessions, {daily['orders'].mean():,.0f} orders, "
          f"EUR {daily['revenue'].mean():,.0f} revenue")
    print(f"conversion {daily['orders'].sum() / daily['sessions'].sum():.2%}; "
          f"purchase orders: {len(result.world.purchase_orders):,}; "
          f"out-of-stock product views: {h['out_of_stock_views'].sum():,}")


if __name__ == "__main__":
    main()
