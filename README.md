# Eye of the System: Fully Agentic Monitoring

An autonomous monitoring system for a simulated online marketplace. It keeps a
**graph of business metrics and how they relate**, watches them hour by hour,
decides on its own what is a real problem, investigates it by following the
graph, and writes up what it found, with the evidence and what is still
uncertain. No person asks it questions or guides an investigation.

Built for the Almedia track "Eye of the System: Fully Agentic Monitoring".

## Run the demo

```bash
pip install -r requirements.txt
python -m demo              # live dashboard, opens http://127.0.0.1:8050
python -m demo --headless   # the same run in the terminal, then a scorecard
```

The demo builds 4 weeks of marketplace history, then streams 6 simulated days,
about one simulated hour per second. A scripted timeline injects faults and
innocent changes while it runs, and adds a new metric halfway. The monitor
isn't told any of this. On the dashboard you can also inject any scenario or
add a metric yourself, and pause, step or change the speed.

| Time | What happens (hidden from the monitor) |
|---|---|
| +3 h | payment failure on Android in Germany, right after a checkout release |
| +8 h | planned promo in the UK (+25%, on the calendar) |
| +30 h | late data for Germany/iOS (rows arrive hours late) |
| +36 h | a new metric arrives: `orders_per_search`, with only a definition |
| +50 h | supplier stock-out in electronics |
| +76 h | a search release breaks German search results |
| +100 h | ad budget spent too fast in France; public holiday in the US |

Other options: `--manual` (no script; inject from the page), `--days N`,
`--speed 1` (a second per hour), `--llm` (see below), `--out DIR`.

### What the system reports (6-day run, seed 42)

| Scenario | Result |
|---|---|
| Payment failure DE/Android | found after 1 h; **confirmed** payment success rate in DE/Android; release named as the trigger; resolved |
| Planned promo UK | explained by the planned event; no incident |
| Late data DE/iOS | data-quality issue, business metrics held back; resolved by the backfill |
| Stock-out electronics | found after 2 h; root out-of-stock rate in electronics (a hypothesis: the graph only says "possible cause") |
| Search release DE | found after 1 h; **confirmed** search click-through in DE; release named; resolved |
| Ad budget FR | found after 1 h; root ad impressions in France (a hypothesis: no event explains the timing) |
| Holiday US | explained by the planned event; no incident |

That is 10 alerts in 6 days: one when each problem was opened, one when it
was resolved, and one when the stock-out's root cause sharpened. Hundreds of
anomalous signals were grouped into those issues. The run takes about
1 second per simulated hour. Reports, alerts, the feed and the scorecard are
saved to `data/generated/demo_run/`.

## How it works

Every simulated hour the runner ([runtime/runner.py](runtime/runner.py)) does this, without a person:

1. **Data.** The hour's rows arrive, plus late rows for earlier hours.
   Missing slices open a data-quality issue, and a backfill resolves it.
2. **Watchlist.** Once a day it picks what to watch from metric priorities
   (graph importance, business weight, noisiness), plus the root KPIs and the
   root causes of open issues.
3. **Detection.** Every watched metric × grouping (total, country,
   country × platform, category) × window (1 h, 6 h) is scored.
4. **Triage.** Each new anomaly is checked against data quality and the event
   calendar: held back, explained as planned, or reported.
5. **Issues.** Reported anomalies join the issue they belong to, or open one.
6. **Investigation.** The agent loop follows the graph, localizes the change,
   checks events, upstream causes and counterfactuals, and decides when to stop.
7. **Reports and alerts.** Alerts go out only when an issue opens, its root
   cause changes, it resolves, or it recurs.

