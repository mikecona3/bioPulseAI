"""
adapters/apple_health.py

Reads an Apple Health export into a ReadingCollector.

Export process on iPhone:
    Health app → profile photo → Export All Health Data → share the .zip

The export contains export.xml (every reading) and an electrocardiograms/
folder with one CSV per ECG recording.

HealthKit types supported
──────────────────────────
  HKQuantityTypeIdentifierHeartRate                → heart_rate      (bpm)
  HKQuantityTypeIdentifierOxygenSaturation         → spo2            (%)
  HKQuantityTypeIdentifierHeartRateVariabilitySDNN → hrv             (ms)
  HKQuantityTypeIdentifierRespiratoryRate          → respiratory_rate (br/min)
  HKQuantityTypeIdentifierBodyTemperature          → body_temperature (°C)
  HKQuantityTypeIdentifierBloodGlucose             → blood_glucose   (mg/dL)
  HKQuantityTypeIdentifierStepCount                → steps           (count)
  HKQuantityTypeIdentifierRestingHeartRate         → resting_hr      (bpm)
  HKCategoryTypeIdentifierSleepAnalysis            → sleep_hours     (hours asleep, by wake-up day)
  <Workout> elements                               → active          (hours overlapping a workout)
  electrocardiograms/*.csv                         → ecg_rhythm      (watch classification)
"""

import csv
import io
import xml.etree.ElementTree as ET
from typing import Optional

from adapters.common import (
    GLUCOSE_MMOL_TO_MGDL, ExportArchive, ReadingCollector, parse_time,
)

NAME = "Apple Health"

# HealthKit type identifier → HealthRecord field name
HK_TO_FIELD: dict[str, str] = {
    "HKQuantityTypeIdentifierHeartRate":                "heart_rate",
    "HKQuantityTypeIdentifierOxygenSaturation":         "spo2",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv",
    "HKQuantityTypeIdentifierRespiratoryRate":           "respiratory_rate",
    "HKQuantityTypeIdentifierBodyTemperature":           "body_temperature",
    "HKQuantityTypeIdentifierBloodGlucose":              "blood_glucose",
    "HKQuantityTypeIdentifierStepCount":                 "steps",
    "HKQuantityTypeIdentifierRestingHeartRate":          "resting_hr",
}

SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
# Matches Asleep, AsleepUnspecified, AsleepCore, AsleepDeep, AsleepREM (not InBed / Awake)
_ASLEEP_PREFIX = "HKCategoryValueSleepAnalysisAsleep"

# Body temperature with no unit and above this value is assumed to be °F
_FAHRENHEIT_THRESHOLD = 50.0


def _export_xml(archive: ExportArchive) -> Optional[str]:
    return next((n for n in archive.names() if n.endswith("export.xml")), None)


def detect(archive: ExportArchive) -> bool:
    return _export_xml(archive) is not None


def _convert(field: str, value: float, unit: str) -> float:
    """Normalise a reading to the unit HealthRecord expects."""
    if field == "spo2" and value <= 1.0:
        return value * 100.0                                # fraction → percent
    if field == "blood_glucose" and "mmol" in unit:
        return value * GLUCOSE_MMOL_TO_MGDL                 # mmol/L → mg/dL
    if field == "body_temperature" and (
        unit == "degF" or (not unit and value > _FAHRENHEIT_THRESHOLD)
    ):
        return (value - 32) * 5 / 9                         # °F → °C
    return value


def _ecg_label(classification: str) -> str:
    """Map Apple's ECG classification text to the screener's rhythm labels."""
    c = classification.lower()
    if "atrial fibrillation" in c:
        return "afib"
    if "over" in c or "high" in c:
        return "high_hr"
    if "under" in c or "low" in c:
        return "low_hr"
    if "inconclusive" in c:
        return "inconclusive"
    if "sinus" in c:
        return "sinus"
    return c.strip().replace(" ", "_")


def _read_ecg(text: str, collector: ReadingCollector):
    """Read the header lines of one ECG export CSV."""
    header = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0]:
            header[row[0].strip()] = row[1].strip()
        if "Recorded Date" in header and "Classification" in header:
            break
    try:
        ts = parse_time(header["Recorded Date"])
    except (KeyError, ValueError):
        return
    if header.get("Classification"):
        collector.add_ecg(ts, _ecg_label(header["Classification"]))


def _iterparse(source):
    root = None
    for event, elem in ET.iterparse(source, events=("start", "end")):
        if root is None:
            root = elem            # first event is the <HealthData> start
            continue
        if event == "end" and elem.tag in ("Record", "Workout"):
            yield elem
            root.clear()           # drop finished elements — keeps memory flat


def read(archive: ExportArchive, collector: ReadingCollector):
    with archive.open(_export_xml(archive)) as f:
        for elem in _iterparse(f):
            try:
                ts = parse_time(elem.get("startDate", ""))
            except ValueError:
                continue

            if elem.tag == "Workout":
                try:
                    collector.add_workout(ts, parse_time(elem.get("endDate", "")))
                except ValueError:
                    pass
                continue

            hk_type = elem.get("type", "")

            if hk_type == SLEEP_TYPE:
                if elem.get("value", "").startswith(_ASLEEP_PREFIX):
                    try:
                        collector.add_sleep(ts, parse_time(elem.get("endDate", "")))
                    except ValueError:
                        pass
                continue

            field = HK_TO_FIELD.get(hk_type)
            if field is None:
                continue
            try:
                value = float(elem.get("value"))
            except (TypeError, ValueError):
                continue

            if field == "steps":
                # iPhone and Watch both count the same steps — tracked per source
                collector.add_steps(ts, elem.get("sourceName", ""), value)
            else:
                collector.add(field, _convert(field, value, elem.get("unit", "")), ts)

    for name in archive.names():
        if "electrocardiograms/" in name and name.endswith(".csv"):
            with archive.open(name) as f:
                _read_ecg(f.read(4096).decode("utf-8", errors="replace"), collector)
