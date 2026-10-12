"""
adapters package — turns health exports into list[HealthRecord]

    records, n_readings, sources = load_export(["~/Downloads/export.zip"])

Supported exports (detected automatically from their contents):
  • Apple Health        — Health app → Export All Health Data (.zip or export.xml)
  • Google Takeout      — Google Fit and/or Fitbit data (.zip, or the unzipped folder)
  • Health Connect      — Android Health Connect export (.zip or .db)
  • Fitbit export       — fitbit.com account archive (.zip, or the unzipped folder)

A large Takeout can arrive split across several zips; pass them all at once.
The live Fitbit API is separate: see adapters/fitbit.py.
"""

from typing import Callable, Optional

from adapters import apple_health, fitbit_export, google_fit, health_connect
from adapters.common import ExportArchive, ReadingCollector

_ADAPTERS = (apple_health, google_fit, fitbit_export, health_connect)

SUPPORTED = "Apple Health, Google Takeout (Google Fit / Fitbit), Health Connect, or Fitbit"


class UnsupportedExportError(ValueError):
    """The file isn't a recognised health export, or holds no usable readings."""


def load_export(
    paths: list,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> tuple[list, int, list[str]]:
    """
    Read one or more export files/folders and return
    (hourly HealthRecords, number of readings used, names of the sources found).
    """
    collector = ReadingCollector(progress_callback)
    sources: list[str] = []

    for path in paths:
        archive = ExportArchive(path)
        try:
            found = [a for a in _ADAPTERS if a.detect(archive)]
            if not found:
                raise UnsupportedExportError(
                    f"{archive.label} doesn't look like a health export. "
                    f"Export your data from {SUPPORTED} and use that file."
                )
            for adapter in found:
                adapter.read(archive, collector)
                if adapter.NAME not in sources:
                    sources.append(adapter.NAME)
        finally:
            archive.close()

    records = collector.build()
    if not records:
        raise UnsupportedExportError(
            f"Found a {' + '.join(sources)} export, but it has no readings this app "
            "can use (heart rate, HRV, SpO₂, respiratory rate, temperature, "
            "glucose, steps, sleep or ECG)."
        )
    return records, collector.n_readings, sources