| Layer | What it decides, and how | Code |
|---|---|---|
| L1 Simulator | hourly marketplace (4 countries × 3 platforms × 4 categories): funnel, ads, recommendations, product-level inventory; live mode with runtime injection | [simulator/](simulator/) |
| L2 Metric graph | 33 metrics; formula, breakdown and possible-cause edges, each checked on the data; DuckDB engine for any window/segment | [graph/](graph/) |
| L3 Extension | a new metric with only a definition: formula search on every hour and segment, seasonality-adjusted lagged correlation, validation, registration | [extension/](extension/), [graph/extend.py](graph/extend.py) |
| L4 Monitoring | expected = median of the same window 1–4 weeks earlier, with growth; range from the backtested error, never below count noise; z ≥ 5, persistence, completeness; triage against data quality and the calendar | [monitoring/](monitoring/) |
| L5 Investigation | Shapley contributions along formulas; drill-down with rate/mix effects (localized only if the rest stayed normal); hypotheses must be localized + timing-aligned + mechanism-confirmed + counterfactual-passed to be **confirmed**; symptoms ranked after causes | [investigation/](investigation/), [agent/](agent/) |
| L6 Reporting | issues by root cause (merge, recurrence, quiet tracking of low-impact noise); report with numbers, impact, evidence, uncertainty, actions, trail | [reporting/](reporting/) |
| L7 Demo | live dashboard; scripted timeline; scorecard against the ground truth | [demo/](demo/) |

Only the scorecard reads the simulator's ground truth. The monitor sees rows,
the event calendar (release log, planned campaigns) and the metric graph.

### The model at the decision points (Claude)

When `ANTHROPIC_API_KEY` and `LLM_MODEL` are set, Claude is the primary
chooser of investigation steps and writes the report summaries. No flag is
needed; `--no-llm` turns it off. Keys and model ids come only from the
environment. Never commit them.

```bash
export ANTHROPIC_API_KEY=...          # read by the anthropic SDK; never printed or logged
export LLM_MODEL=claude-opus-5-5      # chooses the next step
export LLM_REPORT_MODEL=...           # writes report text (optional, default LLM_MODEL)
python -m demo --headless
```

| The model decides | Code decides |
|---|---|
| which menu step runs next, and why (at most two sentences, citing evidence) | the menu, every number, hypothesis status and confidence, the confirmed/uncertain label, when to stop |
| the report summary text | the structured finding |

- **Step choice.** A reply is used only if it names a number on the menu and
  gives a reason that cites evidence from the prompt. Anything else falls back
  to the rule-based chooser for that step: invalid JSON, an off-menu number,
  an empty or evidence-free reason, an error, or no key. The model has no stop
  option. A stop request is ignored and recorded as `llm_stop_ignored`. Each
  trail step records `chosen_by` (`llm` or `fallback`) and the reason or the
  fallback cause. The dashboard shows "model-chosen steps: X of Y".
- **Report text.** Every number, percentage and euro amount in the model's text
  must appear in the structured finding (rounding allowed). The text may not
  call an unconfirmed cause confirmed. Otherwise the template text is used and
  the reason is recorded.
- **Cache.** `--llm-cache-mode record|replay|live` (or `LLM_CACHE_MODE`), stored
  in `--llm-cache-dir` (or `LLM_CACHE_DIR`, default `data/llm_cache`). The key
  is a hash of the model and the exact prompt. Replay needs no key and no
  network, and it reproduces the recorded trails and reports exactly.
- **Checks you run with a key:** `python scripts/live_check_llm.py` (one call,
  then record and replay of the fault-window investigation), and
  `python scripts/llm_scoreboard.py --n 4` (injected faults with the model
  versus the fallback only).

## Layer by layer

```bash
python -m simulator --scenarios all                               # L1: data with scenarios
python -m graph --data data/generated/scenarios                   # L2: check every graph edge
python -m extension --metric extension/examples/new_metrics.yaml --apply   # L3: add new metrics
python -m monitoring.detect                                       # L4: anomalies, last 7 days
python -m investigation.investigate --metric orders --segment country=DE   # L5: investigate
python -m pytest                                                  # all tests
```

Each folder has its own write-up: [simulator/README.md](simulator/README.md),
[graph/README.md](graph/README.md), [extension/README.md](extension/README.md),
[monitoring/DETECTION.md](monitoring/DETECTION.md),
[investigation/INVESTIGATION.md](investigation/INVESTIGATION.md).
