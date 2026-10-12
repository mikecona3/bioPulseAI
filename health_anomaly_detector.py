"""
health_anomaly_detector.py  —  Layer 2: LLM Interpretation

Takes the list[Anomaly] from StatisticalScreener and sends them to the
Claude API for contextual, plain-English interpretation.

A long export can produce hundreds of anomalies, so only the most severe
ones (one per metric per day, up to MAX_DETAILED) are sent in full, along
with counts for all of them.  The response is constrained to a JSON schema
so it always parses.

Requires ANTHROPIC_API_KEY (or another credential the anthropic SDK finds).
"""

import anthropic
import json
from collections import Counter
from dataclasses import asdict

from vitals_monitor import SEVERITY_ORDER, StatisticalScreener


# ---------------------------------------------------------------------------
# Layer 2 — LLM Analyzer
# ---------------------------------------------------------------------------

_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "anomaly_assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric":          {"type": "string"},
                    "timestamp":       {"type": "string"},
                    "plain_english":   {"type": "string"},
                    "possible_causes": {"type": "array", "items": {"type": "string"}},
                    "recommendation":  {"type": "string"},
                    "urgency":         {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["metric", "timestamp", "plain_english",
                             "possible_causes", "recommendation", "urgency"],
                "additionalProperties": False,
            },
        },
        "overall_summary":   {"type": "string"},
        "patterns_detected": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["anomaly_assessments", "overall_summary", "patterns_detected"],
    "additionalProperties": False,
}


class MissingCredentialsError(RuntimeError):
    """No Anthropic API key (or other credential) is configured."""


def _select_for_llm(anomalies: list, limit: int) -> list:
    """
    Most severe anomalies first, keeping only the worst one per metric per
    day so a single bad day can't crowd out everything else.
    """
    ranked = sorted(
        anomalies,
        key=lambda a: (SEVERITY_ORDER.get(a.severity, 3), -abs(a.z_score or 0)),
    )
    seen, picked = set(), []
    for a in ranked:
        key = (a.metric, a.timestamp[:10])
        if key in seen:
            continue
        seen.add(key)
        picked.append(a)
        if len(picked) == limit:
            break
    return sorted(picked, key=lambda a: a.timestamp)


def _compact(record: dict) -> dict:
    """Drop empty fields from a context record — most of them are None."""
    return {k: v for k, v in record.items() if v is not None}


