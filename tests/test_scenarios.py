"""Unit tests for L1 scenario injector."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from simulator.scenarios import (
    EVENTS_COLUMNS,
    MARKETPLACE_COLUMNS,
    ScenarioSpec,
    default_fault_scenarios,
    default_innocent_scenarios,
    inject_scenarios,
)


def _make_baseline_df(hours: int = 120) -> pd.DataFrame:
    """Create a deterministic baseline marketplace_hourly DataFrame."""
    timestamps = pd.date_range("2026-10-15T00:00:00Z", periods=hours, freq="1h", tz="UTC")
    countries = ["DE", "UK", "FR", "US"]
    platforms = ["android", "ios"]
    categories = ["electronics", "fashion"]

    rows = []
    for ts in timestamps:
        for country in countries:
            for platform in platforms:
                for category in categories:
                    rows.append(
                        {
                            "timestamp": ts,
                            "country": country,
                            "platform": platform,
                            "category": category,
                            "sessions": 1000,
                            "searches": 600,
                            "search_clicks": 300,
                            "recommendation_impressions": 500,
                            "recommendation_clicks": 80,
                            "cart_adds": 180,
                            "checkouts": 120,
                            "payment_attempts": 100,
                            "orders": 94,
                            "refunds": 5,
                            "revenue": 4700.0,
                            "ad_impressions": 4000,
                            "ad_clicks": 200,
                            "ad_spend": 150.0,
                            "active_listings": 500,
                            "out_of_stock_views": 10,
                        }
                    )
    return pd.DataFrame(rows, columns=list(MARKETPLACE_COLUMNS))


class TestScenarioInjector(unittest.TestCase):
    def test_payment_failure_targets_only_de_android(self) -> None:
        baseline = _make_baseline_df(hours=48)
        fault = default_fault_scenarios("2026-10-15T10:00:00Z")[0]

        modified, events, gt = inject_scenarios(baseline, [fault])

        ts = pd.to_datetime(modified["timestamp"], utc=True)
        in_window = (ts >= fault.start_time) & (ts <= fault.end_time)
        de_android = (modified["country"] == "DE") & (modified["platform"] == "android")

        # Targeted slice: payment_attempts intact, orders and revenue dropped.
        affected = modified.loc[in_window & de_android]
        self.assertTrue((affected["payment_attempts"] == 100).all())
        self.assertTrue((affected["orders"] < 50).all())
        self.assertTrue((affected["revenue"] < 2500.0).all())

        # Untouched slice: DE iOS in the same window must remain at baseline.
        untouched = modified.loc[in_window & (modified["country"] == "DE") & (modified["platform"] == "ios")]
        self.assertTrue((untouched["orders"] == 94).all())

        # Events and ground truth match contracts.
        self.assertEqual(tuple(events.columns), EVENTS_COLUMNS)
        self.assertEqual(len(events), 1)
        self.assertEqual(events.iloc[0]["event_type"], "release")
        self.assertFalse(bool(events.iloc[0]["planned"]))
        self.assertEqual(gt["scenarios"][0]["kind"], "fault")
        self.assertEqual(gt["scenarios"][0]["related_event_id"], "evt_001")

    def test_all_fault_and_innocent_scenarios_preserve_funnel_and_contracts(self) -> None:
        baseline = _make_baseline_df(hours=120)
        scenarios = default_fault_scenarios("2026-10-15T00:00:00Z") + default_innocent_scenarios(
            "2026-10-15T00:00:00Z"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            gt_path = Path(tmpdir) / "ground_truth.json"
            modified, events, gt = inject_scenarios(baseline, scenarios, ground_truth_path=gt_path)

            self.assertTrue(gt_path.exists())
            loaded_gt = json.loads(gt_path.read_text(encoding="utf-8"))
            self.assertEqual(len(loaded_gt["scenarios"]), len(scenarios))
            self.assertEqual(loaded_gt, gt)

        # Funnel invariants hold everywhere after all injections.
        self.assertTrue((modified["orders"] <= modified["payment_attempts"]).all())
        self.assertTrue((modified["payment_attempts"] <= modified["checkouts"]).all())
        self.assertTrue((modified["search_clicks"] <= modified["searches"]).all())
        self.assertTrue((modified["refunds"] <= modified["orders"]).all())

        # Late/missing data dropped 4 hours * 2 categories = 8 rows for DE/ios.
        self.assertEqual(len(modified), len(baseline) - 8)

        # Planned events are marked planned=True; data_delay is planned=False.
        planned_types = set(events.loc[events["planned"], "event_type"])
        self.assertEqual(planned_types, {"campaign", "holiday"})
        self.assertIn("data_delay", set(events["event_type"]))

    def test_ad_budget_exhaustion_zeros_late_hours(self) -> None:
        baseline = _make_baseline_df(hours=24)
        ad_fault = ScenarioSpec(
            scenario_id="scn_fr_ad",
            kind="fault",
            scenario_type="ad_budget_exhaustion",
            root_cause="ad_budget_spent_too_fast",
            description="Ad budget exhausted at 10:00 UTC",
            start_time=pd.Timestamp("2026-10-15T00:00:00Z"),
            end_time=pd.Timestamp("2026-10-15T23:00:00Z"),
            affected_segment={"country": "FR"},
            primary_metric="ad_spend",
            downstream_metrics=["sessions", "orders"],
            params={"exhaust_hour": 10},
        )

        modified, events, _ = inject_scenarios(baseline, [ad_fault])
        fr = modified[modified["country"] == "FR"]
        hours = pd.to_datetime(fr["timestamp"], utc=True).dt.hour

        self.assertTrue((fr.loc[hours < 10, "ad_spend"] > 150.0).all())
        self.assertTrue((fr.loc[hours >= 10, "ad_spend"] == 0.0).all())
        self.assertTrue((fr.loc[hours >= 10, "ad_clicks"] == 0).all())
        self.assertTrue(events.empty)

    def test_missing_columns_raises(self) -> None:
        bad_df = pd.DataFrame({"timestamp": ["2026-10-15T00:00:00Z"]})
        fault = default_fault_scenarios("2026-10-15T00:00:00Z")[0]
        with self.assertRaises(ValueError):
            inject_scenarios(bad_df, [fault])


if __name__ == "__main__":
    unittest.main()
