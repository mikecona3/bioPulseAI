"""
adapters/fitbit.py

Fitbit REST API adapter — best Android-side option after Google Fit shutdown
(June 2025).  Returns list[HealthRecord] compatible with StatisticalScreener.

SETUP (one-time)
─────────────────
1. dev.fitbit.com → Create App → Application type: Personal
2. OAuth 2.0 redirect URI: http://localhost:8080
3. Set env vars:
       export FITBIT_CLIENT_ID="your_id"
       export FITBIT_CLIENT_SECRET="your_secret"
4. First-run auth:
       python -m adapters.fitbit
   Browser opens → approve → tokens saved to .fitbit_token in the project folder

Usage:
    records, n = FitbitAdapter().load(days=7)
    screener   = StatisticalScreener(records)
"""

import json
import os
import statistics
import time
import webbrowser
from collections import defaultdict
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from vitals_monitor import HealthRecord


CLIENT_ID     = os.getenv("FITBIT_CLIENT_ID",     "YOUR_CLIENT_ID")
CLIENT_SECRET = os.getenv("FITBIT_CLIENT_SECRET", "YOUR_CLIENT_SECRET")
REDIRECT_URI  = "http://localhost:8080"
TOKEN_FILE    = Path(__file__).resolve().parent.parent / ".fitbit_token"
FITBIT_BASE   = "https://api.fitbit.com"

SCOPES = ["heartrate", "oxygen_saturation", "respiratory_rate", "temperature"]


# ---------------------------------------------------------------------------
# Response extractors — each returns [(field_name, value, raw_timestamp)]
# ---------------------------------------------------------------------------

def _extract_heart_rate(data: dict, target_date: str):
    return [
        ("heart_rate", e["value"], f"{target_date} {e['time']}")
        for e in data.get("activities-heart-intraday", {}).get("dataset", [])
    ]

def _extract_spo2(data, target_date: str):
    out = []
    if isinstance(data, dict):
        for e in data.get("minutes", []):
            out.append(("spo2", e.get("value"), e.get("minute", target_date)))
        if not out:
            avg = data.get("value", {}).get("avg")
            if avg:
                out.append(("spo2", avg, target_date))
    return out

def _entries(data, key: str) -> list:
    """Fitbit summary responses wrap their list in a key, e.g. {"br": [...]}."""
    if isinstance(data, dict):
        data = data.get(key, [])
    return data if isinstance(data, list) else []

def _extract_resp(data, target_date: str):
    return [
        ("respiratory_rate", e.get("value", {}).get("breathingRate"), e.get("dateTime", target_date))
        for e in _entries(data, "br")
        if e.get("value", {}).get("breathingRate")
    ]

def _extract_temp(data, target_date: str):
    # nightlyRelative is the change from the user's own baseline (°C), not an
    # absolute temperature — it maps to skin_temp_variation
    return [
        ("skin_temp_variation", e.get("value", {}).get("nightlyRelative"), e.get("dateTime", target_date))
        for e in _entries(data, "tempSkin")
        if e.get("value", {}).get("nightlyRelative") is not None
    ]

ENDPOINTS = {
    "/1/user/-/activities/heart/date/{date}/1d/1min.json": _extract_heart_rate,
    "/1/user/-/spo2/date/{date}/all.json":                  _extract_spo2,
    "/1/user/-/br/date/{date}/{date}.json":                 _extract_resp,
    "/1/user/-/temp/skin/date/{date}/{date}.json":          _extract_temp,
}


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    code: Optional[str] = None
    def do_GET(self):
        _CallbackHandler.code = parse_qs(urlparse(self.path).query).get("code", [None])[0]
        self.send_response(200); self.end_headers()
        self.wfile.write(b"<h2>Authorised \xe2\x9c\x93 \xe2\x80\x94 close this tab.</h2>")
    def log_message(self, *_): pass


