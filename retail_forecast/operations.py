"""Verified local checkpoints and an isolated, executable recovery rehearsal.

Files in this module are private application state. The status API exposes
counts, hashes and bounded audit messages, never observation values or paths.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from threading import RLock
import time
from uuid import uuid4

from .quality import MAX_SNAPSHOT_BYTES, inspect_snapshot, parse_snapshot, read_snapshot, require_snapshot


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _rejected_bytes(value):
    # Quarantine preserves an invalid numeric value without publishing it.
    try:
        return json.dumps(value, sort_keys=True, allow_nan=True).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return b'{"quarantine_note":"Input could not be serialized"}'


def _write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _json(path, default=None):
    if not path.exists():
        return deepcopy(default)
    try:
        return json.loads(path.read_bytes())
    except (ValueError, OSError):
        raise ValueError("Local operations metadata is unreadable") from None


class InterruptedBatch(RuntimeError):
    """Injected after one real forecast artifact is written, before commit."""


class CheckpointStore:
    """A batch becomes visible only through a validated atomic active pointer."""

    def __init__(self, directory, build_payload):
        self.directory = Path(directory)
        self.build_payload = build_payload
        self.lock = RLock()
        self._write_depth = 0
        self.state = _json(self.directory / "journal.json", {
            "runs": [], "events": [], "quality": None, "expected_cutoff": None,
            "latest_drill": None,
        })

    @contextmanager
    def writer(self):
        """Reject simultaneous CLI/server writers while allowing nested calls."""
        with self.lock:
            if self._write_depth:
                self._write_depth += 1
                try:
                    yield
                finally:
                    self._write_depth -= 1
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            stream = (self.directory / "writer.lock").open("a+b")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                stream.close()
                raise ValueError("Another local operation is already writing this workspace; retry after it completes") from None
            self._write_depth = 1
            try:
                self.state = _json(self.directory / "journal.json", self.state)
                yield
            finally:
                self._write_depth = 0
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()

    def _save(self):
        self.state["runs"] = self.state["runs"][-50:]
        self.state["events"] = self.state["events"][-100:]
        _write(self.directory / "journal.json", _bytes(self.state))

    def event(self, kind, message, run_id=None):
        with self.writer():
            self.state["events"].append({"at": _now(), "type": kind,
                                         "message": message, "run_id": run_id})
            self._save()

    def _run(self, run_id):
        return next((run for run in self.state["runs"] if run["id"] == run_id), None)

    def _update_run(self, run_id, **fields):
        run = self._run(run_id)
        if run is not None:
            run.update(fields)
        self._save()

    @staticmethod
    def _version(token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", token):
            raise ValueError("request_token must contain 1 to 128 letters, numbers, dots, underscores or hyphens")
        return "batch-" + _hash(token.encode())[:32]

    def _validate_batch(self, version, manifest_hash=None):
        if not isinstance(version, str) or not re.fullmatch(r"batch-[a-f0-9]{32}", version):
            raise ValueError("Checkpoint identity is invalid")
        folder = self.directory / "snapshots" / version
        try:
            if any((folder / name).stat().st_size > MAX_SNAPSHOT_BYTES
                   for name in ("manifest.json", "dataset.json", "forecasts.json")):
                raise ValueError()
            manifest_bytes = (folder / "manifest.json").read_bytes()
            dataset_bytes = (folder / "dataset.json").read_bytes()
            forecast_bytes = (folder / "forecasts.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            dataset = json.loads(dataset_bytes)
            forecasts = json.loads(forecast_bytes)
            if manifest_hash is not None and _hash(manifest_bytes) != manifest_hash:
                raise ValueError()
            if (manifest["version"] != version or manifest["model"] != "seasonal"
                    or manifest["dataset_hash"] != _hash(dataset_bytes)
                    or manifest["forecast_hash"] != _hash(forecast_bytes)):
                raise ValueError()
            quality = inspect_snapshot(dataset, cutoff=manifest["cutoff"])
            if not quality["passed"]:
                raise ValueError()
            expected = {(series["store_id"], series["item_id"]) for series in dataset["series"]}
            if not isinstance(forecasts, list) or len(forecasts) != len(expected):
                raise ValueError()
            if manifest["series_count"] != len(expected) or manifest["horizon"] != 28:
                raise ValueError()
            seen = set()
            origin = date.fromisoformat(dataset["start_date"]) + timedelta(days=manifest["cutoff"])
            for payload in forecasts:
                key = (payload["store_id"], payload["item_id"])
                if key in seen or key not in expected or payload["cutoff"] != manifest["cutoff"]:
                    raise ValueError()
                seen.add(key)
                if (payload["model"] != "seasonal" or payload["source"] != dataset["source"]
                        or len(payload["forecast"]) != 28
                        or any(name.startswith("_") for name in payload)):
                    raise ValueError()
                for offset, point in enumerate(payload["forecast"]):
                    values = [point[name] for name in ("p10", "p50", "p90", "baseline")]
                    if (point["date"] != (origin + timedelta(days=offset)).isoformat()
                            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                                   or value < 0 or not math.isfinite(value) for value in values)
                            or not values[0] <= values[1] <= values[2]):
                        raise ValueError()
            _bytes(forecasts)  # Reject nonfinite numbers anywhere, including nested metrics.
            if seen != expected:
                raise ValueError()
            return {"manifest": manifest, "manifest_hash": _hash(manifest_bytes),
                    "dataset": dataset, "forecasts": forecasts, "forecast_bytes": forecast_bytes}
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            raise ValueError("Checkpoint artifacts are missing, incomplete or fail integrity validation") from None

    def active(self):
        pointer = _json(self.directory / "active.json")
        if pointer is None:
            return None
        try:
            batch = self._validate_batch(pointer["version"], pointer["manifest_hash"])
        except (KeyError, TypeError):
            raise ValueError("Active checkpoint pointer is invalid") from None
        return {**batch, "pointer": pointer}

    def serve_bytes(self):
        """Read the same verified artifact used by the application's forecast API."""
        active = self.active()
        if active is None:
            raise ValueError("No verified forecast checkpoint is available")
        return active["forecast_bytes"]

    def _quarantine(self, raw, report, run_id):
        folder = self.directory / "quarantine" / ("rejected-" + uuid4().hex)
        _write(folder / "source.json", raw)
        _write(folder / "quality.json", _bytes(report))
        self.state["quality"] = report
        self._update_run(run_id, status="quarantined", stage="data_quality",
                         finished_at=_now(), reason="Data quality failed; the active forecast was preserved.")
        self.event("quality_blocked", "Invalid input quarantined before forecasting; the active forecast stayed available.", run_id)

    def initialize(self, candidate=None, raw=None, source_path=None, approve_contract=False, before_commit=None,
                   checkpoint_token=None):
        with self.writer():
            active = self.active()
            previous = active["dataset"] if active else None
            cutoff = active["manifest"]["cutoff"] if active else None
            expected = None if approve_contract else previous
            if approve_contract:
                cutoff = None
            if source_path is not None:
                candidate, report = read_snapshot(source_path, cutoff=cutoff, expected=expected)
            elif raw is not None:
                candidate, report = parse_snapshot(raw, cutoff=cutoff, expected=expected)
            else:
                report = inspect_snapshot(candidate, cutoff=cutoff, expected=expected)
            self.state["quality"] = report
            if not report["passed"]:
                run_id = "source-" + uuid4().hex
                self.state["runs"].append({"id": run_id, "type": "source_validation", "status": "running",
                    "stage": "data_quality", "cutoff": cutoff, "started_at": _now(), "finished_at": None,
                    "reason": None, "scope": "Local rehearsal"})
                if source_path is not None:
                    with Path(source_path).open("rb") as source:
                        raw = source.read(MAX_SNAPSHOT_BYTES + 1)
                self._quarantine(raw if raw is not None else _rejected_bytes(candidate), report, run_id)
                if active is None:
                    raise ValueError("Input failed data quality checks and no verified previous forecast exists")
                return previous
            dataset_hash = _hash(_bytes(candidate))
            cutoff = max(cutoff, candidate["total_days"] - 28) if cutoff is not None else candidate["total_days"] - 28
            self.state["expected_cutoff"] = cutoff
            self._save()
            if active is None or active["manifest"]["dataset_hash"] != dataset_hash:
                token = checkpoint_token or ("import-" + uuid4().hex if approve_contract else
                                             "initial-" + dataset_hash[:24] + "-" + str(cutoff))
                self.publish(candidate, cutoff, token,
                             kind="checkpoint", approve_contract=approve_contract, before_commit=before_commit)
            if approve_contract:
                self.event("contract_approved", "An explicit dataset import approved a new source contract; earlier artifacts and audit events were retained.")
            return candidate

    def import_snapshot(self, source_path, dataset):
        """Approve an explicit import and keep source replacement transactional."""
        require_snapshot(dataset)  # No state or source mutation for invalid imports.
        with self.writer():
            source_path = Path(source_path)
            old_source = None
            if source_path.exists():
                with source_path.open("rb") as stream:
                    old_source = stream.read(MAX_SNAPSHOT_BYTES + 1)
                if len(old_source) > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Existing import source exceeds the local snapshot budget; use another output file")
            previous = self.active()
            pointer = previous["pointer"] if previous else None
            old_quality = deepcopy(self.state.get("quality"))
            old_expected = self.state.get("expected_cutoff")
            import_token = "import-" + uuid4().hex
            import_version = self._version(import_token)
            source_saved = False
            def save_source():
                nonlocal source_saved
                _write(source_path, _bytes(dataset))
                source_saved = True
            try:
                accepted = self.initialize(dataset, approve_contract=True, before_commit=save_source,
                                           checkpoint_token=import_token)
                imported = self.active()
                # Re-importing exactly the active dataset does not need a new
                # batch commit, but still saves its explicitly selected file.
                if not source_saved:
                    save_source()
            except OSError:
                if pointer is not None:
                    if self.active()["pointer"] != pointer:
                        self.rollback(pointer)
                else:
                    # Only the newly created active pointer is removed; all
                    # candidate artifacts and audit evidence are retained.
                    current = self.active()
                    if current is not None:
                        if current["manifest"]["version"] != import_version:
                            raise ValueError("Import failed and its checkpoint changed unexpectedly") from None
                        (self.directory / "active.json").unlink()
                if source_saved:
                    try:
                        if old_source is not None:
                            _write(source_path, old_source)
                        else:
                            source_path.unlink()
                    except OSError:
                        self.event("source_restore_failed", "Import failed and source restoration needs attention; the previous verified forecast remains protected.", import_version)
                        raise ValueError("Import failed; the previous forecast is protected but its source file could not be restored") from None
                self.state["quality"] = old_quality
                self.state["expected_cutoff"] = old_expected
                self._update_run(import_version, status="failed", stage="source_import",
                                 finished_at=_now(), reason="Import finalization failed; the previous source and active pointer were restored.")
                self.event("source_import_failed", "Import finalization failed; the previous source file and active forecast were preserved.", import_version)
                raise ValueError("Imported source could not be saved; the previous forecast was restored") from None
            self.event("source_imported", "An explicit validated source import was saved with its approved checkpoint.", imported["manifest"]["version"])
            return accepted

    def publish(self, dataset, cutoff, token, *, interrupt=False, kind="replay", approve_contract=False, before_commit=None):
        with self.writer():
            version = self._version(token)
            folder = self.directory / "snapshots" / version
            active = self.active()
            expected = active["dataset"] if active and not approve_contract else None
            report = inspect_snapshot(dataset, cutoff=cutoff, expected=expected)
            request = None
            if report["passed"]:
                request = {"dataset_hash": _hash(_bytes(dataset)), "cutoff": cutoff, "token": token}
                existing_request = _json(folder / "request.json")
                if existing_request is not None and existing_request != request:
                    raise ValueError("An idempotency token cannot be reused for a different snapshot")
            self.state["quality"] = report
            run = self._run(version)
            if run is None:
                run = {"id": version, "type": kind, "status": "running", "stage": "data_quality",
                       "cutoff": cutoff, "started_at": _now(), "finished_at": None,
                       "reason": None, "scope": "Local rehearsal"}
                self.state["runs"].append(run)
            self.state["expected_cutoff"] = cutoff if report["passed"] else self.state["expected_cutoff"]
            self._save()
            if not report["passed"]:
                self._quarantine(_rejected_bytes(dataset), report, version)
                return {"committed": False, "idempotent": False, "version": version}
            if (folder / "manifest.json").exists():
                prior = self._validate_batch(version)
                committed = _json(folder / "committed.json")
                if committed is not None and committed != {"manifest_hash": prior["manifest_hash"]}:
                    raise ValueError("Committed checkpoint record failed integrity validation")
                if committed is not None or (active and active["manifest"]["version"] == version):
                    _write(folder / "committed.json", _bytes({"manifest_hash": prior["manifest_hash"]}))
                    self._update_run(version, status="completed", stage="published", finished_at=_now(),
                                     reason="Previously committed batch verified; repeated request did not republish it.")
                    return {"committed": True, "idempotent": True, "version": version}
            _write(folder / "request.json", _bytes(request))
            _write(folder / "dataset.json", _bytes(dataset))
            self._update_run(version, status="running", stage="forecasting", finished_at=None,
                             reason="A complete seasonal batch is required before publication.")
            try:
                forecasts = []
                for series in dataset["series"]:
                    payload = self.build_payload(dataset, series["store_id"], series["item_id"], cutoff, "seasonal")
                    forecasts.append({key: value for key, value in payload.items() if not key.startswith("_")})
                    if interrupt:
                        _write(folder / "forecasts.json", _bytes(forecasts))
                        raise InterruptedBatch("Rehearsal interrupted after one forecast, before active-pointer commit")
                forecast_bytes = _bytes(forecasts)
                _write(folder / "forecasts.json", forecast_bytes)
                manifest = {"version": version, "model": "seasonal", "cutoff": cutoff,
                            "dataset_hash": request["dataset_hash"], "forecast_hash": _hash(forecast_bytes),
                            "series_count": len(forecasts), "horizon": 28, "created_at": _now(),
                            "committed_at": _now()}
                _write(folder / "manifest.json", _bytes(manifest))
                verified = self._validate_batch(version)
                if before_commit is not None:
                    before_commit()
                self._update_run(version, stage="commit")
                pointer = {"version": version, "manifest_hash": verified["manifest_hash"],
                           "previous": {key: active["pointer"][key] for key in ("version", "manifest_hash")} if active else None}
                _write(self.directory / "active.json", _bytes(pointer))
                _write(folder / "committed.json", _bytes({"manifest_hash": verified["manifest_hash"]}))
                self._update_run(version, status="completed", stage="published", finished_at=_now(),
                                 reason="Every selected series and artifact hash passed verification before commit.")
                self.event("checkpoint_published", "A complete verified seasonal forecast batch was published locally.", version)
                return {"committed": True, "idempotent": False, "version": version}
            except InterruptedBatch:
                self._update_run(version, status="interrupted", stage="forecasting", finished_at=_now(),
                                 reason="Interrupted before commit; incomplete forecasts remained invisible.")
                self.event("batch_interrupted", "An incomplete batch was kept separate from the active forecast.", version)
                raise
            except Exception:
                self._update_run(version, status="failed", stage="forecasting", finished_at=_now(),
                                 reason="Batch generation or integrity validation failed; the active forecast was preserved.")
                raise

    def rollback(self, pointer):
        with self.writer():
            # Validate actual files, not a saved success flag, before changing visibility.
            try:
                previous = self._validate_batch(pointer["version"], pointer["manifest_hash"])
            except (KeyError, TypeError):
                raise ValueError("Rollback checkpoint pointer is invalid") from None
            _write(self.directory / "active.json", _bytes(pointer))
            self.event("rollback_completed", "The previous verified forecast artifact was restored.", previous["manifest"]["version"])
            return previous


