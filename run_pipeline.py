"""
run_pipeline.py  —  CLI entry point

Runs the full two-layer pipeline from the terminal.

Examples
────────
    # Any supported export — the source is detected from the file
    python run_pipeline.py --export ~/Downloads/export.zip

    # Google Takeout split into several parts
    python run_pipeline.py --export takeout-001.zip takeout-002.zip

    # Live Fitbit API (run `python -m adapters.fitbit` first to authorise)
    python run_pipeline.py --fitbit-api --days 7

    # Skip LLM layer (stats only, no API call)
    python run_pipeline.py --export export.zip --no-llm

    # Output raw JSON for piping into another process
    python run_pipeline.py --export export.zip --json
"""

import argparse
import json
import sys

from vitals_monitor import SEVERITY_ORDER, StatisticalScreener


def _log(msg: str):
    """Progress messages go to stderr so --json output on stdout stays clean."""
    print(msg, file=sys.stderr)


def _print_layer1(anomalies: list):
    for a in sorted(anomalies, key=lambda x: SEVERITY_ORDER.get(x.severity, 3)):
        badge = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(a.severity, "•")
        z     = f"  z={a.z_score:+.1f}σ" if a.z_score is not None else ""
        print(f"  {badge} [{a.severity.upper():6}] {a.metric:25}  "
              f"value={a.value!s:<8}{z}")
        print(f"            {a.reason}")
        print(f"            @ {a.timestamp[:16]}\n")

    if not anomalies:
        print("  ✅  No anomalies detected.")


def _print_llm(result: dict):
    reviewed, total = result.get("anomalies_reviewed"), result.get("anomalies_total")
    if reviewed is not None and reviewed < total:
        print(f"\n(Claude reviewed the {reviewed} most severe of {total} anomalies in detail.)")

    print(f"\n── Overall Summary ─────────────────────────────")
    print(result.get("overall_summary", ""))

    patterns = result.get("patterns_detected", [])
    if patterns:
        print(f"\n── Patterns Detected ───────────────────────────")
        for p in patterns:
            print(f"  • {p}")

    print(f"\n── Anomaly Assessments ─────────────────────────")
    for a in result.get("anomaly_assessments", []):
        print(f"\n  {a.get('metric')}  @  {a.get('timestamp','')[:16]}")
        print(f"  {a.get('plain_english')}")
        print(f"  Causes : {', '.join(a.get('possible_causes', []))}")
        print(f"  Action : {a.get('recommendation')}  [{a.get('urgency','').upper()}]")

    print("\n⚠  This is not medical advice.")


def main():
    parser = argparse.ArgumentParser(
        description="Health Anomaly Detector — CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--export", nargs="+", metavar="PATH",
                        help="Health export file(s) or unzipped folder: Apple Health, "
                             "Google Takeout, Health Connect, or Fitbit archive")
    source.add_argument("--fitbit-api", action="store_true",
                        help="Pull from the live Fitbit API instead of an export")
    parser.add_argument("--days",    type=int, default=1,
                        help="Days of history to pull with --fitbit-api (default: 1)")
    parser.add_argument("--no-llm",  action="store_true",
                        help="Run Layer 1 only — skip the Claude API call")
    parser.add_argument("--json",    action="store_true",
                        help="Output raw JSON (for piping)")
    parser.add_argument("--context", default="",
                        help="Optional user context sent to the LLM "
                             "(e.g. 'I ran a half-marathon yesterday')")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Load data
    # ------------------------------------------------------------------
    if args.export:
        from adapters import UnsupportedExportError, load_export
        try:
            records, n_raw, sources = load_export(args.export)
        except (FileNotFoundError, UnsupportedExportError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        _log(f"Loaded {' + '.join(sources)}: {n_raw:,} readings  →  "
             f"{len(records):,} hourly snapshots")

    else:
        from adapters.fitbit import FitbitAdapter
        records, n_raw = FitbitAdapter().load(days=args.days)
        _log(f"Loaded {n_raw:,} readings from Fitbit ({args.days} day(s))  →  "
             f"{len(records):,} snapshots")

    # ------------------------------------------------------------------
    # Layer 1 — Statistical screener
    # ------------------------------------------------------------------
    anomalies = StatisticalScreener(records).screen()
    _log(f"[Layer 1] {len(anomalies)} anomal{'y' if len(anomalies) == 1 else 'ies'} flagged")

    if not args.json:
        print()
        _print_layer1(anomalies)

    # ------------------------------------------------------------------
    # Layer 2 — LLM interpretation
    # ------------------------------------------------------------------
    result, llm_error, exit_code = None, None, 0
    if anomalies and not args.no_llm:
        from health_anomaly_detector import LLMAnalyzer, MissingCredentialsError
        _log("[Layer 2] Sending anomalies to Claude...")
        try:
            result = LLMAnalyzer().analyze(anomalies, user_context=args.context)
        except MissingCredentialsError as e:
            llm_error = f"{e} Skipping the AI step."
        except Exception as e:
            llm_error, exit_code = f"AI analysis failed: {e}", 1
        if llm_error:
            _log(f"WARNING: {llm_error}")

    if args.json:
        out = {
            "anomalies_found": len(anomalies),
            "anomalies": [
                {"metric": a.metric, "value": a.value,
                 "severity": a.severity, "reason": a.reason,
                 "z_score": a.z_score, "timestamp": a.timestamp}
                for a in anomalies
            ],
        }
        if result is not None:
            out["llm"] = result
        if llm_error:
            out["llm_error"] = llm_error
        print(json.dumps(out, indent=2))
    elif result is not None:
        _print_llm(result)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
