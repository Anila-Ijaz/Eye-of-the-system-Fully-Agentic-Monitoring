# Eye of the System — Fully Agentic Monitoring

An autonomous monitoring agent for a simulated online marketplace.
It keeps a **graph of business metrics and how they relate**, watches them continuously,
spots meaningful changes, investigates them by following the graph, and writes up
what it found with the evidence.

Built for the Almedia track "Eye of the System: Fully Agentic Monitoring".

## Planned layers

1. **Simulator**: mock marketplace data (orders, traffic, checkout, payments, ads, inventory, search, recommendations) with faults you can inject and innocent changes.
2. **Metric graph**: metrics as nodes; edges with defined meanings (formula, breakdown by dimension, possible cause).
3. **Graph extension**: an agent that adds a new metric to the graph and checks its connections.
4. **Monitoring**: expected behaviour (seasonality-aware), data-completeness checks, planned events, change detection.
5. **Investigation**: an autonomous loop that picks the next query, follows the graph, drills into segments and ranks competing explanations.
6. **Reporting**: alerts with numbers, an investigation trail, uncertainty, and issue tracking (persists / resolved / deduplicated).
7. **Demo UI**: shows the graph, the agent's decisions, the evidence and the final report.

## Status

- L1 baseline generator: done ([simulator/README.md](simulator/README.md)).
- Everything else: not started.
