"""One call that builds a full L1 dataset: baseline + scenarios + ground truth.

Used by the CLI (``python -m simulator --scenarios all``) and meant to be reused
by the L7 demo script.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import pandas as pd

from .config import SimConfig
from .generator import SimulationResult, generate
from .scenarios import (
    ScenarioSpec,
    build_inventory_hooks,
    default_fault_scenarios,
    default_innocent_scenarios,
    inject_scenarios,
)

ScenarioSet = Literal["none", "faults", "innocent", "all"]

# The default scenario suites run up to ~4.6 days after the anchor.
_SUITE_SPAN = pd.Timedelta(days=5)


def default_anchor(cfg: SimConfig) -> pd.Timestamp:
    """10:00 UTC on the first day of the last week, so there is history before it."""
    return pd.Timestamp(cfg.start, tz="UTC") + pd.Timedelta(days=cfg.days - 7, hours=10)


def select_scenarios(which: ScenarioSet, anchor: pd.Timestamp) -> list[ScenarioSpec]:
    if which == "none":
        return []
    out: list[ScenarioSpec] = []
    if which in ("faults", "all"):
        out += default_fault_scenarios(anchor)
    if which in ("innocent", "all"):
        out += default_innocent_scenarios(anchor)
    return out


def build_dataset(
    cfg: Optional[SimConfig] = None,
    scenarios: ScenarioSet | list[ScenarioSpec] = "all",
    anchor: Optional[pd.Timestamp | str] = None,
    out_dir: Optional[str | Path] = None,
) -> tuple[SimulationResult, dict]:
    """Generate the baseline, apply the scenarios and (optionally) write everything.

    Stock-out scenarios are produced through ``InventoryHooks`` (late supplier
    deliveries), so ``inventory_daily`` and ``purchase_orders`` hold the real
    evidence; all other scenarios are applied to the hourly table.

    Returns ``(result, ground_truth)``. With ``out_dir`` the contract tables,
    the supporting tables and ``ground_truth.json`` are written there.
    """
    cfg = cfg or SimConfig()
    anchor_ts = pd.Timestamp(anchor) if anchor is not None else default_anchor(cfg)
    if anchor_ts.tzinfo is None:
        anchor_ts = anchor_ts.tz_localize("UTC")

    specs = select_scenarios(scenarios, anchor_ts) if isinstance(scenarios, str) else list(scenarios)
    if specs:
        start = pd.Timestamp(cfg.start, tz="UTC")
        end = start + pd.Timedelta(days=cfg.days)
        first = min(s.start_time for s in specs)
        last = max(s.end_time for s in specs)
        if pd.Timestamp(first) < start + pd.Timedelta(days=7):
            raise ValueError("scenarios need at least 7 days of normal history before them")
        if pd.Timestamp(last) >= end:
            raise ValueError(
                f"scenarios end at {last}, after the simulated period ({end}); "
                f"use more --days or an earlier --anchor (default suites span ~{_SUITE_SPAN.days} days)"
            )

    result = generate(cfg, inventory_hooks=build_inventory_hooks(specs))
    truth_path = Path(out_dir) / "ground_truth.json" if out_dir is not None else None
    hourly, events, truth = inject_scenarios(
        result.marketplace_hourly, specs, ground_truth_path=truth_path, apply_stock_out_to_df=False
    )
    result.marketplace_hourly = hourly
    result.events = events
    if out_dir is not None:
        result.write(out_dir)
    return result, truth
