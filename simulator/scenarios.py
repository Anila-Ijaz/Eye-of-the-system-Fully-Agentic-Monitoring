"""Scenario injector for L1 marketplace simulator.

Applies fault scenarios and innocent changes to a baseline ``marketplace_hourly``
DataFrame, and produces the corresponding ``events`` table and ``ground_truth.json``
artifact defined in ``contracts/contracts.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import pandas as pd

MARKETPLACE_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "country",
    "platform",
    "category",
    "sessions",
    "searches",
    "search_clicks",
    "recommendation_impressions",
    "recommendation_clicks",
    "cart_adds",
    "checkouts",
    "payment_attempts",
    "orders",
    "refunds",
    "revenue",
    "ad_impressions",
    "ad_clicks",
    "ad_spend",
    "active_listings",
    "out_of_stock_views",
)

EVENTS_COLUMNS: tuple[str, ...] = (
    "event_id",
    "start_time",
    "end_time",
    "event_type",
    "planned",
    "country",
    "platform",
    "category",
    "description",
    "expected_effect",
)


@dataclass(frozen=True)
class ScenarioSpec:
    """Configuration for a single fault or innocent change scenario."""

    scenario_id: str
    kind: Literal["fault", "innocent"]
    scenario_type: str
    root_cause: str
    description: str
    start_time: pd.Timestamp
    end_time: pd.Timestamp
    affected_segment: dict[str, str]
    primary_metric: str
    downstream_metrics: list[str] = field(default_factory=list)
    event_type: str | None = None
    planned: bool = False
    event_lead_minutes: int = 0
    expected_effect: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


def _ensure_utc(ts: pd.Timestamp | str) -> pd.Timestamp:
    parsed = pd.Timestamp(ts)
    if parsed.tzinfo is None:
        return parsed.tz_localize("UTC")
    return parsed.tz_convert("UTC")


def _segment_mask(
    df: pd.DataFrame,
    start_time: pd.Timestamp,
    end_time: pd.Timestamp,
    segment: dict[str, str],
) -> pd.Series:
    timestamps = pd.to_datetime(df["timestamp"], utc=True)
    mask = (timestamps >= start_time) & (timestamps <= end_time)
    for dim, val in segment.items():
        if dim not in df.columns:
            raise ValueError(f"Unknown segment dimension: {dim}")
        mask &= df[dim] == val
    return mask


def _scale_int_columns(
    df: pd.DataFrame, mask: pd.Series, columns: list[str], factor: float
) -> None:
    for col in columns:
        df.loc[mask, col] = (df.loc[mask, col] * factor).round().clip(lower=0).astype(int)


def _scale_float_columns(
    df: pd.DataFrame, mask: pd.Series, columns: list[str], factor: float
) -> None:
    for col in columns:
        df.loc[mask, col] = (df.loc[mask, col] * factor).clip(lower=0.0).round(2)


def _enforce_funnel_bounds(df: pd.DataFrame, mask: pd.Series) -> None:
    """Keep basic funnel invariants valid after scaling."""
    df.loc[mask, "search_clicks"] = df.loc[mask, ["search_clicks", "searches"]].min(axis=1)
    df.loc[mask, "payment_attempts"] = df.loc[mask, ["payment_attempts", "checkouts"]].min(axis=1)
    df.loc[mask, "orders"] = df.loc[mask, ["orders", "payment_attempts"]].min(axis=1)
    df.loc[mask, "refunds"] = df.loc[mask, ["refunds", "orders"]].min(axis=1)


def _apply_payment_failure(
    df: pd.DataFrame, mask: pd.Series, spec: ScenarioSpec
) -> pd.DataFrame:
    """Drop payment success rate without changing upstream payment_attempts."""
    drop_rate = float(spec.params.get("drop_rate", 0.65))
    factor = max(0.0, 1.0 - drop_rate)
    _scale_int_columns(df, mask, ["orders", "refunds"], factor)
    _scale_float_columns(df, mask, ["revenue"], factor)
    _enforce_funnel_bounds(df, mask)
    return df


def _apply_bad_search_release(
    df: pd.DataFrame, mask: pd.Series, spec: ScenarioSpec
) -> pd.DataFrame:
    """Drop search clicks and propagate proportionally down the funnel."""
    drop_rate = float(spec.params.get("drop_rate", 0.50))
    factor = max(0.0, 1.0 - drop_rate)
    _scale_int_columns(
        df,
        mask,
        ["search_clicks", "cart_adds", "checkouts", "payment_attempts", "orders", "refunds"],
        factor,
    )
    _scale_float_columns(df, mask, ["revenue"], factor)
    _enforce_funnel_bounds(df, mask)
    return df


def _apply_stock_out(
    df: pd.DataFrame, mask: pd.Series, spec: ScenarioSpec
) -> pd.DataFrame:
    """Increase out-of-stock views, reduce active listings, and depress conversions."""
    listing_drop = float(spec.params.get("listing_drop_rate", 0.40))
    oos_multiplier = float(spec.params.get("oos_multiplier", 4.0))
    conversion_drop = float(spec.params.get("conversion_drop_rate", 0.45))

    _scale_int_columns(df, mask, ["active_listings"], max(0.0, 1.0 - listing_drop))
    _scale_int_columns(df, mask, ["out_of_stock_views"], oos_multiplier)
    # Ensure out-of-stock views visibly spike even if baseline was 0.
    zero_oos = mask & (df["out_of_stock_views"] == 0)
    if zero_oos.any():
        df.loc[zero_oos, "out_of_stock_views"] = (
            (df.loc[zero_oos, "sessions"] * 0.15).round().clip(lower=1).astype(int)
        )

    factor = max(0.0, 1.0 - conversion_drop)
    _scale_int_columns(
        df, mask, ["cart_adds", "checkouts", "payment_attempts", "orders", "refunds"], factor
    )
    _scale_float_columns(df, mask, ["revenue"], factor)
    _enforce_funnel_bounds(df, mask)
    return df


def _apply_ad_budget_exhaustion(
    df: pd.DataFrame, mask: pd.Series, spec: ScenarioSpec
) -> pd.DataFrame:
    """Front-load ad spend early in the day and zero it out after exhaust_hour."""
    exhaust_hour = int(spec.params.get("exhaust_hour", 10))
    early_multiplier = float(spec.params.get("early_multiplier", 2.2))
    post_traffic_factor = float(spec.params.get("post_traffic_factor", 0.70))

    hours = pd.to_datetime(df["timestamp"], utc=True).dt.hour
    early_mask = mask & (hours < exhaust_hour)
    late_mask = mask & (hours >= exhaust_hour)

    _scale_int_columns(df, early_mask, ["ad_impressions", "ad_clicks"], early_multiplier)
    _scale_float_columns(df, early_mask, ["ad_spend"], early_multiplier)

    df.loc[late_mask, ["ad_impressions", "ad_clicks"]] = 0
    df.loc[late_mask, "ad_spend"] = 0.0
    _scale_int_columns(
        df,
        late_mask,
        [
            "sessions",
            "searches",
            "search_clicks",
            "cart_adds",
            "checkouts",
            "payment_attempts",
            "orders",
            "refunds",
        ],
        post_traffic_factor,
    )
    _scale_float_columns(df, late_mask, ["revenue"], post_traffic_factor)
    _enforce_funnel_bounds(df, mask)
    return df


def _apply_planned_promo(
    df: pd.DataFrame, mask: pd.Series, spec: ScenarioSpec
) -> pd.DataFrame:
    """Lift traffic and full funnel for a planned promotional campaign."""
    lift_factor = float(spec.params.get("lift_factor", 1.25))
    _scale_int_columns(
        df,
        mask,
        [
            "sessions",
            "searches",
            "search_clicks",
            "cart_adds",
            "checkouts",
            "payment_attempts",
            "orders",
            "refunds",
        ],
        lift_factor,
    )
    _scale_float_columns(df, mask, ["revenue"], lift_factor)
    _enforce_funnel_bounds(df, mask)
    return df


def _apply_holiday(
    df: pd.DataFrame, mask: pd.Series, spec: ScenarioSpec
) -> pd.DataFrame:
    """Apply a country-wide holiday traffic/order shift."""
    traffic_factor = float(spec.params.get("traffic_factor", 0.80))
    _scale_int_columns(
        df,
        mask,
        [
            "sessions",
            "searches",
            "search_clicks",
            "recommendation_impressions",
            "recommendation_clicks",
            "cart_adds",
            "checkouts",
            "payment_attempts",
            "orders",
            "refunds",
        ],
        traffic_factor,
    )
    _scale_float_columns(df, mask, ["revenue"], traffic_factor)
    _enforce_funnel_bounds(df, mask)
    return df


def _apply_late_missing_data(
    df: pd.DataFrame, mask: pd.Series, spec: ScenarioSpec
) -> pd.DataFrame:
    """Drop rows in the affected slice to simulate late/missing ingestion."""
    return df.loc[~mask].reset_index(drop=True)


_SCENARIO_HANDLERS: dict[str, Callable[[pd.DataFrame, pd.Series, ScenarioSpec], pd.DataFrame]] = {
    "payment_failure": _apply_payment_failure,
    "bad_search_release": _apply_bad_search_release,
    "stock_out": _apply_stock_out,
    "ad_budget_exhaustion": _apply_ad_budget_exhaustion,
    "planned_promo": _apply_planned_promo,
    "holiday": _apply_holiday,
    "late_missing_data": _apply_late_missing_data,
}


def _build_event_row(event_id: str, spec: ScenarioSpec) -> dict[str, Any]:
    event_start = spec.start_time - pd.Timedelta(minutes=spec.event_lead_minutes)
    event_end = None if spec.event_type == "release" else spec.end_time.isoformat()
    return {
        "event_id": event_id,
        "start_time": event_start.isoformat(),
        "end_time": event_end,
        "event_type": spec.event_type,
        "planned": bool(spec.planned),
        "country": spec.affected_segment.get("country"),
        "platform": spec.affected_segment.get("platform"),
        "category": spec.affected_segment.get("category"),
        "description": spec.description,
        "expected_effect": spec.expected_effect,
    }


def _build_ground_truth_entry(
    spec: ScenarioSpec, event_id: str | None
) -> dict[str, Any]:
    return {
        "scenario_id": spec.scenario_id,
        "kind": spec.kind,
        "root_cause": spec.root_cause,
        "description": spec.description,
        "start_time": spec.start_time.isoformat(),
        "end_time": spec.end_time.isoformat(),
        "affected_segment": dict(spec.affected_segment),
        "primary_metric": spec.primary_metric,
        "downstream_metrics": list(spec.downstream_metrics),
        "related_event_id": event_id,
    }


def write_ground_truth(ground_truth: dict[str, Any], path: str | Path) -> Path:
    """Write ``ground_truth.json`` to disk, creating parent directories if needed."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(ground_truth, indent=2) + "\n", encoding="utf-8")
    return output_path


