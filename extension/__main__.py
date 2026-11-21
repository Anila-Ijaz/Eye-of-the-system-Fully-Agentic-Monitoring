"""Propose graph connections for new metric definitions.

    python -m extension --metric extension/examples/new_metrics.yaml
    python -m extension --metric my_metric.yaml --data data/generated/scenarios --save data/generated/candidates.yaml

Prints the candidates for each metric. ``--save`` writes all candidate edges
in the graph YAML format (status "uncertain", source "auto_discovered") for
the validation step.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from graph.__main__ import DEFAULT_GRAPH
from graph.engine import MetricEngine
from graph.metric_graph import MetricGraph
from graph.registry import DEFAULT_METRICS_FILE, MetricDefinition, MetricDefinitionError, MetricRegistry

from .discovery import discover_connections


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find candidate graph connections for new metrics.")
    ap.add_argument("--metric", required=True, help="YAML file with one or more new metric definitions")
    ap.add_argument("--data", default="data/generated/scenarios", help="simulator output folder")
    ap.add_argument("--metrics", default=str(DEFAULT_METRICS_FILE), help="existing metric definitions")
    ap.add_argument("--graph", default=str(DEFAULT_GRAPH), help="existing graph edges")
    ap.add_argument("--max-lag", type=int, default=6, help="hours of lead/lag to test in correlations")
    ap.add_argument("--save", default=None, help="write candidate edges (graph YAML format) here")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    data = Path(args.data)
    if not (data / "marketplace_hourly.csv").exists():
        print(f"no data in {data}. Generate it first:  python -m simulator --scenarios all --out {data}",
              file=sys.stderr)
        return 2
    try:
        raw = yaml.safe_load(Path(args.metric).read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else [raw]
        new = [MetricDefinition.from_dict(i) for i in items]
    except (OSError, yaml.YAMLError, MetricDefinitionError) as exc:
        print(f"cannot read {args.metric}: {exc}", file=sys.stderr)
        return 2

    engine = MetricEngine.from_dir(data, MetricRegistry.load(args.metrics))
    graph = MetricGraph.load(args.graph)
    all_edges = []
    for defn in new:
        rep = discover_connections(defn, engine, graph, max_lag=args.max_lag)
        print(f"\n=== {defn.name}: {defn.description}")
        if not rep.validation.ok:
            print("  definition does not work on the data: " + "; ".join(rep.validation.errors))
            continue
        for w in rep.validation.warnings:
            print(f"  warning: {w}")
        for n in rep.notes:
            print(f"  note: {n}")
        if rep.related_metrics:
            print("  related by columns: " + ", ".join(f"{n} ({s:.2f})" for n, s in rep.related_metrics[:5]))
        for method, title in [("formula", "formulas"), ("correlation", "possible causes"), ("breakdown", "breakdowns")]:
            cands = rep.by_method(method)
            if not cands:
                continue
            print(f"  {title}:")
            for c in cands:
                extra = f"  [needs {c.details['not_in_graph']} in graph]" if c.details.get("not_in_graph") else ""
                print(f"    {c.summary}{extra}")
                if method != "breakdown":
                    print(f"        {c.evidence}")
        all_edges += rep.edges()

    if args.save:
        out = Path(args.save)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump([e.to_dict() for e in all_edges], sort_keys=False), encoding="utf-8")
        print(f"\nsaved {len(all_edges)} candidate edges to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
