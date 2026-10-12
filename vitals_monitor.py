"""
vitals_monitor.py  —  Layer 1: Statistical Pre-Screener

Defines the shared data types (HealthRecord, Anomaly) used throughout the
pipeline, and the StatisticalScreener that gates access to the LLM layer.

Baselines are computed once over the whole dataset.  Hours where the person
was exercising (a workout, or a burst of steps) are left out of the heart-rate
and respiratory-rate baselines and skip the z-score check for those metrics,
so ordinary workouts are not reported as anomalies.  Hard physiological bounds
still apply to every hour.
"""

import statistics
from dataclasses import dataclass, asdict, field
from typing import Optional, Union


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class HealthRecord:
    """
    One time-bucketed snapshot of a person's vitals.
    Populated by adapters/apple_health.py or adapters/fitbit.py via hourly
    aggregation of individual wearable readings.
    """
    timestamp:           str
    heart_rate:          Optional[float] = None   # bpm
    hrv:                 Optional[float] = None   # ms (RMSSD / SDNN)
    steps:               Optional[int]   = None
    resting_hr:          Optional[float] = None   # bpm
    calories_burned:     Optional[float] = None
    sleep_hours:         Optional[float] = None   # hours
    spo2:                Optional[float] = None   # %
    blood_glucose:       Optional[float] = None   # mg/dL
    stress_score:        Optional[float] = None   # 0–100
    respiratory_rate:    Optional[float] = None   # breaths / min
    body_temperature:    Optional[float] = None   # °C
    skin_temp_variation: Optional[float] = None   # °C delta from baseline
    ecg_rhythm:          Optional[str]   = None   # e.g. "sinus", "afib"
    active:              Optional[bool]  = None   # True if a workout overlapped this hour


@dataclass
class Anomaly:
    """A single flagged anomaly produced by StatisticalScreener."""
    metric:          str
    value:           Union[float, str]   # str for ECG rhythm labels
    timestamp:       str
    reason:          str                 # human-readable explanation of the flag
    severity:        str                 # "low" | "medium" | "high"
    context_window:  list               # surrounding HealthRecord dicts for LLM
    # Optional — populated for z-score anomalies; None for hard-bounds / combo flags
    baseline_mean:   Optional[float] = None
    baseline_stdev:  Optional[float] = None
    z_score:         Optional[float] = None   # signed: + above baseline, − below


# Sort order for display: high first
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------------------
# Statistical Screener
# ---------------------------------------------------------------------------

