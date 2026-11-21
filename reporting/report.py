"""Report writer: the story of an issue, with its evidence (L6, Person A).

    report = build_report(issue, tracker)          # dict: title, severity, sections, markdown
    report = build_report(issue, tracker, narrator=LLMNarrator())   # optional LLM narrative

Every number in a report comes from the investigation or the detector. The
optional LLM narrator only rewrites the summary in plain language: it gets the
facts as JSON, and its text is rejected (the template is kept) if it contains a
number that isn't among those facts.

Recommended actions are suggestions for a person. The system never acts on
the business itself (no rollbacks, no budget changes).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import pandas as pd

from .issues import Issue, IssueTracker, seg_key

SEVERITY_EUR = [(500_000, "critical"), (100_000, "high"), (20_000, "medium")]  # revenue lost per day

ACTIONS = {
    "payment_success_rate": "Check payment provider / gateway error rates for {seg}; if a release went out just before, roll it back.",
    "payment_attempt_rate": "Check the payment step of checkout for {seg} (errors, timeouts).",
    "checkout_rate": "Check the checkout flow for {seg} (errors, slow pages, a recent release).",
    "search_ctr": "Review the latest search ranking change for {seg}: compare result relevance before and after it.",
    "searches_per_session": "Check search entry points and traffic sources for {seg}.",
    "add_to_cart_rate": "Check product availability and product pages for {seg} (out-of-stock items, prices).",
    "out_of_stock_rate": "Check stock levels, supplier deliveries and open purchase orders for {seg}; restock the top sellers first.",
    "avg_active_listings": "Check stock levels and supplier deliveries for {seg}.",
    "ad_impressions": "Check campaign budget pacing and daily caps for {seg}: was the budget spent early in the day?",
    "ad_spend": "Check campaign budget pacing and bids for {seg}.",
    "ad_clicks": "Check campaign delivery and budget pacing for {seg}.",
    "cost_per_click": "Check bid changes for {seg}.",
    "recommendation_ctr": "Check the latest recommendation model change for {seg}.",
    "sessions": "Check traffic sources and marketing activity for {seg}.",
    "row_count": "Check the data pipeline for {seg}; numbers for this segment are incomplete until the backfill arrives.",
}


def severity(issue: Issue) -> str:
    """From the revenue being lost per day (projected from the investigated
    window), so a big failure is critical in its first hour, not only later."""
    if issue.kind == "data_quality":
        return "medium"
    f = issue.finding or {}
    impact = f.get("business_impact") or {}
    lost = max(float(impact.get("lost_revenue_eur") or 0.0), 0.0)
    tw = f.get("time_window") or {}
    try:
        hours = max(1.0, (pd.Timestamp(tw["end"]) - pd.Timestamp(tw["start"])) / pd.Timedelta(hours=1))
    except (KeyError, ValueError):
        hours = 24.0
    per_day = lost / hours * 24.0
    for limit, name in SEVERITY_EUR:
        if per_day >= limit:
            return name
    return "low"


def _seg(seg: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in seg.items()) or "all segments"


def _num(x: Any, digits: int = 0) -> str:
    try:
        return f"{float(x):,.{digits}f}"
    except (TypeError, ValueError):
        return "?"


def build_report(issue: Issue, tracker: IssueTracker, narrator: Optional["LLMNarrator"] = None) -> dict[str, Any]:
    f = issue.finding or {}
    root = issue.root or {}
    eps = [tracker.episodes[k] for k in issue.episodes if k in tracker.episodes]
    first = next((e for e in eps if e.metric == issue.metric), eps[0] if eps else None)
    sev = severity(issue)

    # --- what happened ------------------------------------------------------------------
    if issue.kind == "data_quality":
        dd = issue.data_detail
        title = f"Data incomplete for {_seg(issue.segment)}"
        headline = (f"{dd.get('missing_rows', 0)} rows for {_seg(issue.segment)} did not arrive on time "
                    f"({len(dd.get('pending_hours', [])) or 'all'} hour(s) still pending)."
                    if issue.open else
                    f"Rows for {_seg(issue.segment)} arrived late; they were backfilled at {dd.get('backfilled_at', '?')}.")
        what = [headline, "Metrics for this segment were held back as incomplete data, not reported as business changes."]
    else:
        rel = (first.actual / first.expected - 1) if first and first.expected else float("nan")
        what = [f"{issue.metric} {'fell' if first and first.direction == 'down' else 'rose'} "
                f"{abs(100 * rel):.1f}% in {_seg(issue.segment)}: {_num(first.actual, 2)} vs an expected "
                f"{_num(first.expected, 2)} (normal range {_num(first.lower, 2)}–{_num(first.upper, 2)}), "
                f"peak score z={first.peak_z:.1f}."] if first else []
        if root:
            share = root.get("explained_share", 0.0)
            confirmed = root.get("status") == "confirmed"
            rseg = root.get("segment") or {}
            word = "down" if first and first.direction == "down" else "up"
            pct = f" {abs(100 * rel):.0f}%" if rel == rel else ""
            same = root.get("node") == issue.metric
            title = f"{issue.metric} {word}{pct} in {_seg(issue.segment)}" + (
                "" if same and rseg == issue.segment else f", driven by {root.get('node')} in {_seg(rseg)}")
            src = root.get("source")
            if src == "segment":
                what_cause = f"the change in {root.get('node')} is confined to {_seg(rseg)}"
            elif src == "graph_cause_edge":
                what_cause = f"{root.get('cause')} in {_seg(rseg)}"
            else:
                what_cause = f"{root.get('node')} in {_seg(rseg)}"
            headline = (f"{'Confirmed' if confirmed else 'Most likely (not yet confirmed)'}: {what_cause}"
                        + (f" ({100 * share:.0f}% of the change)." if share else "."))
            trig = _trigger(f, rseg)
            if trig:
                headline += f" Likely trigger: {trig}."
                root = {**root, "event_id": root.get("event_id") or trig.split()[1]}
        else:
            title = f"{issue.metric} {'down' if first and first.direction == 'down' else 'up'} in {_seg(issue.segment)}"
            headline = "Under investigation."

    # --- evidence ---------------------------------------------------------------------------
    evidence = []
    for ev in f.get("evidence_list", []):
        t = ev.get("type")
        if t == "formula_decomposition":
            parts = ", ".join(f"{d['metric']} {100 * float(d['share']):.0f}%" for d in ev.get("drivers", []))
            if parts:
                evidence.append(f"Formula {ev.get('formula')}: contributions {parts}.")
        elif t == "dimension_drilldown":
            evidence.append(f"Localized: {ev.get('summary')}")
        elif t == "counterfactual":
            evidence.append(f"Counterfactual: {ev.get('detail')}")
        elif t == "event_match":
            evidence.append(f"Event {ev.get('event_id')} ({_seg(ev.get('scope') or {})}): {ev.get('description')}")
        elif t == "cause_edge_check":
            evidence.append(f"Upstream {ev.get('from_metric')} -> {ev.get('to_metric')}: "
                            f"{'anomalous' if ev.get('anomalous') else 'normal'} "
                            f"({_num(ev.get('baseline'), 3)} -> {_num(ev.get('actual'), 3)}).")
        elif t == "planned_events":
            evidence.append(f"Planned event(s) {', '.join(ev.get('events', []))} overlap; only the excess was investigated.")
    if issue.kind == "data_quality":
        dd = issue.data_detail
        if dd.get("events"):
            evidence.append(f"Matching data_delay event(s): {', '.join(dd['events'])}.")

    # --- hypotheses / uncertainty -------------------------------------------------------
    hyps = [h for h in f.get("hypotheses", []) if h.get("status") != "rejected"]
    others = [h for h in hyps if h.get("node") != root.get("node") and not h.get("is_symptom")][:3]
    rejected = [h for h in f.get("hypotheses", []) if h.get("status") == "rejected"][:3]
    uncertainty = [f.get("uncertainty")] if f.get("uncertainty") else []
    if root.get("missing"):
        uncertainty.append("Not yet shown: " + ", ".join(m.replace("_", " ") for m in root["missing"]) + ".")
    for h in others:
        uncertainty.append(f"Also possible: {h['cause']} in {_seg(h.get('segment') or {})} ({h['status']}).")
    ruled_out = [f"{h['cause']}" for h in rejected]

    # --- impact / actions -----------------------------------------------------------------
    imp = f.get("business_impact") or {}
    impact = {}
    if imp.get("lost_orders") is not None:
        impact["lost_orders"] = imp.get("lost_orders")
    if imp.get("lost_revenue_eur") is not None:
        impact["lost_revenue_eur"] = imp.get("lost_revenue_eur")
    actions = []
    if root.get("event_id"):
        actions.append(f"Look at event {root['event_id']} first: it happened in the affected segment right before the change.")
    node = root.get("node") or issue.metric
    if node in ACTIONS:
        actions.append(ACTIONS[node].format(seg=_seg(root.get("segment") or issue.segment)))
    elif node:
        actions.append(f"Look at recent changes affecting {node} in {_seg(root.get('segment') or issue.segment)}.")

    symptoms = [e.summary() for e in eps if e.metric != node or e.segment != root.get("segment")]
    report = {
        "issue_id": issue.id, "kind": issue.kind, "status": issue.status, "severity": sev,
        "title": title, "headline": headline, "what_happened": what,
        "root_cause": root, "evidence": evidence, "business_impact": impact,
        "uncertainty": uncertainty, "ruled_out": ruled_out, "recommended_actions": actions,
        "symptoms": symptoms[:12], "trail": f.get("trail", []),
        "opened_at": issue.to_dict()["opened_at"], "resolved_at": issue.to_dict()["resolved_at"],
        "narrative": None,
    }
    if narrator is not None and issue.kind == "incident" and root:
        report["narrative"] = narrator.narrate(report)
    report["markdown"] = to_markdown(report)
    return report


def _trigger(finding: dict, segment: dict) -> Optional[str]:
    """An unplanned event found by the investigation whose scope covers the segment."""
    for ev in finding.get("evidence_list", []):
        if ev.get("type") != "event_match":
            continue
        scope = ev.get("scope") or {}
        if all(segment.get(k) == v for k, v in scope.items()):
            return f"event {ev.get('event_id')} ({ev.get('description')})"
    return None


def to_markdown(r: dict[str, Any]) -> str:
    lines = [f"## [{r['severity'].upper()}] {r['issue_id']}: {r['title']}", "",
             f"Status: **{r['status']}**, opened {r['opened_at']}" + (f", resolved {r['resolved_at']}" if r["resolved_at"] else ""),
             "", f"**{r['headline']}**", ""]
    if r.get("narrative"):
        lines += [r["narrative"], ""]
    lines += ["### What happened"] + [f"- {x}" for x in r["what_happened"]]
    if r["business_impact"]:
        lines += ["", "### Business impact"] + [
            f"- {k.replace('_', ' ')}: {_num(v)}" for k, v in r["business_impact"].items()]
    if r["evidence"]:
        lines += ["", "### Evidence"] + [f"- {x}" for x in r["evidence"]]
    if r["uncertainty"]:
        lines += ["", "### What is still uncertain"] + [f"- {x}" for x in r["uncertainty"]]
    if r["ruled_out"]:
        lines += ["", "### Ruled out"] + [f"- {x}" for x in r["ruled_out"]]
    if r["recommended_actions"]:
        lines += ["", "### Recommended actions"] + [f"- {x}" for x in r["recommended_actions"]]
    if r["trail"]:
        lines += ["", "### Investigation trail"] + [
            f"{t['step']}. **{t['action']}** {t['query']} → {t['observation']}" for t in r["trail"]]
    if r["symptoms"]:
        lines += ["", "### Also affected (symptoms)"] + [
            f"- {s['metric']} {s['direction']} in {seg_key(s['segment'])} ({s['rel_change_pct']}%)" for s in r["symptoms"]]
    return "\n".join(lines)


class LLMNarrator:
    """Optional: a short plain-language summary written by an LLM, checked for numbers.

    Uses ``agent.llm_client.ChatClient`` (env vars LLM_BASE_URL, LLM_MODEL,
    LLM_API_KEY). Any failure or any unknown number falls back to no narrative.
    """

    SYSTEM = ("You write a 2-3 sentence incident summary for marketplace managers from JSON facts. "
              "Use only numbers that appear in the facts. Say clearly what is confirmed and what is only a "
              "hypothesis. Reply as JSON: {\"narrative\": \"...\"}")

    def __init__(self, client: Any = None):
        self.client = client
        self.failures = 0

    def narrate(self, report: dict[str, Any]) -> Optional[str]:
        facts = {k: report[k] for k in ("title", "headline", "what_happened", "business_impact", "uncertainty",
                                        "recommended_actions")}
        try:
            if self.client is None:
                from agent.llm_client import ChatClient

                self.client = ChatClient()
            text = str(self.client.complete_json(self.SYSTEM, json.dumps(facts, default=str)).get("narrative", ""))
        except Exception:  # noqa: BLE001 - the narrative is optional
            self.failures += 1
            return None
        allowed = _numbers(json.dumps(facts, default=str))
        if not text or any(n not in allowed for n in _numbers(text)):
            self.failures += 1
            return None
        return text


def _numbers(text: str) -> set[str]:
    return {m.replace(",", "").rstrip(".0") or "0" for m in re.findall(r"\d[\d,]*\.?\d*", text)}
