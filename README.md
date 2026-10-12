# Health Anomaly Detector

A two-layer health anomaly detection pipeline for health data exported from
Apple Health, Google (Takeout or Health Connect) and Fitbit. Layer 1 runs a fast statistical pre-screen to catch genuine anomalies without wasting
API tokens. Layer 2 sends only the flagged readings to Claude for plain-English
interpretation.

---

## Architecture

```
 Apple Health    Google Takeout     Health Connect    Fitbit      Fitbit REST API
  export.zip    (Fit and/or Fitbit)   export.zip      archive       (optional)
       │               │                  │              │              │
       └───────────────┴──────┬───────────┴──────────────┘              │
                              ▼                                         ▼
                 adapters.load_export()                        adapters/fitbit.py
                 detects the source, reads it                           │
                              │                                         │
                              └──────────────────┬──────────────────────┘
                                                 │  list[HealthRecord]  (one per hour)
                                                 ▼
          vitals_monitor.py
          StatisticalScreener
          ┌─────────────────────────────┐
          │ 1. Hard physiological bounds│  ← catches acute events
          │ 2. Personal z-score         │  ← catches "unusual for YOU"
          │ 3. Multi-metric combo rules │  ← HR+SpO₂, sleep+HRV, etc.
          └─────────────────────────────┘
                   │  list[Anomaly]  (only if anomalies found)
                   ▼
       health_anomaly_detector.py
            LLMAnalyzer  →  Claude API
                   │
                   ▼
          Plain-English interpretation
          per anomaly + overall summary
```

## File Structure

```
├── gui.py                      # macOS GUI — drag & drop + results table + AI button
├── vitals_monitor.py           # HealthRecord, Anomaly, StatisticalScreener (Layer 1)
├── health_anomaly_detector.py  # LLMAnalyzer, HealthAnomalyDetector (Layer 2)
├── run_pipeline.py             # CLI entry point
├── requirements.txt
└── adapters/
    ├── __init__.py             # load_export(): detects the export type and reads it
    ├── common.py               # Zip/folder access, hourly bucketing shared by all sources
    ├── apple_health.py         # Apple Health export
    ├── google_fit.py           # Google Takeout → Fit
    ├── fitbit_export.py        # Google Takeout → Fitbit, or fitbit.com account archive
    ├── health_connect.py       # Android Health Connect export
    └── fitbit.py               # Fitbit REST API (optional, CLI only)
```

---

## Setup

Requires Python 3.10 or newer with Tk (the python.org installer includes it;
with Homebrew, also run `brew install python-tk`).

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run `source .venv/bin/activate` again in each new terminal before using the app.

### 2. Set your Anthropic API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Add it to your `~/.zshrc` or `~/.bash_profile` to make it permanent.
Without a key, everything except the AI step still works.

---

## Usage

The app only works with data you export yourself — it never generates or
substitutes sample data. Get an export from one of these:

