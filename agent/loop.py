"""L5 Agent Investigation Loop: orchestrates DQ gate, hypotheses, menu, chooser, and Finding."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

import numpy as np
import pandas as pd

from graph.edges import GraphEdge
from graph.engine import MetricEngine
from graph.metric_graph import MetricGraph
from investigation.compare import Window, compare_total
from investigation.decompose import decompose
from investigation.drilldown import drill_down
from monitoring.data_quality import DQResult
from monitoring.event_calendar import EventCalendar, ExplanationResult
from monitoring.triage import adapt_anomaly

from agent.chooser import Chooser, FallbackChooser
from agent.hypotheses import (
    Hypothesis,
    build_initial_hypotheses,
    classify_symptom_vs_cause,
    label_hypothesis,
    rank_hypotheses,
)
from agent.menu import MenuItem, action_signature, build_menu
from agent.state import InvestigationState

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DQCheckerFn = Callable[[pd.Timestamp, pd.Timestamp, dict[str, str]], DQResult]


@dataclass(frozen=True)
class Limits:
    """Budget and stopping limits for an investigation."""

    max_steps: int = 8
    max_llm_calls: int = 8
    explained_threshold: float = 0.8
    min_gain: float = 0.05


class InvestigationTools(Protocol):
    """Tool interface used by the L5 loop (backed by Anila's investigation/ tools)."""

    def decompose_formula(
        self, metric: str, start: pd.Timestamp, end: pd.Timestamp, segment: dict[str, str]
    ) -> dict[str, Any]: ...

    def drill_dimension(
        self, metric: str, start: pd.Timestamp, end: pd.Timestamp, segment: dict[str, str]
    ) -> dict[str, Any]: ...

    def check_cause_edge(
        self,
        from_metric: str,
        to_metric: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        segment: dict[str, str],
        lag_hours: int = 0,
    ) -> dict[str, Any]: ...

    def counterfactual(
        self,
        metric: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        suspect_segment: dict[str, str],
        component: str | None = None,
    ) -> dict[str, Any]: ...

    def business_impact(
        self, metric: str, start: pd.Timestamp, end: pd.Timestamp, segment: dict[str, str]
    ) -> dict[str, Any]: ...


class RealInvestigationTools:
    """Adapter wrapping Anila's ``investigation/`` functions and ``MetricEngine``."""

    def __init__(
        self,
        engine: MetricEngine,
        graph: MetricGraph,
        detector: Any = None,
        weeks: int = 4,
    ) -> None:
        self.engine = engine
        self.graph = graph
        self.detector = detector
        self.weeks = int(weeks)

    def decompose_formula(
        self, metric: str, start: pd.Timestamp, end: pd.Timestamp, segment: dict[str, str]
    ) -> dict[str, Any]:
        win = Window.of(start, end)
        tree = decompose(self.engine, self.graph, metric, win, segment, weeks=self.weeks)
        drivers = tree.drivers(min_share=0.2)
        return {
            "formula": tree.formula,
            "actual": tree.actual,
            "baseline": tree.baseline,
            "drivers": [
                {
                    "metric": d.metric,
                    "share": float(d.share) if np.isfinite(d.share) else 0.0,
                    "actual": d.actual,
                    "baseline": d.baseline,
                    "rel_change": d.rel_change,
                }
                for d in drivers
            ],
            "note": tree.note,
        }

    def drill_dimension(
        self, metric: str, start: pd.Timestamp, end: pd.Timestamp, segment: dict[str, str]
    ) -> dict[str, Any]:
        win = Window.of(start, end)
        res = drill_down(self.engine, metric, win, segment, weeks=self.weeks)
        return {
            "metric": metric,
            "localized_segment": res.segment,
            "share": float(res.share_of_root) if np.isfinite(res.share_of_root) else 0.0,
            "summary": "; ".join(s.summary() for s in res.steps),
        }

    def check_cause_edge(
        self,
        from_metric: str,
        to_metric: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        segment: dict[str, str],
        lag_hours: int = 0,
    ) -> dict[str, Any]:
        del to_metric
        shift = pd.Timedelta(hours=int(lag_hours))
        win = Window.of(start - shift, end - shift)
        actual, baseline = compare_total(self.engine, from_metric, win, segment, weeks=self.weeks)
        anomalous = False
        if self.detector is not None and from_metric in self.engine.registry:
            eps = self.detector.detect([from_metric], by=list(segment.keys()), segment=segment, start=win.start, end=win.end)
            anomalous = len(eps) > 0
        elif np.isfinite(actual) and np.isfinite(baseline) and abs(baseline) > 1e-12:
            anomalous = abs(actual - baseline) / abs(baseline) >= 0.05
        return {
            "from_metric": from_metric,
            "actual": actual,
            "baseline": baseline,
            "anomalous": bool(anomalous),
        }

    def counterfactual(
        self,
        metric: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        suspect_segment: dict[str, str],
        component: str | None = None,
        parent_segment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del component
        if not suspect_segment:
            return {"passed": False, "recovery_share": 0.0, "detail": "empty suspect segment"}

        win = Window.of(start, end)
        parent_seg = dict(parent_segment or {})
        tot_a, tot_b = compare_total(self.engine, metric, win, parent_seg, weeks=self.weeks)
        seg_a, seg_b = compare_total(self.engine, metric, win, suspect_segment, weeks=self.weeks)

        tot_change = tot_a - tot_b
        seg_change = seg_a - seg_b
        if not (np.isfinite(tot_change) and np.isfinite(seg_change)) or abs(tot_change) < 1e-12:
            return {"passed": False, "recovery_share": 0.0, "detail": "zero total change"}

        if self.engine.registry.get(metric).is_additive:
            if parent_seg == suspect_segment:
                # Suspect segment IS the anomaly segment; compare against overall market.
                mkt_a, mkt_b = compare_total(self.engine, metric, win, {}, weeks=self.weeks)
                rest_a, rest_b = mkt_a - seg_a, mkt_b - seg_b
                rest_rel = abs(rest_a - rest_b) / abs(rest_b) if abs(rest_b) > 1e-12 else 0.0
                recovery = 1.0 - min(1.0, rest_rel / max(abs(seg_change / seg_b), 1e-12)) if abs(seg_b) > 1e-12 else 0.0
            else:
                remaining = tot_change - seg_change
                recovery = 1.0 - abs(remaining) / abs(tot_change)
        else:
            recovery = 1.0 if abs(seg_change) > 0 else 0.0
        recovery = float(np.clip(recovery, 0.0, 1.0))
        return {
            "passed": recovery >= 0.7,
            "recovery_share": recovery,
            "detail": f"removing {suspect_segment} recovers {100 * recovery:.1f}% of change",
        }

    def business_impact(
        self, metric: str, start: pd.Timestamp, end: pd.Timestamp, segment: dict[str, str]
    ) -> dict[str, Any]:
        del metric
        win = Window.of(start, end)
        impact: dict[str, Any] = {}
        for m, key in (("orders", "lost_orders"), ("revenue", "lost_revenue_eur")):
            if m in self.engine.registry:
                a, b = compare_total(self.engine, m, win, segment, weeks=self.weeks)
                if np.isfinite(a) and np.isfinite(b):
                    diff = b - a
                    impact[key] = int(round(diff)) if m == "orders" else round(float(diff), 2)
        return impact


def _mechanism_confirmed(graph: MetricGraph, metric: str, node: str) -> bool:
    if metric not in graph.graph:
        return False
    for u, _, data in graph.graph.in_edges(metric, data=True):
        edge: GraphEdge = data["edge"]
        if u == node and edge.status == "confirmed":
            return True
    return False


def _execute_item(
    item: MenuItem,
    state: InvestigationState,
    hyps: list[Hypothesis],
    graph: MetricGraph,
    tools: InvestigationTools,
    calendar: EventCalendar,
    start: pd.Timestamp,
    end: pd.Timestamp,
    anomaly_metric: str,
    anomaly_segment: dict[str, str],
) -> tuple[str, str]:
    """Execute one menu item, updating ``state`` and ``hyps``; returns ``(query, observation)``."""
    kind = item.kind
    args = item.args

    if kind == "decompose_formula":
        formula = str(args.get("formula", ""))
        if formula:
            state.active_formula_by_metric[args["metric"]] = formula
        res = tools.decompose_formula(args["metric"], start, end, args["segment"])
        drivers = res.get("drivers") or []
        if not drivers:
            return f"decompose({args['metric']})", f"no drivers found ({res.get('note', '')})"

        top = drivers[0]
        top_share = max(0.0, min(1.0, abs(float(top.get("share", 0.0)))))
        state.explained_share = max(state.explained_share, top_share)
        for d in drivers:
            d_metric = str(d["metric"])
            d_share = max(0.0, min(1.0, abs(float(d.get("share", 0.0)))))
            matched = [h for h in hyps if h.node == d_metric and h.source == "formula_component"]
            if not matched:
                h = Hypothesis(
                    cause=f"Formula component {d_metric} of {args['metric']}",
                    source="formula_component",
                    node=d_metric,
                    segment=dict(args["segment"]),
                    mechanism_confirmed=_mechanism_confirmed(graph, args["metric"], d_metric),
                )
                hyps.append(h)
                matched = [h]
            for h in matched:
                h.explained_share = max(h.explained_share, d_share)
                h.confidence = max(h.confidence, min(0.95, 0.4 + 0.5 * d_share))
                h.evidence.append({"type": "formula_contribution", **d})

        obs = ", ".join(f"{d['metric']} ({100 * d['share']:.0f}%)" for d in drivers)
        state.evidence.append({"type": "formula_decomposition", "formula": res.get("formula"), "drivers": drivers})
        return f"decompose({args['metric']}) via {res.get('formula')}", obs

    if kind == "drill_dimension":
        res = tools.drill_dimension(args["metric"], start, end, args["segment"])
        loc_seg = dict(res.get("localized_segment") or {})
        share = max(0.0, min(1.0, abs(float(res.get("share", 0.0)))))
        if share > 0 and loc_seg:
            state.explained_share = max(state.explained_share, share)
            for h in hyps:
                if h.node == args["metric"] or h.source in {"event", "formula_component"}:
                    if not h.segment or all(loc_seg.get(k) == v for k, v in h.segment.items()):
                        h.segment = loc_seg
                        h.localized = share >= 0.5
                        h.explained_share = max(h.explained_share, share)
            if not any(h.source == "segment" and h.segment == loc_seg for h in hyps):
                hyps.append(
                    Hypothesis(
                        cause=f"Localized drop in {args['metric']} at {loc_seg}",
                        source="segment",
                        node=args["metric"],
                        segment=loc_seg,
                        confidence=min(0.9, 0.4 + 0.5 * share),
                        explained_share=share,
                        localized=(share >= 0.5),
                        mechanism_confirmed=(anomaly_metric not in graph.graph or _mechanism_confirmed(graph, anomaly_metric, args["metric"])),
                    )
                )
        state.evidence.append({"type": "dimension_drilldown", **res})
        return f"drill_down({args['metric']}, {args['segment']})", str(res.get("summary") or f"localized={loc_seg} ({100 * share:.0f}%)")

    if kind == "check_cause_edge":
        res = tools.check_cause_edge(
            args["from_metric"], args["to_metric"], start, end, args["segment"], args.get("lag_hours", 0)
        )
        anomalous = bool(res.get("anomalous"))
        anomalous_nodes = {args["to_metric"]}
        if anomalous:
            anomalous_nodes.add(args["from_metric"])
        role = classify_symptom_vs_cause(args["to_metric"], graph, lambda n: n in anomalous_nodes)
        for h in hyps:
            if h.node == args["from_metric"] and h.source == "graph_cause_edge":
                if not anomalous:
                    h.contradicted = True
                else:
                    h.timing_aligned = True
                    h.evidence.append({"type": "upstream_check", **res})
            if h.node == args["to_metric"] and role == "symptom":
                h.is_symptom = True
        state.evidence.append({"type": "cause_edge_check", **res})
        return f"check_cause({args['from_metric']} -> {args['to_metric']})", f"anomalous={anomalous}, downstream_role={role}"

    if kind == "check_events":
        lookback = pd.Timedelta(hours=int(args.get("lookback_hours", 1)))
        exp = calendar.explains(start - lookback, end, anomaly_segment, metric=anomaly_metric)
        if exp.unplanned_events:
            for h in hyps:
                if h.source in {"formula_component", "segment", "event"}:
                    h.timing_aligned = True
            for ev in exp.unplanned_events:
                if not any(e.get("event_id") == ev.event_id for e in state.evidence):
                    state.evidence.append({"type": "event_match", "event_id": ev.event_id, "description": ev.description})
        obs = f"found {len(exp.unplanned_events)} unplanned, {len(exp.planned_events)} planned, {len(exp.data_delay_events)} data_delay"
        return f"check_events({anomaly_segment}, lookback={int(lookback.total_seconds() // 3600)}h)", obs

    if kind == "counterfactual":
        try:
            res = tools.counterfactual(
                args["metric"],
                start,
                end,
                args["suspect_segment"],
                args.get("component"),
                parent_segment=anomaly_segment,
            )
        except TypeError:
            res = tools.counterfactual(
                args["metric"], start, end, args["suspect_segment"], args.get("component")
            )
        passed = bool(res.get("passed"))
        for h in hyps:
            if h.segment == args["suspect_segment"] or h.node == args.get("component"):
                h.counterfactual_passed = passed
                h.evidence.append({"type": "counterfactual", **res})
        state.evidence.append({"type": "counterfactual", **res})
        return f"counterfactual(remove={args['suspect_segment']})", str(res.get("detail") or f"passed={passed}")

    return kind, "no-op"


def _build_finding(
    state: InvestigationState,
    hyps: list[Hypothesis],
    impact: dict[str, Any],
    now: pd.Timestamp,
) -> dict[str, Any]:
    a = state.anomaly
    metric = str(a["metric"])
    seg = dict(a.get("segment") or {})
    seg_key = "|".join(f"{k}={v}" for k, v in sorted(seg.items())) or "all"
    top_cause = hyps[0].node if hyps else metric
    fp_raw = f"{metric}|{seg_key}|{top_cause}"
    finding_id = "fnd_" + hashlib.sha1(f"{fp_raw}|{a['start']}".encode()).hexdigest()[:12]

    actual = float(a["actual"])
    expected = float(a["expected"])
    abs_chg = actual - expected
    rel_pct = (100.0 * abs_chg / expected) if abs(expected) > 1e-12 else 0.0

    unmet = hyps[0].missing_conditions if hyps else ["no_hypotheses_generated"]
    if hyps and hyps[0].status == "confirmed":
        uncertainty = "None — top hypothesis confirmed across all 4 conditions."
    elif state.stop_reason == "insufficient_evidence":
        uncertainty = "Data quality check failed; investigation halted before causal attribution."
    else:
        uncertainty = f"Unmet confirmation conditions for top hypothesis: {', '.join(unmet)}"

    now_iso = now.strftime(TIME_FORMAT)
    return {
        "finding_id": finding_id,
        "fingerprint": fp_raw,
        "metric": metric,
        "segment": seg,
        "time_window": {"start": a["start"], "end": a["end"]},
        "expected": {"value": expected, "lower": float(a["lower"]), "upper": float(a["upper"])},
        "actual": actual,
        "change_size": {"absolute": round(abs_chg, 4), "relative_pct": round(rel_pct, 2), "score": float(a["score"])},
        "business_impact": impact,
        "evidence_list": list(state.evidence),
        "trail": list(state.trail),
        "hypotheses": [h.to_dict() for h in hyps],
        "explained_share": round(float(state.explained_share), 4),
        "stop_reason": state.stop_reason,
        "uncertainty": uncertainty,
        "status": "new",
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def investigate(
    anomaly: Any,
    graph: MetricGraph,
    tools: InvestigationTools,
    calendar: EventCalendar,
    dq_checker: DQCheckerFn,
    chooser: Chooser | None = None,
    limits: Limits | None = None,
    now: Any = None,
    available_tools: set[str] | None = None,
) -> dict[str, Any]:
    """Run the L5 investigation loop and return a Section 4 Finding dict."""
    item = adapt_anomaly(anomaly)
    if item.start >= item.end:
        raise ValueError(f"Anomaly start ({item.start}) must be before end ({item.end})")

    lim = limits or Limits()
    active_chooser: Chooser = chooser or FallbackChooser(min_gain=lim.min_gain)
    now_ts = pd.Timestamp(now).tz_convert("UTC") if (now is not None and pd.Timestamp(now).tzinfo) else (
        pd.Timestamp(now, tz="UTC") if now is not None else item.end
    )

    state = InvestigationState(
        anomaly={
            "metric": item.metric,
            "segment": dict(item.segment),
            "start": item.start.strftime(TIME_FORMAT),
            "end": item.end.strftime(TIME_FORMAT),
            "expected": item.expected,
            "lower": item.lower,
            "upper": item.upper,
            "actual": item.actual,
            "score": item.score,
        }
    )

    # 1. Data-quality gate first.
    dq = dq_checker(item.start, item.end, dict(item.segment))
    if not dq.fresh or not dq.complete:
        state.stop_reason = "insufficient_evidence"
        state.evidence.append({"type": "data_quality", "detail": dq.detail, "completeness": dq.completeness})
        state.trail.append(
            {
                "step": 1,
                "action": "check_data_quality",
                "query": f"check_dq({item.segment})",
                "observation": dq.detail,
                "reason": "Data quality gate failed; stopping with insufficient evidence",
            }
        )
        hyps = build_initial_hypotheses(item.metric, item.segment, graph, dq=dq)
        return _build_finding(state, hyps, {}, now_ts)

    # 2. Check calendar (with 2h lookback for unplanned releases right before window).
    events = calendar.explains(item.start - pd.Timedelta(hours=2), item.end, item.segment, metric=item.metric)
    if events.planned_events:
        state.evidence.append(
            {
                "type": "planned_events",
                "events": [e.event_id for e in events.planned_events],
                "note": "Planned event(s) overlap window; investigating excess change",
            }
        )

    # 3. Build initial hypotheses.
    hyps = build_initial_hypotheses(item.metric, item.segment, graph, events=events, dq=dq)

    # 4. Investigation loop.
    while True:
        hyps = rank_hypotheses([label_hypothesis(h) for h in hyps])
        state.hypotheses = [h.to_dict() for h in hyps]

        if state.explained_share >= lim.explained_threshold and hyps and hyps[0].status == "confirmed":
            state.stop_reason = "explained_and_confirmed"
            break
        if state.steps_used >= lim.max_steps:
            state.stop_reason = "max_steps_reached"
            break

        menu = build_menu(state, graph, available_tools=available_tools)
        if not menu:
            state.stop_reason = "menu_empty"
            break
        if all(m.expected_gain < lim.min_gain for m in menu):
            state.stop_reason = "min_gain_reached"
            break

        is_llm = hasattr(active_chooser, "client")
        if is_llm and state.llm_calls_used >= lim.max_llm_calls:
            state.stop_reason = "max_llm_calls_reached"
            break

        choice = active_chooser.choose(state.snapshot(), menu)
        if is_llm and not choice.used_fallback:
            state.llm_calls_used += 1
        if choice.used_fallback:
            state.fallbacks_used += 1
            state.fallback_reasons.append(choice.fallback_reason)

        if choice.decision == "stop" or choice.item_id is None:
            state.stop_reason = choice.stop_reason or "chooser_stopped"
            break

        chosen_item = next((m for m in menu if m.id == choice.item_id), None)
        if chosen_item is None:
            state.stop_reason = "invalid_choice"
            break

        sig = action_signature(chosen_item.kind, chosen_item.args)
        state.completed_actions.append(sig)
        state.steps_used += 1

        try:
            query, obs = _execute_item(
                chosen_item, state, hyps, graph, tools, calendar, item.start, item.end, item.metric, item.segment
            )
        except Exception as exc:
            query = f"{chosen_item.kind}({chosen_item.args})"
            obs = f"tool_error: {exc}"

        reason = choice.reason
        if choice.used_fallback and choice.fallback_reason:
            reason = f"{reason} ({choice.fallback_reason})"

        state.trail.append(
            {
                "step": state.steps_used,
                "action": chosen_item.kind,
                "query": query,
                "observation": obs,
                "reason": reason,
            }
        )

    hyps = rank_hypotheses([label_hypothesis(h) for h in hyps])
    state.hypotheses = [h.to_dict() for h in hyps]

    try:
        impact = tools.business_impact(item.metric, item.start, item.end, dict(item.segment))
    except Exception:
        impact = {}

    return _build_finding(state, hyps, impact, now_ts)