def _run_oauth() -> dict:
    url = "https://www.fitbit.com/oauth2/authorize?" + urlencode({
        "client_id": CLIENT_ID, "response_type": "code",
        "scope": " ".join(SCOPES), "redirect_uri": REDIRECT_URI,
    })
    print(f"\nOpening Fitbit auth...\n{url}\n")
    webbrowser.open(url)
    server = HTTPServer(("localhost", 8080), _CallbackHandler)
    while _CallbackHandler.code is None:
        server.handle_request()
    # Fitbit expects the client secret as HTTP Basic auth, not in the form body
    resp = requests.post("https://api.fitbit.com/oauth2/token", auth=(CLIENT_ID, CLIENT_SECRET), data={
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI, "code": _CallbackHandler.code,
    })
    resp.raise_for_status()
    tokens = resp.json()
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    TOKEN_FILE.write_text(json.dumps(tokens))
    return tokens

def _load_tokens() -> dict:
    return json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else _run_oauth()

def _refresh(tokens: dict) -> dict:
    if time.time() < tokens.get("expires_at", 0) - 60:
        return tokens
    resp = requests.post("https://api.fitbit.com/oauth2/token", auth=(CLIENT_ID, CLIENT_SECRET), data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
    })
    resp.raise_for_status()
    new = resp.json()
    new["expires_at"] = time.time() + new.get("expires_in", 3600)
    TOKEN_FILE.write_text(json.dumps(new))
    return new


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

def _parse_fitbit_ts(raw: str, base_date: str) -> datetime:
    s = str(raw).rstrip("Z")
    if "T" in s:    return datetime.fromisoformat(s)
    if len(s) == 10: return datetime.strptime(s, "%Y-%m-%d")
    return datetime.strptime(f"{base_date} {s}", "%Y-%m-%d %H:%M:%S")

def _avg(vals): return round(statistics.mean(vals), 2) if vals else None


class FitbitAdapter:
    """Pulls Fitbit vitals and returns HealthRecord objects."""

    def __init__(self):
        self._tokens = _load_tokens()

    def _get(self, path: str) -> dict:
        self._tokens = _refresh(self._tokens)
        r = requests.get(FITBIT_BASE + path,
                         headers={"Authorization": f"Bearer {self._tokens['access_token']}"},
                         timeout=10)
        r.raise_for_status()
        return r.json()

    def load(
        self,
        days: int = 1,
        progress_callback: Optional[Callable] = None,
    ) -> tuple[list[HealthRecord], int]:
        """
        Pull *days* of data and return (records, n_raw_readings).
        """
        buckets: dict = defaultdict(lambda: defaultdict(list))
        n_raw = 0

        for offset in range(days):
            target = (date.today() - timedelta(days=offset)).isoformat()
            for endpoint_tpl, extractor in ENDPOINTS.items():
                endpoint = endpoint_tpl.format(date=target)
                if progress_callback:
                    progress_callback(target, endpoint)
                try:
                    data = self._get(endpoint)
                except requests.HTTPError:
                    continue

                for field, value, raw_ts in extractor(data, target):
                    if value is None:
                        continue
                    try:
                        ts     = _parse_fitbit_ts(raw_ts, target)
                        bucket = ts.replace(minute=0, second=0, microsecond=0)
                        buckets[bucket][field].append(float(value))
                        n_raw += 1
                    except (ValueError, TypeError):
                        continue

        records = []
        for bucket_ts in sorted(buckets.keys()):
            m = buckets[bucket_ts]
            records.append(HealthRecord(
                timestamp        = bucket_ts.isoformat(),
                heart_rate       = _avg(m.get("heart_rate")),
                spo2             = _avg(m.get("spo2")),
                respiratory_rate = _avg(m.get("respiratory_rate")),
                skin_temp_variation = _avg(m.get("skin_temp_variation")),
            ))

        return records, n_raw


if __name__ == "__main__":
    print("Fitbit OAuth setup")
    _load_tokens()
    print("Done.")
