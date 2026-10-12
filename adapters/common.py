"""
adapters/common.py

Shared plumbing for the export adapters:

  ExportArchive     — one interface over a .zip, an unzipped folder, or a single file
  ReadingCollector  — accumulates readings from any adapter and buckets them into
                      hourly HealthRecord objects
  parse_time        — tolerant timestamp parser for the formats the exports use
"""

import statistics
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from vitals_monitor import HealthRecord


# Fields that are averaged within each hourly bucket
AVERAGED_FIELDS = (
    "heart_rate", "hrv", "resting_hr", "spo2", "respiratory_rate",
    "body_temperature", "blood_glucose", "skin_temp_variation",
)

# Blood glucose: mmol/L × 18.016 = mg/dL
GLUCOSE_MMOL_TO_MGDL = 18.016


# ---------------------------------------------------------------------------
# Archive access
# ---------------------------------------------------------------------------

class ExportArchive:
    """
    Read-only view of an export, whatever shape it arrived in.  Names are
    forward-slash paths relative to the archive root, as in a zip.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        if not self.path.exists():
            raise FileNotFoundError(f"Export not found: {self.path}")
        self._zip: Optional[zipfile.ZipFile] = None

        if self.path.is_dir():
            # Names keep the folder's own name, so a dropped "Fit" or
            # "Takeout" folder looks the same as it would inside the zip
            self._root  = self.path.parent
            self._names = [
                p.relative_to(self._root).as_posix()
                for p in self.path.rglob("*") if p.is_file()
            ]
        elif zipfile.is_zipfile(self.path):
            self._zip   = zipfile.ZipFile(self.path)
            self._names = [n for n in self._zip.namelist() if not n.endswith("/")]
        else:
            # A single exported file, e.g. Apple's export.xml or Health Connect's .db.
            # Apple keeps ECGs in a sibling folder, so include that if present.
            self._root  = self.path.parent
            self._names = [self.path.name]
            ecg_dir = self._root / "electrocardiograms"
            if ecg_dir.is_dir():
                self._names += [f"electrocardiograms/{p.name}" for p in ecg_dir.glob("*.csv")]

    @property
    def label(self) -> str:
        return self.path.name

    def names(self) -> list[str]:
        return self._names

    def open(self, name: str):
        """Binary file object for one member."""
        if self._zip is not None:
            return self._zip.open(name)
        return open(self._root / name, "rb")

    def local_path(self, name: str) -> Optional[Path]:
        """Real filesystem path for a member, or None if it's inside a zip."""
        return None if self._zip is not None else self._root / name

    def close(self):
        if self._zip is not None:
            self._zip.close()


def basename(name: str) -> str:
    return name.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

_FORMATS = (
    "%Y-%m-%d %H:%M:%S",      # Apple (offset stripped below)
    "%m/%d/%y %H:%M:%S",      # Fitbit JSON exports
    "%Y-%m-%d",
)


def parse_time(raw) -> datetime:
    """
    Parse a timestamp string to a naive local datetime.  ISO strings with a
    'Z' or an offset are converted to this computer's local time; strings
    without zone information are taken as-is.
    """
    s = str(raw).strip()
    if "T" in s:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    if len(s) > 20 and s[-5] in "+-" and s[-6] == " ":
        s = s[:-6]            # Apple: '2024-01-15 08:32:00 -0500' → local wall time
    for fmt in _FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised timestamp: {raw!r}")


def from_epoch(seconds: float, utc_offset_seconds: Optional[int] = None) -> datetime:
    """Epoch → naive local datetime, using the recorded UTC offset when known."""
    if utc_offset_seconds is None:
        return datetime.fromtimestamp(seconds)
    utc = datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)
    return utc + timedelta(seconds=utc_offset_seconds)


def _hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class ReadingCollector:
    """
    Adapters push individual readings in; build() returns one HealthRecord
    per hour that had any data.  Several adapters (or several exports) can
    feed the same collector — overlapping data is handled so it isn't
    counted twice:
      • steps:  per-source totals per hour, then the busiest source wins
      • sleep:  overlapping intervals are merged before totalling
    """

    PROGRESS_EVERY = 10_000

    def __init__(self, progress_callback: Optional[Callable[[int], None]] = None):
        self._values: dict = defaultdict(lambda: defaultdict(list))   # hour → field → [v]
        self._steps:  dict = defaultdict(lambda: defaultdict(float))  # hour → source → n
        self._active_hours: set = set()
        self._sleep_intervals: list[tuple[datetime, datetime]] = []
        self._sleep_totals: dict[str, float] = {}                     # wake day → hours
        self._ecg: dict = {}
        self._progress = progress_callback
        self.n_readings = 0

    def _count(self):
        self.n_readings += 1
        if self._progress and self.n_readings % self.PROGRESS_EVERY == 0:
            self._progress(self.n_readings)

    def add(self, field: str, value: float, ts: datetime):
        self._values[_hour(ts)][field].append(float(value))
        self._count()

    def add_steps(self, ts: datetime, source: str, count: float):
        self._steps[_hour(ts)][source] += count
        self._count()

    def add_workout(self, start: datetime, end: datetime):
        hour = _hour(start)
        while hour <= end:
            self._active_hours.add(hour)
            hour += timedelta(hours=1)
        self._count()

    def add_sleep(self, start: datetime, end: datetime):
        """One asleep interval (stage or session)."""
        if end > start:
            self._sleep_intervals.append((start, end))
            self._count()

    def add_sleep_total(self, wake_day: str, hours: float):
        """A source that reports total time asleep directly (e.g. Fitbit)."""
        self._sleep_totals[wake_day] = max(self._sleep_totals.get(wake_day, 0.0), hours)
        self._count()

    def add_ecg(self, ts: datetime, rhythm: str):
        self._ecg[_hour(ts)] = rhythm
        self._count()

    def _sleep_by_day(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        merged: list[list[datetime]] = []
        for start, end in sorted(self._sleep_intervals):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            totals[end.date().isoformat()] += (end - start).total_seconds() / 3600
        for day, hours in self._sleep_totals.items():
            totals[day] = max(totals.get(day, 0.0), hours)
        return {day: round(h, 2) for day, h in totals.items()}

    def build(self) -> list[HealthRecord]:
        hours = set(self._values) | set(self._steps) | set(self._ecg)
        sleep = self._sleep_by_day()

        records = []
        for bucket_ts in sorted(hours):
            m = self._values.get(bucket_ts, {})
            per_source = self._steps.get(bucket_ts)
            fields = {
                f: round(statistics.mean(m[f]), 2) for f in AVERAGED_FIELDS if m.get(f)
            }
            records.append(HealthRecord(
                timestamp   = bucket_ts.isoformat(),
                steps       = int(max(per_source.values())) if per_source else None,
                sleep_hours = sleep.get(bucket_ts.date().isoformat()),
                ecg_rhythm  = self._ecg.get(bucket_ts),
                active      = True if bucket_ts in self._active_hours else None,
                **fields,
            ))
        return records
