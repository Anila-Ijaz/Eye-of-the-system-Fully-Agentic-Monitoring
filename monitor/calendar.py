"""L4 Planned-events calendar for distinguishing expected changes from incidents.

Window convention: ``[start, end)`` — ``start`` inclusive, ``end`` exclusive.
All timestamps are UTC timezone-aware.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

_REQUIRED_COLUMNS = ("event_id", "start_time", "event_type", "planned")
_EFFECT_PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")


def _to_utc(ts: Any) -> pd.Timestamp:
    try:
        parsed = pd.Timestamp(ts)
    except Exception as exc:
        raise ValueError(f"Invalid timestamp: {ts!r}") from exc
    if pd.isna(parsed):
        raise ValueError(f"Timestamp cannot be NaT/None: {ts!r}")
    if parsed.tzinfo is None:
        return parsed.tz_localize("UTC")
    return parsed.tz_convert("UTC")


def _clean_opt_str(val: Any) -> str | None:
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    if not s or s == "*" or s.lower() == "nan":
        return None
    return s


def _matches_dim(event_val: str | None, seg_val: str | None) -> bool:
    if event_val is None or seg_val is None:
        return True
    return event_val == seg_val


@dataclass(frozen=True)
class CalendarEvent:
    """Normalized event record from the ``events`` table."""

    event_id: str
    start_time: pd.Timestamp
    end_time: pd.Timestamp | None
    event_type: str
    planned: bool
    country: str | None = None
    platform: str | None = None
    category: str | None = None
    description: str = ""
    expected_effect: str | None = None


@dataclass(frozen=True)
class ExplanationResult:
    """Categorized events overlapping a window and segment."""

    planned_events: list[CalendarEvent]
    unplanned_events: list[CalendarEvent]
    data_delay_events: list[CalendarEvent]


class EventCalendar:
    """In-memory queryable calendar of marketplace events."""

    def __init__(self, events: list[CalendarEvent]) -> None:
        self._events = list(events)

    @classmethod
    def from_dataframe(cls, df: pd.DataFrame) -> EventCalendar:
        """Load events from an ``events`` DataFrame without mutating it."""
        missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"events DataFrame missing required columns: {missing}")

        if df.empty:
            return cls([])

        records: list[CalendarEvent] = []
        for row in df.to_dict(orient="records"):
            start_ts = _to_utc(row["start_time"])
            raw_end = row.get("end_time")
            end_ts = None if (raw_end is None or pd.isna(raw_end) or str(raw_end).strip() == "") else _to_utc(raw_end)
            if end_ts is not None and end_ts < start_ts:
                raise ValueError(f"Event {row['event_id']!r} has end_time < start_time")
            records.append(
                CalendarEvent(
                    event_id=str(row["event_id"]),
                    start_time=start_ts,
                    end_time=end_ts,
                    event_type=str(row["event_type"]).strip(),
                    planned=bool(row["planned"]),
                    country=_clean_opt_str(row.get("country")),
                    platform=_clean_opt_str(row.get("platform")),
                    category=_clean_opt_str(row.get("category")),
                    description="" if pd.isna(row.get("description")) else str(row.get("description", "")),
                    expected_effect=_clean_opt_str(row.get("expected_effect")),
                )
            )
        return cls(records)

    def events_for(
        self,
        start: Any,
        end: Any,
        segment: Mapping[str, str] | None = None,
    ) -> list[CalendarEvent]:
        """Return events overlapping ``[start, end)`` and matching ``segment``."""
        start_ts = _to_utc(start)
        end_ts = _to_utc(end)
        if start_ts >= end_ts:
            raise ValueError(f"start ({start_ts}) must be strictly before end ({end_ts})")

        seg = segment or {}
        seg_country = _clean_opt_str(seg.get("country"))
        seg_platform = _clean_opt_str(seg.get("platform"))
        seg_category = _clean_opt_str(seg.get("category"))

        matched: list[CalendarEvent] = []
        for ev in self._events:
            if ev.end_time is None or ev.end_time == ev.start_time:
                overlaps = start_ts <= ev.start_time < end_ts
            else:
                overlaps = ev.start_time < end_ts and ev.end_time > start_ts
            if not overlaps:
                continue
            if not (
                _matches_dim(ev.country, seg_country)
                and _matches_dim(ev.platform, seg_platform)
                and _matches_dim(ev.category, seg_category)
            ):
                continue
            matched.append(ev)
        return matched

    def explains(
        self,
        start: Any,
        end: Any,
        segment: Mapping[str, str] | None = None,
        metric: str | None = None,
    ) -> ExplanationResult:
        """Split overlapping events into planned innocent, unplanned, and data-delay."""
        del metric  # reserved for future metric-specific event filtering
        overlapping = self.events_for(start, end, segment)
        planned_events: list[CalendarEvent] = []
        unplanned_events: list[CalendarEvent] = []
        data_delay_events: list[CalendarEvent] = []

        for ev in overlapping:
            if ev.event_type == "data_delay":
                data_delay_events.append(ev)
            elif ev.planned and ev.event_type in {"campaign", "holiday"}:
                planned_events.append(ev)
            else:
                # Releases (even if planned=True) are never innocent explanations.
                unplanned_events.append(ev)

        return ExplanationResult(
            planned_events=planned_events,
            unplanned_events=unplanned_events,
            data_delay_events=data_delay_events,
        )

    def sensitivity_multiplier(
        self,
        start: Any,
        end: Any,
        segment: Mapping[str, str] | None = None,
        metric: str | None = None,
        default_multiplier: float = 1.5,
    ) -> float:
        """Return >1.0 when a planned campaign/holiday is active, else 1.0."""
        if default_multiplier < 1.0:
            raise ValueError(f"default_multiplier must be >= 1.0, got {default_multiplier}")

        exp = self.explains(start, end, segment, metric=metric)
        if not exp.planned_events:
            return 1.0

        mult = default_multiplier
        for ev in exp.planned_events:
            if ev.expected_effect:
                match = _EFFECT_PCT_RE.search(ev.expected_effect)
                if match:
                    pct = abs(float(match.group(1)))
                    if pct > 0.0:
                        mult = max(1.0 + pct / 100.0, 1.05)
        return float(mult)