class StatisticalScreener:
    """
    Layer 1 pre-screener.  Runs two checks on every HealthRecord:

      1. Hard physiological bounds  — catches acute events, no baseline needed
      2. Personal z-score baseline  — catches "unusual for YOU" even within
                                      normal population ranges

    Plus multi-metric combo rules that catch correlated anomaly patterns
    (e.g. elevated HR + low SpO₂) that neither metric would surface alone.

    Usage:
        screener  = StatisticalScreener(records)   # list[HealthRecord]
        anomalies = screener.screen()              # list[Anomaly]
    """

    # Absolute physiological limits — flag immediately, regardless of baseline
    HARD_BOUNDS: dict[str, tuple] = {
        "heart_rate":           (40,   220),
        "hrv":                  (0,    300),
        "resting_hr":          (30,    150),
        "spo2":                 (80,   100),
        "blood_glucose":        (50,   400),
        "stress_score":         (0,    100),
        "respiratory_rate":     (5,     35),
        "skin_temp_variation":  (-3.0,  3.0),
        "body_temperature":     (35.0, 41.0),
    }

    # ECG classifications the watch flags as potentially concerning
    ECG_CONCERNING = {"afib", "high_hr", "low_hr", "inconclusive"}

    ZSCORE_THRESHOLD     = 2.5   # SDs from personal mean → flag
    MIN_BASELINE_RECORDS = 7     # minimum readings before z-score is trusted

    # Metrics that rise naturally with exercise — no z-score check in active hours
    ACTIVITY_SENSITIVE   = {"heart_rate", "respiratory_rate"}
    ACTIVE_STEPS_PER_HOUR = 1000  # an hour with this many steps counts as active

    def __init__(self, records: list):
        self.records    = records
        self._baselines = self._compute_baselines()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def screen(self) -> list:
        """Run all checks and return every flagged Anomaly."""
        anomalies     = []
        records_dicts = [asdict(r) for r in self.records]
        sleep_days    = set()   # sleep+hrv fires at most once per day

        for i, record in enumerate(self.records):
            # ±3-record context window passed to LLM for each anomaly
            context = records_dicts[max(0, i - 3) : i + 4]

            # Per-metric: hard bounds then z-score
            for metric, (lo, hi) in self.HARD_BOUNDS.items():
                value = getattr(record, metric, None)
                if value is None:
                    continue

                if not (lo <= value <= hi):
                    anomalies.append(Anomaly(
                        metric=metric, value=value,
                        timestamp=record.timestamp,
                        reason=f"Outside safe range [{lo}–{hi}]",
                        severity="high", context_window=context,
                    ))
                    continue

                if metric in self.ACTIVITY_SENSITIVE and self._is_active(record):
                    continue

                z = self._zscore(metric, value)
                if z is not None and abs(z) >= self.ZSCORE_THRESHOLD:
                    b        = self._baselines[metric]
                    severity = "high" if abs(z) > 3.5 else "medium"
                    anomalies.append(Anomaly(
                        metric=metric, value=value,
                        timestamp=record.timestamp,
                        reason=(
                            f"Z-score {z:+.1f}σ from personal mean "
                            f"(mean={b['mean']:.1f}, σ={b['stdev']:.1f})"
                        ),
                        severity=severity, context_window=context,
                        baseline_mean=round(b["mean"],  2),
                        baseline_stdev=round(b["stdev"], 2),
                        z_score=round(max(min(z, 99.9), -99.9), 2),
                    ))

            # Multi-metric combo rules
            anomalies += self._check_hr_spo2(record, context)
            day = record.timestamp[:10]
            if day not in sleep_days:
                found = self._check_sleep_hrv(record, context)
                if found:
                    sleep_days.add(day)
                    anomalies += found
            anomalies += self._check_resp_spo2(record, context)
            anomalies += self._check_ecg(record, context)

        return anomalies

    def get_summary(self) -> dict:
        """Per-metric baseline stats (mean, stdev, count of readings used)."""
        return {
            metric: {
                "mean":  round(b["mean"],  2),
                "stdev": round(b["stdev"], 2),
                "count": b["count"],
            }
            for metric, b in self._baselines.items()
        }

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _is_active(self, r: HealthRecord) -> bool:
        return bool(r.active) or (r.steps or 0) >= self.ACTIVE_STEPS_PER_HOUR

    def _compute_baselines(self) -> dict:
        baselines = {}
        for m in self.HARD_BOUNDS:
            values = [
                getattr(r, m) for r in self.records
                if getattr(r, m, None) is not None
                and not (m in self.ACTIVITY_SENSITIVE and self._is_active(r))
            ]
            if len(values) >= self.MIN_BASELINE_RECORDS:
                stdev = statistics.stdev(values) if len(values) > 1 else 0.01
                baselines[m] = {
                    "mean":  statistics.mean(values),
                    "stdev": max(stdev, 0.01),   # avoid div/0
                    "min":   min(values),
                    "max":   max(values),
                    "count": len(values),
                }
        return baselines

    def _zscore(self, metric: str, value: float) -> Optional[float]:
        if metric not in self._baselines:
            return None
        b = self._baselines[metric]
        return (value - b["mean"]) / b["stdev"]

    # Combo rules ──────────────────────────────────────────────────────

    def _check_hr_spo2(self, r: HealthRecord, ctx: list) -> list:
        """High HR + low SpO₂ together is more alarming than either alone."""
        if r.heart_rate and r.spo2 and r.heart_rate > 110 and r.spo2 < 94:
            return [Anomaly(
                metric="heart_rate+spo2", value=r.heart_rate,
                timestamp=r.timestamp,
                reason=(
                    f"Elevated HR ({r.heart_rate:.0f} bpm) "
                    f"with low SpO₂ ({r.spo2:.0f}%)"
                ),
                severity="high", context_window=ctx,
            )]
        return []

    def _check_sleep_hrv(self, r: HealthRecord, ctx: list) -> list:
        """Low sleep + low HRV → under-recovery."""
        if r.sleep_hours and r.hrv and r.sleep_hours < 5 and r.hrv < 20:
            return [Anomaly(
                metric="sleep+hrv", value=r.sleep_hours,
                timestamp=r.timestamp,
                reason=(
                    f"Poor sleep ({r.sleep_hours:.1f} h) "
                    f"with very low HRV ({r.hrv:.0f} ms)"
                ),
                severity="medium", context_window=ctx,
            )]
        return []

    def _check_resp_spo2(self, r: HealthRecord, ctx: list) -> list:
        """Elevated RR + low SpO₂ can indicate respiratory distress or illness onset."""
        if r.respiratory_rate and r.spo2 and r.respiratory_rate > 20 and r.spo2 < 95:
            return [Anomaly(
                metric="respiratory_rate+spo2", value=r.respiratory_rate,
                timestamp=r.timestamp,
                reason=(
                    f"Elevated respiratory rate ({r.respiratory_rate:.0f} br/min) "
                    f"with reduced SpO₂ ({r.spo2:.0f}%)"
                ),
                severity="high", context_window=ctx,
            )]
        return []

    def _check_ecg(self, r: HealthRecord, ctx: list) -> list:
        """Escalate device-classified concerning ECG rhythms to the LLM."""
        if r.ecg_rhythm and r.ecg_rhythm.lower() in self.ECG_CONCERNING:
            rhythm   = r.ecg_rhythm.lower()
            severity = "high" if rhythm == "afib" else "medium"
            return [Anomaly(
                metric="ecg_rhythm", value=rhythm,
                timestamp=r.timestamp,
                reason=f"Device classified ECG rhythm as '{rhythm}'",
                severity=severity, context_window=ctx,
            )]
        return []