class LLMAnalyzer:
    """
    Sends flagged anomalies to the Claude API for contextual interpretation.
    Only called when StatisticalScreener has found at least one anomaly,
    keeping API costs minimal.
    """

    MAX_DETAILED = 30   # anomalies sent with full context

    SYSTEM_PROMPT = """You are a health data analyst reviewing statistical anomalies \
flagged in a person's wearable device data.

You will receive a JSON payload with:
  • "totals" — counts of every anomaly the statistical screen flagged
  • "anomalies" — the most severe of those, each with surrounding hourly readings
Readings marked "active": true were taken during a workout.

For each anomaly in "anomalies", provide:
  • A plain-English explanation of what the reading means
  • Likely causes — benign explanations first, concerning ones second
  • A clear recommended action

Use "totals" for the overall summary and patterns, since it covers everything flagged.

Rules:
  - You are NOT a medical professional. Never diagnose. Frame everything as observations.
  - Always recommend consulting a doctor for anything high-severity.
  - Be concise and reassuring where appropriate — most anomalies have benign causes.
  - overall_summary is 2–3 sentences."""

    def __init__(self, model: str = "claude-sonnet-5-5"):
        # Reads ANTHROPIC_API_KEY from environment automatically
        self.client = anthropic.Anthropic()
        self.model  = model

    def analyze(self, anomalies: list, user_context: str = "") -> dict:
        """
        Send anomalies to the LLM and return its structured interpretation.

        Args:
            anomalies:    list[Anomaly] from StatisticalScreener.screen()
            user_context: Optional free-text the user provides about themselves
                          (e.g. "I went on a long run yesterday")

        Returns:
            dict with keys: anomaly_assessments, overall_summary, patterns_detected,
            anomalies_reviewed, anomalies_total

        Raises:
            RuntimeError if Claude declines or the response is cut off.
        """
        if not anomalies:
            return {
                "anomaly_assessments": [],
                "overall_summary":     "No anomalies detected in this dataset.",
                "patterns_detected":   [],
                "anomalies_reviewed":  0,
                "anomalies_total":     0,
            }

        selected = _select_for_llm(anomalies, self.MAX_DETAILED)
        timestamps = sorted(a.timestamp for a in anomalies)
        payload = {
            "user_context": user_context or "No additional context provided.",
            "totals": {
                "anomalies_flagged": len(anomalies),
                "detailed_below":    len(selected),
                "date_range":        [timestamps[0][:10], timestamps[-1][:10]],
                "by_metric_and_severity": {
                    f"{m} ({sev})": n
                    for (m, sev), n in Counter(
                        (a.metric, a.severity) for a in anomalies
                    ).most_common()
                },
            },
            "anomalies": [
                {
                    "timestamp":          a.timestamp,
                    "metric":             a.metric,
                    "value":              a.value,
                    "statistical_reason": a.reason,
                    "severity":           a.severity,
                    "context_window":     [_compact(r) for r in a.context_window],
                }
                for a in selected
            ],
        }

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=self.SYSTEM_PROMPT,
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": _RESULT_SCHEMA},
                },
                messages=[
                    {
                        "role":    "user",
                        "content": (
                            "Please analyze the following health anomalies:\n\n"
                            f"```json\n{json.dumps(payload, indent=2)}\n```"
                        ),
                    }
                ],
            )
        except TypeError as e:
            # The SDK raises a bare TypeError when it finds no credentials
            if "authentication" not in str(e):
                raise
            raise MissingCredentialsError(
                "No Anthropic API key found. Set ANTHROPIC_API_KEY and try again."
            ) from e

        if response.stop_reason == "refusal":
            details = response.stop_details
            raise RuntimeError(
                "Claude declined to analyze this data"
                + (f": {details.explanation}" if details and details.explanation else ".")
            )
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude's response was cut off before it finished.")

        text   = next(b.text for b in response.content if b.type == "text")
        result = json.loads(text)
        result["anomalies_reviewed"] = len(selected)
        result["anomalies_total"]    = len(anomalies)
        return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class HealthAnomalyDetector:
    """
    Full two-layer pipeline orchestrator.

      Layer 1 → StatisticalScreener  (fast, free, runs first)
      Layer 2 → LLMAnalyzer          (Claude API — only runs if anomalies found)

    Usage (CLI / scripting):
        from adapters import load_export
        from health_anomaly_detector import HealthAnomalyDetector

        records, _, _ = load_export(["export.zip"])
        result      = HealthAnomalyDetector(records).run()
        print(result["overall_summary"])

    The GUI calls Layer 1 and Layer 2 separately so it can show stat results
    immediately and defer the LLM call to a button press.
    """

    def __init__(self, records: list):
        self.records  = records
        self.screener = StatisticalScreener(records)

    def run(self, user_context: str = "") -> dict:
        print(f"[Layer 1] Screening {len(self.records)} records...")
        anomalies = self.screener.screen()
        print(f"[Layer 1] {len(anomalies)} anomal{'y' if len(anomalies) == 1 else 'ies'} flagged.")

        if not anomalies:
            return {
                "anomalies_found":     0,
                "overall_summary":     "No anomalies detected. All readings within normal range.",
                "patterns_detected":   [],
                "anomaly_assessments": [],
                "raw_anomalies":       [],
            }

        print("[Layer 2] Sending anomalies to Claude...")
        llm_result = LLMAnalyzer().analyze(anomalies, user_context)

        return {
            "anomalies_found": len(anomalies),
            "raw_anomalies":   [asdict(a) for a in anomalies],
            **llm_result,
        }
