"""L2 metric graph: registry, calculation engine, relationships, checkers, and tree view."""

from __future__ import annotations

from graph.checkers import CheckResult, check_breakdown, check_formula, check_graph
from graph.edges import GraphEdge
from graph.formulas import eval_formula, parse_formula
from graph.metric_graph import MetricGraph
from graph.registry import (
    ALLOWED_UNITS,
    DEFAULT_METRICS_FILE,
    MetricDefinition,
    MetricDefinitionError,
    MetricRegistry,
)
from graph.tree import build_tree, render_text

try:
    from graph.engine import MetricEngine, ValidationReport
except ImportError:  # duckdb optional when only using graph/checkers with a custom compute()
    MetricEngine = None  # type: ignore[assignment,misc]
    ValidationReport = None  # type: ignore[assignment,misc]

__all__ = [
    "ALLOWED_UNITS",
    "CheckResult",
    "DEFAULT_METRICS_FILE",
    "GraphEdge",
    "MetricDefinition",
    "MetricDefinitionError",
    "MetricEngine",
    "MetricGraph",
    "MetricRegistry",
    "ValidationReport",
    "build_tree",
    "check_breakdown",
    "check_formula",
    "check_graph",
    "eval_formula",
    "parse_formula",
    "render_text",
]