class LocalOperations(CheckpointStore):
    scope = "Local rehearsal"

    def status(self):
        with self.lock:
            self.state = _json(self.directory / "journal.json", self.state)
            alerts = []
            try:
                active = self.active()
            except ValueError:
                active = None
                alerts.append({"severity": "error", "message": "Active artifacts failed integrity verification; forecasts cannot be served safely."})
            quality = self.state["quality"]
            if quality is not None and not quality.get("passed"):
                alerts.append({"severity": "error", "message": "The latest source was quarantined. The previous verified forecast remains active when available."})
            elif quality is not None:
                alerts.extend({"severity": "warning", "message": issue["message"]}
                              for issue in quality.get("issues", [])[:3]
                              if issue.get("severity") == "warning")
            manifest = active["manifest"] if active else {}
            dataset = active["dataset"] if active else {}
            if quality is None:
                quality = {"passed": None, "checked_at": None, "source": dataset.get("source"),
                           "cutoff": None, "series_count": None, "snapshot_sha256": None,
                           "checks": [], "issues": []}
            cutoff = manifest.get("cutoff")
            expected = self.state.get("expected_cutoff")
            lag = max(0, expected - cutoff) if isinstance(expected, int) and isinstance(cutoff, int) else None
            if lag:
                alerts.append({"severity": "warning", "message": f"The active forecast is {lag} historical replay day(s) behind the expected cutoff."})
            latest_run = self.state["runs"][-1] if self.state["runs"] else None
            if latest_run and latest_run.get("status") in ("failed", "interrupted", "running"):
                alerts.append({"severity": "warning", "message": "The latest local batch has not recorded successful completion; inspect its audit record or retry it."})
            return {"mode": "local", "scope": self.scope,
                "source": dataset.get("source"), "source_label": dataset.get("source_label"),
                "summary": {"status": "attention" if alerts else "healthy" if active else "awaiting_publication",
                    "last_success_at": manifest.get("committed_at"), "active_cutoff": cutoff,
                    "active_as_of": (date.fromisoformat(dataset["start_date"]) + timedelta(days=cutoff - 1)).isoformat() if cutoff is not None else None,
                    "expected_cutoff": expected, "replay_lag_days": lag, "active_model": manifest.get("model"),
                    "active_version": manifest.get("version"), "snapshot_hash": manifest.get("forecast_hash"),
                    "series_count": manifest.get("series_count", 0)},
                "quality": quality, "runs": deepcopy(self.state["runs"][-20:][::-1]),
                "events": deepcopy(self.state["events"][-30:][::-1]),
                "latest_drill": deepcopy(self.state.get("latest_drill")),
                "capabilities": {"can_run_drill": True}, "alerts": alerts,
                "limitations": ["Local seasonal checkpoints are verified rehearsals; they do not report AWS execution or promote a trained candidate.",
                    "Freshness follows observed historical replay days, not the current calendar date.",
                    "No latency or uptime telemetry is collected by this dashboard."]}

    def run_drill(self, dataset, token=None):
        with self.writer():
            token = "drill-" + uuid4().hex if token is None else token
            version = self._version(token)
            drill_id = version.replace("batch-", "drill-")
            folder = self.directory / "drills" / drill_id
            previous = _json(folder / "report.json")
            if previous is not None:
                if previous.get("status") != "completed":
                    raise ValueError("This drill token already identifies an incomplete rehearsal; use a new token")
                self.state["latest_drill"] = previous
                self._save()
                return self.status()
            started = _now()
            report = {"id": drill_id, "status": "running", "started_at": started,
                      "finished_at": None, "recovery_seconds": None, "scope": "Isolated local recovery rehearsal",
                      "steps": [], "proof": {"invalid_snapshot_blocked": False, "interrupted_batch_hidden": False,
                          "retry_idempotent": False, "rollback_restored": False,
                          "forecast_hash_before": None, "forecast_hash_after": None}}
            _write(folder / "report.json", _bytes(report))
            self.state["latest_drill"] = report
            self._save()
            def step(identifier, label, success, detail):
                report["steps"].append({"id": identifier, "label": label,
                                        "status": "passed" if success else "failed", "detail": detail})
                if not success:
                    raise ValueError("Recovery drill verification failed: " + identifier)
            try:
                isolated = deepcopy(dataset)
                max_cutoff = isolated["total_days"] - 28
                baseline_cutoff = max(196, max_cutoff - 1)
                candidate_cutoff = min(baseline_cutoff + 1, max_cutoff)
                store = CheckpointStore(folder / "workspace", self.build_payload)
                result = store.publish(isolated, baseline_cutoff, "baseline", kind="drill_baseline")
                before = store.serve_bytes()
                baseline_pointer = store.active()["pointer"]
                report["proof"]["forecast_hash_before"] = _hash(before)
                step("baseline_commit", "Publish a verified baseline", result["committed"],
                     f"All {len(isolated['series'])} selected series were committed in the isolated workspace.")
                invalid = deepcopy(isolated)
                # A missing product is a real membership failure. With one series,
                # remove observations so the nonempty membership remains testable.
                if len(invalid["series"]) > 1:
                    invalid["series"].pop()
                else:
                    invalid["series"][0]["values"].pop()
                rejected = store.publish(invalid, candidate_cutoff, "invalid", kind="drill_candidate")
                blocked = not rejected["committed"] and store.serve_bytes() == before
                report["proof"]["invalid_snapshot_blocked"] = blocked
                step("quarantine_invalid", "Block and quarantine invalid data", blocked,
                     "The missing or incomplete input was quarantined; served baseline bytes were unchanged.")
                try:
                    store.publish(isolated, candidate_cutoff, "retryable", interrupt=True, kind="drill_candidate")
                except InterruptedBatch:
                    pass
                # Re-open the store after the simulated crash to verify persisted
                # files and the active pointer without relying on in-memory flags.
                store = CheckpointStore(folder / "workspace", self.build_payload)
                hidden = store.serve_bytes() == before
                report["proof"]["interrupted_batch_hidden"] = hidden
                step("interrupt_candidate", "Interrupt a batch before publication", hidden,
                     "One candidate forecast was written, then the store restarted; incomplete results stayed invisible.")
                recovery_started = time.perf_counter()
                retried = store.publish(isolated, candidate_cutoff, "retryable", kind="drill_candidate")
                retry_bytes = store.serve_bytes()
                active_version = store.active()["manifest"]["version"]
                duplicate = store.publish(isolated, candidate_cutoff, "retryable", kind="drill_candidate")
                once = (retried["committed"] and duplicate["idempotent"]
                        and store.serve_bytes() == retry_bytes
                        and store.active()["manifest"]["version"] == active_version
                        and len([run for run in store.state["runs"] if run["id"] == active_version]) == 1)
                report["proof"]["retry_idempotent"] = once
                step("retry_candidate", "Retry and publish exactly once", once,
                     "The complete retry committed once; repeating the same request token preserved identical bytes and version.")
                store.rollback(baseline_pointer)
                store = CheckpointStore(folder / "workspace", self.build_payload)
                after = store.serve_bytes()
                restored = after == before and store.active()["pointer"] == baseline_pointer
                report["proof"]["rollback_restored"] = restored
                report["proof"]["forecast_hash_after"] = _hash(after)
                report["recovery_seconds"] = round(time.perf_counter() - recovery_started, 6)
                step("rollback", "Restore the previous verified forecast", restored,
                     "Rollback revalidated artifact files; after restart, served bytes and SHA-256 matched the original baseline.")
                report["status"] = "completed"
            except Exception:
                report["status"] = "failed"
                if not report["steps"] or report["steps"][-1]["status"] != "failed":
                    report["steps"].append({"id": "execution", "label": "Complete the rehearsal",
                                            "status": "failed", "detail": "The rehearsal stopped without changing the application dataset or active forecast."})
            report["finished_at"] = _now()
            _write(folder / "report.json", _bytes(report))
            self.state["latest_drill"] = report
            self.event("recovery_drill_" + report["status"], "Isolated recovery rehearsal " + report["status"] + "; the application dataset and forecast were preserved.", drill_id)
            return self.status()
