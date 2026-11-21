"""L3: extending the metric graph with new metrics.

Person A (discovery.py): find candidate connections for a new metric.
Person B: validate candidates, score them, and add the metric to the graph.
"""

from .discovery import Candidate, DiscoveryReport, discover_connections

__all__ = ["Candidate", "DiscoveryReport", "discover_connections"]
