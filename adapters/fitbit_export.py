"""
adapters/fitbit_export.py

Reads a Fitbit data export into a ReadingCollector.  Works with either:
  • Google Takeout → select "Fitbit"  (files under Takeout/Fitbit/...)
  • fitbit.com → Settings → Data Export → "Export Your Account Archive"

Both use the same file names, so files are matched by name wherever they
sit in the archive.

Files supported
────────────────
  heart_rate-YYYY-MM-DD.json                        → heart_rate        (bpm)
  resting_heart_rate-YYYY-MM-DD.json                → resting_hr        (bpm)
  steps-YYYY-MM-DD.json                             → steps
  sleep-YYYY-MM-DD.json                             → sleep_hours       (minutesAsleep)
  exercise-N.json                                   → active            (logged workouts)
  Minute SpO2 - YYYY-MM-DD.csv                      → spo2              (%)
  Heart Rate Variability Details - YYYY-MM-DD.csv   → hrv               (RMSSD, ms)
  Respiratory Rate Summary - YYYY-MM-DD.csv         → respiratory_rate  (br/min, during sleep)
  Computed Temperature - YYYY-MM-DD.csv             → skin_temp_variation (°C vs. baseline)

Timestamps are used as written in the files.
"""

import csv
import io
import json
import re
from datetime import timedelta

from adapters.common import ExportArchive, ReadingCollector, basename, parse_time

NAME = "Fitbit export"

_JSON_FILES = re.compile(
    r"^(heart_rate|resting_heart_rate|steps|sleep)-\d{4}-\d{2}-\d{2}\.json$"
    r"|^exercise-\d+\.json$"
)
_CSV_PREFIXES = (
    "Minute SpO2 - ",
    "Heart Rate Variability Details - ",
    "Respiratory Rate Summary - ",
    "Computed Temperature - ",
)


def _kind(name: str):
    b = basename(name)
    if _JSON_FILES.match(b):
        return b.split("-", 1)[0]
    for prefix in _CSV_PREFIXES:
        if b.startswith(prefix) and b.endswith(".csv"):
            return prefix
    return None


def detect(archive: ExportArchive) -> bool:
    return any(_kind(n) for n in archive.names())


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_json(kind: str, entries: list, collector: ReadingCollector):
    for e in entries:
        try:
            if kind == "heart_rate":
                bpm = _float(e["value"]["bpm"])
                if bpm:
                    collector.add("heart_rate", bpm, parse_time(e["dateTime"]))
            elif kind == "resting_heart_rate":
                rhr = _float((e.get("value") or {}).get("value"))
                if rhr:   # 0 means Fitbit had no estimate that day
                    collector.add("resting_hr", rhr, parse_time(e["dateTime"]))
            elif kind == "steps":
                n = _float(e["value"])
                if n:
                    collector.add_steps(parse_time(e["dateTime"]), "fitbit", n)
            elif kind == "sleep":
                minutes = _float(e.get("minutesAsleep"))
                if minutes:
                    wake = parse_time(e["endTime"])
                    collector.add_sleep_total(wake.date().isoformat(), minutes / 60)
            elif kind == "exercise":
                start = parse_time(e["startTime"])
                ms = _float(e.get("duration")) or 0
                collector.add_workout(start, start + timedelta(milliseconds=ms))
        except (KeyError, TypeError, ValueError):
            continue


def _read_csv(prefix: str, rows: csv.DictReader, collector: ReadingCollector):
    for row in rows:
        try:
            if prefix == "Minute SpO2 - ":
                v = _float(row.get("value"))
                if v:
                    collector.add("spo2", v, parse_time(row["timestamp"]))
            elif prefix == "Heart Rate Variability Details - ":
                v = _float(row.get("rmssd"))
                if v:
                    collector.add("hrv", v, parse_time(row["timestamp"]))
            elif prefix == "Respiratory Rate Summary - ":
                v = _float(row.get("full_sleep_breathing_rate"))
                if v:
                    collector.add("respiratory_rate", v, parse_time(row["timestamp"]))
            elif prefix == "Computed Temperature - ":
                # Average of each sample's difference from the personal baseline
                total = _float(row.get("baseline_relative_sample_sum"))
                n     = _float(row.get("temperature_samples"))
                if total is not None and n:
                    collector.add("skin_temp_variation", total / n,
                                  parse_time(row["sleep_end"]))
        except (KeyError, ValueError):
            continue


def read(archive: ExportArchive, collector: ReadingCollector):
    for name in archive.names():
        kind = _kind(name)
        if kind is None:
            continue
        with archive.open(name) as f:
            raw = f.read()
        if kind in _CSV_PREFIXES:
            text = raw.decode("utf-8-sig", errors="replace")
            _read_csv(kind, csv.DictReader(io.StringIO(text)), collector)
        else:
            try:
                entries = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entries, list):
                _read_json(kind, entries, collector)
