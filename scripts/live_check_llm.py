"""Standalone live check for ChatClient + LLMChooser record/replay (run manually).

Usage:
    export LLM_BASE_URL=...
    export LLM_MODEL=...
    export LLM_API_KEY=...
    python3 scripts/live_check_llm.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

from agent.chooser import LLMChooser
from agent.llm_client import ChatClient
from agent.loop import Limits, investigate
from agent.loop import RealInvestigationTools
from graph.engine import MetricEngine
from graph.metric_graph import MetricGraph
from graph.registry import MetricRegistry
from monitoring.data_quality import check_dq
from monitoring.detect import Detector
from monitoring.event_calendar import EventCalendar


def main() -> int:
    missing = [k for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY") if not os.environ.get(k, "").strip()]
    if missing:
        print(f"ERROR: Missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        return 1

    try:
        client = ChatClient()
        print("1) Running tiny ChatClient test call...")
        parsed = client.complete_json(
            'Return JSON only, e.g. {"ok": true}',
            'Reply with {"ok": true}',
        )
        print("   Parsed JSON:", parsed)
    except Exception as exc:
        print(f"ERROR: ChatClient call failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    data_dir = Path("data/generated/scenarios")
    if not data_dir.exists():
        print(f"ERROR: {data_dir} not found", file=sys.stderr)
        return 1

    reg = MetricRegistry.load()
    eng = MetricEngine.from_dir(data_dir, reg)
    mg = MetricGraph.load("graph/seed_graph.yaml")
    for name in reg.names():
        mg.graph.add_node(name)
    for edge in mg.edges():
        if edge.type in {"formula", "breakdown"}:
            edge.status = "confirmed"

    mh = pd.read_csv(data_dir / "marketplace_hourly.csv", parse_dates=["timestamp"])
    ev = pd.read_csv(data_dir / "events.csv")
    cal = EventCalendar.from_dataframe(ev)
    det = Detector(eng)
    eps = det.detect(["orders"], by=["country"], start="2026-10-22T10:00:00Z", end="2026-10-23T10:00:00Z")
    de_ep = [e for e in eps if e.segment.get("country") == "DE"]
    if not de_ep:
        print("ERROR: No DE orders anomaly found in simulated data", file=sys.stderr)
        return 1

    expected_slices = {
        "country": sorted(mh["country"].unique()),
        "platform": sorted(mh["platform"].unique()),
        "category": sorted(mh["category"].unique()),
    }
    dq_fn = lambda s, e, seg: check_dq(mh, s, e, now=e, expected_slices=expected_slices, segment=seg)
    tools = RealInvestigationTools(eng, mg, detector=det)

    with tempfile.TemporaryDirectory() as tmpdir:
        rec_chooser = LLMChooser(client=client, cache_dir=tmpdir, mode="record")
        f_rec = investigate(de_ep[0], mg, tools, cal, dq_fn, chooser=rec_chooser, limits=Limits())

        rep_chooser = LLMChooser(client=None, cache_dir=tmpdir, mode="replay", model_name=client.model)
        f_rep = investigate(de_ep[0], mg, tools, cal, dq_fn, chooser=rep_chooser, limits=Limits())

        print("2) Record trail:", f_rec["trail"])
        print("3) Replay trail:", f_rep["trail"])
        print("4) Trails identical:", f_rec["trail"] == f_rep["trail"])
        print("5) Fallbacks:", sum("fallback_used" in t["reason"] for t in f_rec["trail"]))
        for t in f_rec["trail"]:
            if "fallback_used" in t["reason"]:
                print("   Fallback reason:", t["reason"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
