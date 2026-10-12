"""
adapters/health_connect.py

Reads an Android Health Connect export into a ReadingCollector.

Export process on Android:
    Settings → Health Connect → Export data (or "Backup and restore" →
    Export) → choose a location → the export is a .zip containing a
    SQLite database (health_connect_export.db)

Health Connect gathers data from every app that writes to it (Samsung
Health, Fitbit, Google Fit, Oura, ...).  The database layout can differ
between Android versions, so each table and column is checked before it
is read and anything missing is skipped.

Tables supported
─────────────────
  heart_rate_record_table (+ _series_table)        → heart_rate       (bpm)
  resting_heart_rate_record_table                  → resting_hr       (bpm)
  heart_rate_variability_rmssd_record_table        → hrv              (ms)
  oxygen_saturation_record_table                   → spo2             (%)
  respiratory_rate_record_table                    → respiratory_rate (br/min)
  body_temperature_record_table                    → body_temperature (°C)
  blood_glucose_record_table                       → blood_glucose    (stored as mmol/L → mg/dL)
  steps_record_table                               → steps
  sleep_session_record_table (+ sleep_stages_table) → sleep_hours
  exercise_session_record_table                    → active
"""

import os
import shutil
import sqlite3
import tempfile

from adapters.common import (
    GLUCOSE_MMOL_TO_MGDL, ExportArchive, ReadingCollector, from_epoch,
)

NAME = "Health Connect"

# (table, value column, HealthRecord field, multiplier) — instantaneous records
INSTANT_TABLES = [
    ("resting_heart_rate_record_table",           "beats_per_minute",              "resting_hr",       1.0),
    ("heart_rate_variability_rmssd_record_table", "heart_rate_variability_millis", "hrv",              1.0),
    ("oxygen_saturation_record_table",            "percentage",                    "spo2",             1.0),
    ("respiratory_rate_record_table",             "rate",                          "respiratory_rate", 1.0),
    ("body_temperature_record_table",             "temperature",                   "body_temperature", 1.0),
    ("blood_glucose_record_table",                "level",                         "blood_glucose",    GLUCOSE_MMOL_TO_MGDL),
]

# sleep_stages_table stage_type: 1 awake, 2 sleeping, 3 out of bed, 4 light, 5 deep, 6 REM, 7 awake in bed
_ASLEEP_STAGES = {2, 4, 5, 6}


def _database(archive: ExportArchive):
    return next((n for n in archive.names() if n.lower().endswith(".db")), None)


def detect(archive: ExportArchive) -> bool:
    db = _database(archive)
    return db is not None and "health" in db.lower()


def _time(ms, offset):
    return from_epoch(ms / 1000, offset)


class _Db:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    def columns(self, table: str) -> set:
        if table not in self.tables:
            return set()
        return {r[1] for r in self.conn.execute(f'PRAGMA table_info("{table}")')}

    def has(self, table: str, *cols: str) -> bool:
        return set(cols) <= self.columns(table)

    def rows(self, sql: str):
        return self.conn.execute(sql)


def _offset_col(db: _Db, table: str, name: str) -> str:
    """The UTC-offset column if the table has one, else NULL (→ local time)."""
    return name if name in db.columns(table) else "NULL"


def _read_db(db: _Db, collector: ReadingCollector):
    # Heart rate is stored as a series of samples under each record
    if db.has("heart_rate_record_series_table", "beats_per_minute", "epoch_millis", "parent_key") \
            and db.has("heart_rate_record_table", "row_id"):
        off = _offset_col(db, "heart_rate_record_table", "start_zone_offset")
        if off != "NULL":
            off = f"r.{off}"
        for bpm, ms, offset in db.rows(
            f"SELECT s.beats_per_minute, s.epoch_millis, {off} "
            f"FROM heart_rate_record_series_table s "
            f"JOIN heart_rate_record_table r ON s.parent_key = r.row_id"
        ):
            if bpm:
                collector.add("heart_rate", bpm, _time(ms, offset))

    for table, col, field, mult in INSTANT_TABLES:
        if not db.has(table, col, "time"):
            continue
        off = _offset_col(db, table, "zone_offset")
        for value, ms, offset in db.rows(f"SELECT {col}, time, {off} FROM {table}"):
            if value is not None:
                collector.add(field, value * mult, _time(ms, offset))

    if db.has("steps_record_table", "count", "start_time"):
        off = _offset_col(db, "steps_record_table", "start_zone_offset")
        src = "app_info_id" if "app_info_id" in db.columns("steps_record_table") else "NULL"
        for count, ms, offset, app in db.rows(
            f"SELECT count, start_time, {off}, {src} FROM steps_record_table"
        ):
            if count:
                # Phones and watches both write steps — tracked per app
                collector.add_steps(_time(ms, offset), f"app-{app}", count)

    if db.has("exercise_session_record_table", "start_time", "end_time"):
        s_off = _offset_col(db, "exercise_session_record_table", "start_zone_offset")
        e_off = _offset_col(db, "exercise_session_record_table", "end_zone_offset")
        for start, end, so, eo in db.rows(
            f"SELECT start_time, end_time, {s_off}, {e_off} FROM exercise_session_record_table"
        ):
            collector.add_workout(_time(start, so), _time(end, eo))

    _read_sleep(db, collector)


def _read_sleep(db: _Db, collector: ReadingCollector):
    if not db.has("sleep_session_record_table", "start_time", "end_time"):
        return
    s_off = _offset_col(db, "sleep_session_record_table", "start_zone_offset")
    e_off = _offset_col(db, "sleep_session_record_table", "end_zone_offset")
    has_id = "row_id" in db.columns("sleep_session_record_table")

    # Asleep stages per session, when the stages table exists
    stages: dict = {}
    if has_id and db.has("sleep_stages_table",
                         "parent_key", "stage_start_time", "stage_end_time", "stage_type"):
        for parent, start, end, stage in db.rows(
            "SELECT parent_key, stage_start_time, stage_end_time, stage_type FROM sleep_stages_table"
        ):
            stages.setdefault(parent, []).append((start, end, stage))

    row_id = "row_id" if has_id else "NULL"
    for rid, start, end, so, eo in db.rows(
        f"SELECT {row_id}, start_time, end_time, {s_off}, {e_off} FROM sleep_session_record_table"
    ):
        session_stages = stages.get(rid)
        if session_stages:
            for s, e, stage in session_stages:
                if stage in _ASLEEP_STAGES:
                    collector.add_sleep(_time(s, so), _time(e, eo))
        else:
            collector.add_sleep(_time(start, so), _time(end, eo))


def read(archive: ExportArchive, collector: ReadingCollector):
    name = _database(archive)
    path = archive.local_path(name)
    tmp  = None
    if path is None:
        # sqlite needs a real file — copy the database out of the zip
        fd, tmp = tempfile.mkstemp(suffix=".db")
        with os.fdopen(fd, "wb") as out, archive.open(name) as src:
            shutil.copyfileobj(src, out)
        path = tmp
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            _read_db(_Db(conn), collector)
        finally:
            conn.close()
    finally:
        if tmp:
            os.remove(tmp)