| Source | How to export | What to drop on the app |
|---|---|---|
| **Apple Health** | iPhone Health app → profile photo → Export All Health Data | `export.zip` (or the unzipped `export.xml`) |
| **Google Fit** | [takeout.google.com](https://takeout.google.com) → select **Fit** | the Takeout `.zip` file(s), or the unzipped folder |
| **Fitbit** | [takeout.google.com](https://takeout.google.com) → select **Fitbit**, or fitbit.com → Settings → Data Export → Export Your Account Archive | the `.zip` file(s), or the unzipped folder |
| **Health Connect** (Android) | Settings → Health Connect → Export data | the export `.zip` (or the `.db` inside it) |

The app works out which source a file came from. Anything else is rejected
with a message. Large Takeout exports are split into several zips. Drop them
all at once.

### GUI (recommended)

```bash
python gui.py
```

Drag the export onto the drop zone, or click it to browse.

**What happens:**
1. The export is parsed in the background (large files show progress in the status bar)
2. Layer 1 results appear immediately in the colour-coded table
   - 🔴 **HIGH** — outside hard physiological bounds or >3.5 SD from your baseline
   - 🟡 **MEDIUM** — between 2.5–3.5 SD from your baseline
   - Z-scores are signed: `+` means above your baseline, `−` below
3. Click **🤖 Analyze with AI** to send flagged anomalies to Claude
4. Claude's plain-English interpretation appears in the panel below

### CLI

```bash
# Any supported export — stats only (no API call)
python run_pipeline.py --export ~/Downloads/export.zip --no-llm

# Full pipeline with LLM
python run_pipeline.py --export ~/Downloads/export.zip

# A Takeout split into parts
python run_pipeline.py --export takeout-001.zip takeout-002.zip

# With user context (helps the LLM interpret anomalies)
python run_pipeline.py --export export.zip \
    --context "I ran a half-marathon on Saturday"

# Output raw JSON for piping (progress messages go to stderr)
python run_pipeline.py --export export.zip --json
```

### Optional: live Fitbit API

Instead of exporting, the CLI can pull recent Fitbit data directly
(heart rate, SpO₂, breathing rate, skin temperature):

```bash
python run_pipeline.py --fitbit-api --days 7
```

This needs a one-time setup:

```bash
export FITBIT_CLIENT_ID="your_id"
export FITBIT_CLIENT_SECRET="your_secret"
python -m adapters.fitbit      # opens browser, saves token to .fitbit_token
```

Run this from the project folder. Get credentials at [dev.fitbit.com](https://dev.fitbit.com)
→ Create App → Personal, with redirect URI `http://localhost:8080`.

---

## Metrics Tracked

| Metric | Read from | Hard Bounds |
|---|---|---|
| Heart rate | Apple, Google Fit, Fitbit, Health Connect | 40–220 bpm |
| HRV | Apple (SDNN), Fitbit (RMSSD), Health Connect (RMSSD) | 0–300 ms |
| SpO₂ | Apple, Google Fit, Fitbit, Health Connect | 80–100 % |
| Respiratory rate | Apple, Fitbit (during sleep), Health Connect | 5–35 br/min |
| Body temperature | Apple, Google Fit, Health Connect | 35–41 °C |
| Skin temperature change | Fitbit (nightly, vs. your baseline) | ±3 °C |
| Blood glucose | Apple, Google Fit, Health Connect (mg/dL or mmol/L) | 50–400 mg/dL |
| Resting heart rate | Apple, Fitbit, Health Connect | 30–150 bpm |
| Sleep | Apple, Google Fit, Fitbit, Health Connect | used by the Sleep + HRV rule |
| ECG rhythm | Apple (Watch ECG recordings) | afib / high_hr / low_hr / inconclusive |
| Steps / workouts | all export sources | used to detect exercise (see below) |

Every metric with hard bounds is also checked against your own baseline (z-score).

**Exercise:** hours that overlap a recorded workout, or have 1,000+ steps, are
treated as active. Heart rate and respiratory rate in active hours skip the
baseline check (and are left out of the baseline), so normal workouts aren't
flagged. Hard bounds still apply. The live Fitbit API option doesn't fetch
workouts, so it doesn't get this.

### Multi-metric combo rules

| Rule | Condition | Severity |
|---|---|---|
| HR + SpO₂ | HR > 110 bpm AND SpO₂ < 94% | High |
| Sleep + HRV | Sleep < 5 h AND HRV < 20 ms (once per day) | Medium |
| RR + SpO₂ | Resp rate > 20 AND SpO₂ < 95% | High |
| ECG rhythm | Device flags afib / inconclusive / high or low HR | High (afib) / Medium |

---

## How exports are read

Every source is reduced to the same thing: individual readings grouped into
1-hour buckets, with each metric averaged within its bucket, producing one
`HealthRecord` per hour.

- **Duplicates across devices:** phones and watches often record the same
  steps, so steps are totalled per device and the busiest device is used
  rather than adding them up. Overlapping sleep records are merged.
- **Units:** glucose in mmol/L and temperatures in °F are converted.
- **Time zones:** Apple and Fitbit timestamps are used as written; Google Fit
  and Health Connect timestamps are converted to this computer's time zone
  (Health Connect uses the offset recorded with each reading).
- **Format changes:** Google and Fitbit change their export layouts from time
  to time. Files the app doesn't recognise inside an export are skipped. If an
  export loads but a metric you expected is missing, its layout has probably
  changed.

Apple Health and Health Connect have no web API, so exporting is the only way
to get their data into this app.

---

## Notes

- **ANTHROPIC_API_KEY** must be set for the LLM layer. Layer 1 (stats) works without it.
- A long export can flag hundreds of anomalies. Only the 30 most severe (one per
  metric per day) are sent to Claude in detail, along with counts for all of them.
- The **Google Fit API** was shut down in June 2025, but Google Fit history is still
  available through Google Takeout.
- This tool is **not a medical device** and does not provide medical diagnoses.
  Always consult a healthcare professional for anything concerning.
