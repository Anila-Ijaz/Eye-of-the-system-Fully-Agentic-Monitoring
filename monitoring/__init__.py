"""L4 monitoring.

Person A: expected behaviour and change detection
    monitoring.baseline  seasonal baseline, prediction range (backtested)
    monitoring.detect    change scores, anomalies, episodes; python -m monitoring.detect
Person B: what to watch, data quality, planned events

Import from the submodules (``from monitoring.detect import Detector``). This
file stays free of imports so both halves can add modules without touching it.
"""
