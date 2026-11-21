# Work Plan: Anila & Ayesha

Every layer is split into two halves. **Person A = Anila, Person B = Ayesha** (swap any row if you prefer).
Rough rule for the split: **A builds the "numbers" side** (data, statistics, calculations) and
**B builds the "logic" side** (rules, decisions, state). You both work on the same layer at the
same time, so you finish each layer together and can test it end to end before moving on.

---

## Step 0: Agree on the contracts (together, before writing code)

If these four formats are fixed first, you can work in parallel without waiting for each other.
Put them in `contracts/` and don't change them without telling the other person.

| Contract | What it describes | Produced by → used by |
|---|---|---|
| **Data tables** | Columns of the simulated tables (e.g. `timestamp, country, platform, category, sessions, searches, orders, ...`) plus an `events` table (releases, campaigns, data delays) | L1 → everything |
| **Metric definition** | `name, description, unit, calculation/query, dimensions` (YAML) | L2/L3 |
| **Graph edge** | `from, to, type (formula / breakdown / possible_cause), formula?, dimension?, confidence, validated?` | L2/L3 → L5 |
| **Finding / Alert** | `metric, segment, time window, expected vs actual, size of change, evidence list, trail, status` | L4/L5 → L6/L7 |

Suggested stack: Python, pandas, networkx (graph), DuckDB or SQLite (queries), pytest (tests),
Streamlit (demo UI), optional LLM for proposing connections and writing reports.

Suggested folders:

```
contracts/      simulator/      graph/        extension/
monitoring/     investigation/  reporting/    demo/        tests/
```

---

## Layer-by-layer split

### L1: Simulator (mock marketplace)
| Anila (A) | Ayesha (B) |
|---|---|
| **Baseline data generator:** products, sellers, categories, countries, platforms; the funnel traffic → searches → clicks → cart → checkout → payment → orders; ads, inventory, recommendations; daily/weekly seasonality and noise. | **Scenario injector:** faults (payment failure on Android in DE, bad search release for DE, stock-outs, ad budget spent too fast) and innocent changes (planned promo, holiday, late/missing data). Writes the `events` table and a **ground-truth file** saying what was injected. |

**Test together:** generate 4 weeks of normal data and 1 week with a fault; plot the funnel and check that the fault is visible.

### L2: Metric graph
| Anila (A) | Ayesha (B) |
|---|---|
| **Metric registry + calculation engine:** load metric definitions and compute any metric for any time window and any segment (e.g. `orders` for `country=DE, platform=android`). | **Relationships:** the three edge types, the seed graph for the marketplace, a **formula checker** (does `orders = checkouts × payment_success_rate` hold on the data?), a **breakdown checker** (do the country values add up to the total?) and the tree view for browsing. |

**Test together:** every formula and breakdown edge in the seed graph passes its check on clean data.

### L3: Extending the graph automatically
| Anila (A) | Ayesha (B) |
|---|---|
| **Finding candidate connections** for a new metric: read its definition, match shared columns/dimensions, try simple formulas (ratio, product, sum), look for correlated metrics (with lag); optionally ask an LLM for suggestions. | **Validation + adding it:** test every candidate on the data, give it a confidence score, label unclear ones as `possible_cause` (a hypothesis, not a fact), add the metric to the graph and register it for monitoring. |

**Test together:** add a metric the graph doesn't know (e.g. `refund_rate`) with only a definition, and check it gets the right place in the graph.

### L4: Monitoring
| Anila (A) | Ayesha (B) |
|---|---|
| **Expected behaviour + detection:** a seasonal baseline per metric and segment (e.g. same hour in previous weeks), a prediction range, and a change score. | **What to watch + data quality:** priority score for metrics/segments (importance in the graph, business impact, how noisy it is), freshness and completeness checks, and a planned-events calendar that explains changes that are expected. |

**Test together:** the fault is detected; the planned promo and the late data are **not** reported as incidents.

### L5: Investigation (the core)
| Anila (A) | Ayesha (B) |
|---|---|
| **Following the graph + quantifying:** walk formula edges down, measure how much each component contributed to the change, and drill into dimensions to find the segment that explains most of it (e.g. Android + DE). | **Hypotheses + the agent loop:** turn cause edges and events (releases, data delays) into hypotheses, test them, rank competing explanations, separate symptoms from the first cause, **decide when to stop**, and run the loop that picks the next query. |

**Test together:** for each injected fault, the top explanation matches the ground-truth file.

### L6: Reporting
| Anila (A) | Ayesha (B) |
|---|---|
| **Report writer:** alert with numbers, business impact (e.g. lost orders/revenue), investigation trail, what is still uncertain; an LLM can turn the structured findings into readable text. | **Issue tracking:** give each issue a fingerprint, so the same problem isn't reported twice; track the status (new → ongoing → resolved); keep an alert history. |

**Test together:** run 3 days with an ongoing fault and check there is 1 alert with updates, not 3 separate alerts.

### L7: Demo
| Anila (A) | Ayesha (B) |
|---|---|
| **UI:** graph view, tree browser, live feed of the agent's decisions, report page. | **Demo script + scoring:** one command that starts the simulator, injects a fault and an innocent change, adds a new metric and lets the system respond; compares results against the ground truth. |

**Test together:** full rehearsal of the demo from a clean start.

---

## How you work together on GitHub

1. **Never commit directly to `main`.** Each task gets its own branch:
   `anila/l1-generator`, `ayesha/l1-scenarios`, etc.
2. Before starting work each day: `git checkout main && git pull`, then
   `git checkout -b <your-branch>` (or `git merge main` into your existing branch).
3. Push your branch and open a **Pull Request**. The **other person** pulls the branch,
   runs `pytest` and the layer test, and approves before it is merged.
4. Every piece of code comes with at least one test in `tests/`.
5. Only touch your own folder/files where possible; changes to `contracts/` are discussed first.
6. After a layer is merged, run the joint "Test together" check above before starting the next layer.
