"""Bounded snapshot checks shared by local recovery and AWS ingestion.

Reports contain counts and identities, never sales observations. Business shifts
are review warnings, while malformed data blocks training and publication.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re

# The deployed ingestion function has 512 MiB memory. This application validates
# a scoped product selection in memory; full M5 needs chunked ingestion rather
# than accepting a 59-million-observation JSON document into that function.
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_OBSERVATIONS = 1_000_000
MAX_SERIES = 50_000


class SnapshotQualityError(ValueError):
    """A readable exception that preserves the structured quality evidence."""

    def __init__(self, report):
        self.report = report
        codes = [issue["code"] for issue in report["issues"] if issue["severity"] == "error"]
        super().__init__("Snapshot failed quality gate: " + ", ".join(codes))


def _whole(value, minimum, maximum):
    if isinstance(value, Decimal):
        return value.is_finite() and minimum <= value <= maximum and value == int(value)
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and minimum <= value <= maximum and math.isfinite(value) and value == int(value))


def _integer(value, minimum, maximum):
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _text(value, maximum=256):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and all(ord(char) >= 32 for char in value))


def _identity(series):
    if not isinstance(series, dict):
        return None
    values = series.get("store_id"), series.get("item_id")
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
           for value in values):
        return None
    return values


def _report(source=None, cutoff=None, count=0, digest=None):
    return {"passed": False, "checked_at": datetime.now(timezone.utc).isoformat(),
            "source": source if source in ("synthetic", "m5") else None,
            "cutoff": int(cutoff) if _whole(cutoff, 0, 1_000_000) else None,
            "series_count": count, "snapshot_sha256": digest, "checks": [], "issues": []}


def _check(report, code, label, passed, detail, *, warning=False):
    status = "passed" if passed else "warning" if warning else "failed"
    report["checks"].append({"id": code, "label": label, "status": status, "detail": detail})
    if not passed:
        report["issues"].append({"code": code, "severity": "warning" if warning else "error", "message": detail})


def oversized_snapshot(*, cutoff=None, source=None, series_count=0):
    """Return bounded evidence without reading or echoing oversized content."""
    report = _report(source, cutoff, series_count)
    _check(report, "snapshot_budget", "Scoped snapshot resource budget", False,
           "Snapshot exceeds the scoped ingestion budget: at most 16 MiB JSON and 1,000,000 daily observations. Select fewer products; full M5 requires chunked ingestion.")
    return report


def inspect_snapshot(dataset, *, cutoff=None, expected=None):
    """Inspect a normalized snapshot without fitting a model or changing files.

    ``expected`` can be an accepted dataset, a catalog, or its product list. Its
    identities must match exactly; source/start date stay stable, and later
    arrivals can extend total_days without shrinking accepted history.
    Sales shifts inspect observed history only, excluding replay scoring labels.
    """
    root = dataset if isinstance(dataset, dict) else {}
    entries = root.get("series")
    series = entries if isinstance(entries, list) else []
    total = root.get("total_days")
    selected_cutoff = cutoff if cutoff is not None else total - 28 if _integer(total, 224, 1_000_000) else None
    report = _report(root.get("source"), selected_cutoff, len(series))
    declared_count = total * len(series) if _integer(total, 224, 1_000_000) else 0
    actual_count = 0
    if len(series) <= MAX_SERIES and declared_count <= MAX_OBSERVATIONS:
        for item in series:
            if isinstance(item, dict) and isinstance(item.get("values"), list):
                actual_count += len(item["values"])
                if actual_count > MAX_OBSERVATIONS:
                    break
    if len(series) > MAX_SERIES or declared_count > MAX_OBSERVATIONS or actual_count > MAX_OBSERVATIONS:
        return oversized_snapshot(cutoff=selected_cutoff, source=root.get("source"), series_count=len(series))
    try:
        # Incremental canonical hashing avoids allocating a second complete
        # snapshot. The encoder emits ASCII, so character and byte counts agree.
        encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256()
        encoded_size = 0
        for chunk in encoder.iterencode(dataset):
            encoded_size += len(chunk)
            if encoded_size > MAX_SNAPSHOT_BYTES:
                return oversized_snapshot(cutoff=selected_cutoff, source=root.get("source"), series_count=len(series))
            digest.update(chunk.encode("utf-8"))
        report["snapshot_sha256"] = digest.hexdigest()
        serializable = True
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        serializable = False

    structure_ok = (isinstance(dataset, dict) and isinstance(root.get("metadata"), dict)
                    and isinstance(entries, list) and 1 <= len(series) <= MAX_SERIES and serializable)
    _check(report, "structure", "Normalized snapshot", structure_ok,
           "Snapshot is a finite JSON object with metadata and 1–50,000 series." if structure_ok else
           "Snapshot must be a finite JSON object with metadata and 1–50,000 series.")
    source_ok = root.get("source") in ("synthetic", "m5") and _text(root.get("source_label"), 512)
    _check(report, "source", "Source disclosure", source_ok,
           "Source and display label are present." if source_ok else
           "Declare source as synthetic or m5 and provide a nonempty source label.")
    try:
        start = date.fromisoformat(root.get("start_date"))
        calendar_ok = root["start_date"] == start.isoformat() and _integer(total, 224, 1_000_000)
        if calendar_ok:
            start + timedelta(days=int(total) + 27)
    except (TypeError, ValueError, OverflowError):
        calendar_ok = False
    _check(report, "calendar", "Daily calendar", calendar_ok,
           "Daily start date and dataset length are valid." if calendar_ok else
           "Use an ISO daily start date and an integer total_days between 224 and 1,000,000 within calendar bounds.")
    cutoff_ok = calendar_ok and _integer(selected_cutoff, 196, int(total) - 28)
    _check(report, "replay_window", "Replay window", cutoff_ok,
           "Replay contains at least 196 observed days and leaves 28 scoring days." if cutoff_ok else
           "Replay cutoff must be an integer with at least 196 observed days and 28 later scoring days.")

    identities = [_identity(item) for item in series]
    valid_ids = [identity for identity in identities if identity is not None]
    invalid_ids = len(identities) - len(valid_ids)
    duplicate_ids = len(valid_ids) - len(set(valid_ids))
    identities_ok = bool(series) and not invalid_ids and not duplicate_ids
    _check(report, "identities", "Unique store and product identities", identities_ok,
           "All store/product identities are unique and safe." if identities_ok else
           f"Snapshot has {invalid_ids} invalid identities and {duplicate_ids} duplicate store/product identities.")

    fields_bad = 0
    sales_bad = 0
    shifts = 0
    for item in series:
        if not isinstance(item, dict):
            fields_bad += 1
            sales_bad += 1
            continue
        price = item.get("price")
        if (any(not _text(item.get(field)) for field in ("name", "category", "department"))
                or isinstance(price, bool) or not isinstance(price, (int, float))
                or not 0 < price <= 100_000_000 or not math.isfinite(price)):
            fields_bad += 1
        values = item.get("values")
        valid_sales = (calendar_ok and isinstance(values, list) and len(values) == total
                       and all(_whole(value, 0, 100_000_000) for value in values))
        if not valid_sales:
            sales_bad += 1
        elif cutoff_ok:
            observed = values[:int(selected_cutoff)]
            previous = sum(observed[-84:-28]) / 56
            recent = sum(observed[-28:]) / 28
            # A heuristic review flag, not a statistical drift detector or an
            # assertion that a real business change is invalid demand.
            if ((previous >= 2 and abs(recent - previous) >= 2
                 and (recent >= previous * 3 or recent <= previous / 3))
                    or (previous == 0 and recent >= 5)):
                shifts += 1
    _check(report, "product_fields", "Product display fields", bool(series) and fields_bad == 0,
           "All products have names, category, department and finite positive descriptive prices." if series and not fields_bad else
           f"{fields_bad} series have missing or invalid product display fields or descriptive prices.")
    _check(report, "daily_sales", "Complete nonnegative daily sales", bool(series) and sales_bad == 0,
           "Every series has exactly total_days finite, nonnegative whole sales observations." if series and not sales_bad else
           f"{sales_bad} series have incorrect daily lengths or invalid sales observations; observations must be whole values from 0 to 100,000,000.")

    if expected is not None:
        expected_root = expected if isinstance(expected, dict) else {}
        expected_entries = expected if isinstance(expected, list) else expected_root.get("series", expected_root.get("products"))
        expected_ids = [_identity(item) for item in expected_entries] if isinstance(expected_entries, list) else []
        expected_valid = bool(expected_ids) and None not in expected_ids and len(set(expected_ids)) == len(expected_ids)
        missing = len(set(expected_ids) - set(valid_ids)) if expected_valid else 0
        extra = len(set(valid_ids) - set(expected_ids)) if expected_valid else 0
        calendar_changed = any(field in expected_root and root.get(field) != expected_root[field]
                               for field in ("source", "start_date"))
        expected_days = expected_root.get("total_days")
        shortened = ("total_days" in expected_root and
                     (not _whole(expected_days, 224, 1_000_000) or not _integer(total, 224, 1_000_000)
                      or total < expected_days))
        calendar_changed = calendar_changed or shortened
        matched = expected_valid and not missing and not extra and not calendar_changed
        _check(report, "expected_snapshot", "Accepted catalog agreement", matched,
               "Snapshot agrees with the accepted catalog identities and source/start bounds; accepted history has not shrunk." if matched else
               f"Snapshot differs from the accepted catalog: {missing} missing products, {extra} unexpected products; calendar/source changed: {calendar_changed}; expected catalog valid: {expected_valid}.")
    _check(report, "observed_sales_shift", "Observed sales shift review", not shifts,
           "No large sales-shift heuristic flag in the observed replay history." if not shifts else
           f"{shifts} series have a large observed sales shift. Review business context before promotion; this warning does not establish data corruption.", warning=True)
    report["passed"] = not any(issue["severity"] == "error" for issue in report["issues"])
    return report


def require_snapshot(dataset, *, cutoff=None, expected=None):
    report = inspect_snapshot(dataset, cutoff=cutoff, expected=expected)
    if not report["passed"]:
        raise SnapshotQualityError(report)
    return report


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON object key")
        value[key] = item
    return value


def _nonfinite(_):
    raise ValueError("Non-finite JSON")


def parse_snapshot(raw, *, cutoff=None, expected=None):
    """Parse bytes/text with a bounded report, without exposing malformed content."""
    if isinstance(raw, (bytes, str)):
        if len(raw) > MAX_SNAPSHOT_BYTES:
            return None, oversized_snapshot(cutoff=cutoff)
        if isinstance(raw, str):
            try:
                if len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
                    return None, oversized_snapshot(cutoff=cutoff)
            except UnicodeError:
                pass  # The ordinary unreadable-JSON path below handles it.
    try:
        dataset = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        report = _report(cutoff=cutoff)
        if isinstance(raw, (bytes, str)):
            try:
                report["snapshot_sha256"] = hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode("utf-8")).hexdigest()
            except UnicodeError:
                pass
        _check(report, "snapshot_parse", "Readable snapshot JSON", False,
               "Snapshot must be valid UTF-8 JSON with unique object keys and finite numbers.")
        return None, report
    return dataset, inspect_snapshot(dataset, cutoff=cutoff, expected=expected)


def read_snapshot(path, *, cutoff=None, expected=None):
    """Inspect a file with a byte cap before allocating or parsing its body."""
    path = Path(path)
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        return None, oversized_snapshot(cutoff=cutoff)
    with path.open("rb") as stream:
        raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
    return parse_snapshot(raw, cutoff=cutoff, expected=expected)