def inject_scenarios(
    df: pd.DataFrame,
    scenarios: list[ScenarioSpec],
    ground_truth_path: str | Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Apply fault and innocent scenarios to baseline marketplace data.

    Returns ``(modified_df, events_df, ground_truth_dict)`` conforming to
    ``contracts/contracts.md``.
    """
    missing_cols = [col for col in MARKETPLACE_COLUMNS if col not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing required marketplace columns: {missing_cols}")

    modified_df = df.copy()
    modified_df["timestamp"] = pd.to_datetime(modified_df["timestamp"], utc=True)

    event_rows: list[dict[str, Any]] = []
    gt_entries: list[dict[str, Any]] = []

    for idx, spec in enumerate(scenarios, start=1):
        handler = _SCENARIO_HANDLERS.get(spec.scenario_type)
        if handler is None:
            raise ValueError(f"Unsupported scenario_type: {spec.scenario_type}")

        start_utc = _ensure_utc(spec.start_time)
        end_utc = _ensure_utc(spec.end_time)
        normalized_spec = ScenarioSpec(
            scenario_id=spec.scenario_id,
            kind=spec.kind,
            scenario_type=spec.scenario_type,
            root_cause=spec.root_cause,
            description=spec.description,
            start_time=start_utc,
            end_time=end_utc,
            affected_segment=spec.affected_segment,
            primary_metric=spec.primary_metric,
            downstream_metrics=spec.downstream_metrics,
            event_type=spec.event_type,
            planned=spec.planned,
            event_lead_minutes=spec.event_lead_minutes,
            expected_effect=spec.expected_effect,
            params=spec.params,
        )

        mask = _segment_mask(modified_df, start_utc, end_utc, normalized_spec.affected_segment)
        modified_df = handler(modified_df, mask, normalized_spec)

        event_id: str | None = None
        if normalized_spec.event_type is not None:
            event_id = f"evt_{idx:03d}"
            event_rows.append(_build_event_row(event_id, normalized_spec))

        gt_entries.append(_build_ground_truth_entry(normalized_spec, event_id))

    events_df = pd.DataFrame(event_rows, columns=list(EVENTS_COLUMNS))
    ground_truth = {"scenarios": gt_entries}

    if ground_truth_path is not None:
        write_ground_truth(ground_truth, ground_truth_path)

    return modified_df, events_df, ground_truth


def default_fault_scenarios(anchor_time: pd.Timestamp | str) -> list[ScenarioSpec]:
    """Return the standard suite of 4 fault scenarios anchored at ``anchor_time``."""
    t0 = _ensure_utc(anchor_time)
    return [
        ScenarioSpec(
            scenario_id="scn_de_android_payment",
            kind="fault",
            scenario_type="payment_failure",
            root_cause="payment_gateway_timeout",
            description="Payment failure spike on Android in DE after checkout release",
            start_time=t0,
            end_time=t0 + pd.Timedelta(hours=24),
            affected_segment={"country": "DE", "platform": "android"},
            primary_metric="payment_success_rate",
            downstream_metrics=["orders", "revenue"],
            event_type="release",
            planned=False,
            event_lead_minutes=15,
            params={"drop_rate": 0.65},
        ),
        ScenarioSpec(
            scenario_id="scn_de_bad_search",
            kind="fault",
            scenario_type="bad_search_release",
            root_cause="search_ranking_regression",
            description="Search ranking release in DE degraded search click-through",
            start_time=t0 + pd.Timedelta(days=1),
            end_time=t0 + pd.Timedelta(days=1, hours=18),
            affected_segment={"country": "DE"},
            primary_metric="search_ctr",
            downstream_metrics=["cart_adds", "checkouts", "orders", "revenue"],
            event_type="release",
            planned=False,
            event_lead_minutes=10,
            params={"drop_rate": 0.50},
        ),
        ScenarioSpec(
            scenario_id="scn_de_electronics_stockout",
            kind="fault",
            scenario_type="stock_out",
            root_cause="inventory_stock_out",
            description="Supplier stock-out in DE electronics",
            start_time=t0 + pd.Timedelta(days=2),
            end_time=t0 + pd.Timedelta(days=2, hours=20),
            affected_segment={"country": "DE", "category": "electronics"},
            primary_metric="out_of_stock_rate",
            downstream_metrics=["cart_adds", "orders", "revenue"],
            event_type=None,
            planned=False,
            params={"listing_drop_rate": 0.40, "oos_multiplier": 4.0, "conversion_drop_rate": 0.45},
        ),
        ScenarioSpec(
            scenario_id="scn_fr_ad_exhaustion",
            kind="fault",
            scenario_type="ad_budget_exhaustion",
            root_cause="ad_budget_spent_too_fast",
            description="Daily ad budget exhausted before 10:00 UTC in FR",
            start_time=t0 + pd.Timedelta(days=3),
            end_time=t0 + pd.Timedelta(days=3, hours=23),
            affected_segment={"country": "FR"},
            primary_metric="ad_spend",
            downstream_metrics=["ad_clicks", "sessions", "orders", "revenue"],
            event_type=None,
            planned=False,
            params={"exhaust_hour": 10, "early_multiplier": 2.2, "post_traffic_factor": 0.70},
        ),
    ]


def default_innocent_scenarios(anchor_time: pd.Timestamp | str) -> list[ScenarioSpec]:
    """Return the standard suite of 3 innocent-change scenarios anchored at ``anchor_time``."""
    t0 = _ensure_utc(anchor_time)
    return [
        ScenarioSpec(
            scenario_id="scn_uk_planned_promo",
            kind="innocent",
            scenario_type="planned_promo",
            root_cause="planned_campaign",
            description="Planned autumn promo in UK",
            start_time=t0,
            end_time=t0 + pd.Timedelta(days=1, hours=23),
            affected_segment={"country": "UK"},
            primary_metric="sessions",
            downstream_metrics=["orders", "revenue"],
            event_type="campaign",
            planned=True,
            expected_effect="+25% sessions and orders",
            params={"lift_factor": 1.25},
        ),
        ScenarioSpec(
            scenario_id="scn_us_holiday",
            kind="innocent",
            scenario_type="holiday",
            root_cause="national_holiday",
            description="Public holiday traffic shift in US",
            start_time=t0 + pd.Timedelta(days=2),
            end_time=t0 + pd.Timedelta(days=2, hours=23),
            affected_segment={"country": "US"},
            primary_metric="sessions",
            downstream_metrics=["orders", "revenue"],
            event_type="holiday",
            planned=True,
            expected_effect="-20% sessions during holiday",
            params={"traffic_factor": 0.80},
        ),
        ScenarioSpec(
            scenario_id="scn_de_ios_late_data",
            kind="innocent",
            scenario_type="late_missing_data",
            root_cause="ingestion_pipeline_delay",
            description="Late telemetry batch for iOS in DE",
            start_time=t0 + pd.Timedelta(days=4, hours=12),
            end_time=t0 + pd.Timedelta(days=4, hours=15),
            affected_segment={"country": "DE", "platform": "ios"},
            primary_metric="row_completeness",
            downstream_metrics=["sessions", "orders"],
            event_type="data_delay",
            planned=False,
            expected_effect="Missing rows for 4 hours until backfill",
        ),
    ]
