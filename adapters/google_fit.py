"""
adapters/google_fit.py

Reads Google Fit data from a Google Takeout export into a ReadingCollector.

Export process:
    takeout.google.com → deselect all → select "Fit" → export → download the .zip

Takeout's Fit folder contains:
  Fit/All Data/*.json      — every data point, one file per data source
  Fit/All Sessions/*.json  — workouts and sleep sessions

Data types supported (All Data)
────────────────────────────────
  com.google.heart_rate.bpm       → heart_rate       (bpm)
  com.google.oxygen_saturation    → spo2             (%)
  com.google.body.temperature     → body_temperature (°C)
  com.google.blood_glucose        → blood_glucose    (stored as mmol/L → mg/dL)
  com.google.step_count.delta     → steps
  com.google.sleep.segment        → sleep_hours      (asleep stages only)
  com.google.activity.segment     → sleep_hours      (sleep activity types only)
"""

import json

from adapters.common import (
    GLUCOSE_MMOL_TO_MGDL, ExportArchive, ReadingCollector, basename, from_epoch,
    parse_time,
)

NAME = "Google Fit (Takeout)"

# dataTypeName → (HealthRecord field, multiplier)
FIT_TO_FIELD = {
    "com.google.heart_rate.bpm":    ("heart_rate",       1.0),
    "com.google.oxygen_saturation": ("spo2",             1.0),
    "com.google.body.temperature":  ("body_temperature", 1.0),
    "com.google.blood_glucose":     ("blood_glucose",    GLUCOSE_MMOL_TO_MGDL),
}
STEPS_TYPE    = "com.google.step_count.delta"
SLEEP_TYPE    = "com.google.sleep.segment"
ACTIVITY_TYPE = "com.google.activity.segment"

# sleep.segment values: 1 awake, 2 sleeping, 3 out of bed, 4 light, 5 deep, 6 REM
_ASLEEP_STAGES = {2, 4, 5, 6}
# activity.segment values for sleep: 72 sleeping, 109 light, 110 deep, 111 REM
_SLEEP_ACTIVITIES = {72, 109, 110, 111}


def _in_fit(name: str, folder: str) -> bool:
    return f"/Fit/{folder}/" in "/" + name and name.endswith(".json")


def detect(archive: ExportArchive) -> bool:
    return any(_in_fit(n, "All Data") or _in_fit(n, "All Sessions") for n in archive.names())


def _value(point: dict):
    """First value of a data point — fpVal for measurements, intVal for counts/enums."""
    v = (point.get("fitValue") or [{}])[0].get("value", {})
    return v.get("fpVal", v.get("intVal"))


def _read_data_file(name: str, data: dict, collector: ReadingCollector):
    for point in data.get("Data Points", []):
        dtype = point.get("dataTypeName")
        value = _value(point)
        try:
            start = from_epoch(int(point["startTimeNanos"]) / 1e9)
            end   = from_epoch(int(point["endTimeNanos"]) / 1e9)
        except (KeyError, TypeError, ValueError):
            continue
        if value is None:
            continue

        if dtype in FIT_TO_FIELD:
            field, mult = FIT_TO_FIELD[dtype]
            collector.add(field, value * mult, start)
        elif dtype == STEPS_TYPE:
            # Raw per-device files and Google's merged file overlap — each file
            # is its own source, so the busiest one wins rather than the sum
            collector.add_steps(start, basename(name), value)
        elif dtype == SLEEP_TYPE and int(value) in _ASLEEP_STAGES:
            collector.add_sleep(start, end)
        elif dtype == ACTIVITY_TYPE and int(value) in _SLEEP_ACTIVITIES:
            collector.add_sleep(start, end)


def _read_session(session: dict, collector: ReadingCollector):
    try:
        start = parse_time(session["startTime"])
        end   = parse_time(session["endTime"])
    except (KeyError, ValueError):
        return
    activity = str(session.get("fitnessActivity", "")).lower()
    if activity.startswith("sleep"):
        collector.add_sleep(start, end)
    elif activity not in ("", "still", "unknown", "in_vehicle"):
        collector.add_workout(start, end)


def read(archive: ExportArchive, collector: ReadingCollector):
    for name in archive.names():
        is_data, is_session = _in_fit(name, "All Data"), _in_fit(name, "All Sessions")
        if not (is_data or is_session):
            continue
        with archive.open(name) as f:
            try:
                data = json.load(f)
            except ValueError:
                continue
        if is_data and isinstance(data, dict):
            _read_data_file(name, data, collector)
        elif is_session and isinstance(data, dict):
            _read_session(data, collector)
