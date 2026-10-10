"""Read-only workspace snapshot export and offline replacement-table comparison.

Offline commands need only Python's standard library. AWS is contacted only by
the explicit export command, after account and table identity checks. Snapshots
contain private workspace data; keep them in ignored build/ or data/ directories.
An exact comparison proves equality with the chosen baseline, not a live restore
or application cutover.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re

from aws.schema import RepositoryUnavailable, SECTIONS, entity_items
from orderflow.engine import validate_state


SNAPSHOT_SCHEMA = "orderflow.workspace-snapshot"
SNAPSHOT_VERSION = 1
MAX_INPUT_BYTES = 4_000_000
MAX_JSON_DEPTH = 100
_MANIFEST_FIELDS = {"schema", "version", "revision", "state_sha256", "state"}


class RecoveryError(ValueError):
    """A snapshot or explicitly selected AWS read could not be verified."""


def _check_json(value):
    # Reject non-JSON Python values, non-string object keys and non-finite
    # numbers before hashing; bound recursion even for in-memory callers.
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise RecoveryError("JSON exceeds the snapshot nesting limit.")
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise RecoveryError("JSON object keys must be strings.")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is float:
            if not math.isfinite(item):
                raise RecoveryError("JSON numbers must be finite.")
        elif item is not None and type(item) not in (str, int, bool):
            raise RecoveryError("Snapshot contains a non-JSON value.")


def canonical_json(value):
    """Deterministic UTF-8 JSON bytes, used for both state and entity digests."""
    _check_json(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise RecoveryError("Snapshot cannot be represented as finite UTF-8 JSON.") from error


def _validated_state(state):
    encoded = canonical_json(state)
    if len(encoded) > MAX_INPUT_BYTES:
        raise RecoveryError("Workspace exceeds the snapshot file size limit.")
    try:
        validate_state(state)
        entity_items(state)
    except (ValueError, TypeError, KeyError, AttributeError, RepositoryUnavailable) as error:
        # Domain errors and Python errors from malformed references must never
        # include customer values, entity identifiers or raw payloads in output.
        raise RecoveryError("Workspace schema, capacity or recovery invariants are invalid.") from error
    return encoded


def make_snapshot(state, revision):
    """Create a versioned manifest from a validated, stable repository read."""
    if type(revision) is not int or revision < 0:
        raise RecoveryError("Snapshot revision must be a non-negative integer.")
    encoded = _validated_state(state)
    return {"schema": SNAPSHOT_SCHEMA, "version": SNAPSHOT_VERSION,
            "revision": revision, "state_sha256": hashlib.sha256(encoded).hexdigest(),
            "state": json.loads(encoded)}


def export_snapshot(repository):
    """Read the repository's revision-stable snapshot; never call a mutator."""
    state, revision = repository.snapshot()
    return make_snapshot(state, revision)


def _state_and_digest(value):
    if isinstance(value, dict) and set(value) == set(SECTIONS):
        state = value
        encoded = _validated_state(state)
        return state, hashlib.sha256(encoded).hexdigest()
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise RecoveryError("Expected a workspace state or a supported snapshot manifest.")
    if (value["schema"] != SNAPSHOT_SCHEMA or type(value["version"]) is not int
            or value["version"] != SNAPSHOT_VERSION):
        raise RecoveryError("Snapshot schema or version is unsupported.")
    if type(value["revision"]) is not int or value["revision"] < 0:
        raise RecoveryError("Snapshot revision must be a non-negative integer.")
    digest = value["state_sha256"]
    if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise RecoveryError("Snapshot state digest is malformed.")
    encoded = _validated_state(value["state"])
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise RecoveryError("Snapshot state digest does not match its contents.")
    return value["state"], digest


def validate_snapshot(value):
    """Validate either a manifest or a raw eight-section canonical backup state."""
    state, digest = _state_and_digest(value)
    counts = {section: len(state[section]) for section in SECTIONS}
    return {"valid": True, "state_sha256": digest, "entities": sum(counts.values()),
            "sections": counts}


def compare_snapshots(source, replacement, *, require_revision_match=False):
    """Compare all entity payloads and report META revision differences.

    The report contains counts and digests only, without identifying keys or
    customer payloads. Both inputs must satisfy domain and storage invariants.
    Revisions affect the verdict only when require_revision_match is enabled.
    """
    before, source_digest = _state_and_digest(source)
    after, replacement_digest = _state_and_digest(replacement)
    source_revision = source.get("revision") if set(source) == _MANIFEST_FIELDS else None
    replacement_revision = replacement.get("revision") if set(replacement) == _MANIFEST_FIELDS else None
    revision_matches = (source_revision == replacement_revision
                        if source_revision is not None and replacement_revision is not None else None)
    state_matches = source_digest == replacement_digest
    sections = {}
    totals = {"missing": 0, "extra": 0, "changed": 0}
    for section in SECTIONS:
        original, current = before[section], after[section]
        old_keys, new_keys = set(original), set(current)
        counts = {"source": len(original), "replacement": len(current),
                  "missing": len(old_keys - new_keys), "extra": len(new_keys - old_keys),
                  "changed": sum(canonical_json(original[key]) != canonical_json(current[key])
                                 for key in old_keys & new_keys)}
        sections[section] = counts
        for kind in totals:
            totals[kind] += counts[kind]
    return {"matches": state_matches and (not require_revision_match or revision_matches is True),
            "state_matches": state_matches, "source_revision": source_revision,
            "replacement_revision": replacement_revision, "revision_matches": revision_matches,
            "revision_required": require_revision_match,
            "source_sha256": source_digest, "replacement_sha256": replacement_digest,
            "source_entities": sum(len(before[section]) for section in SECTIONS),
            "replacement_entities": sum(len(after[section]) for section in SECTIONS),
            "differences": totals, "sections": sections}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError("Snapshot JSON contains a duplicate object key.")
        result[key] = value
    return result


def _nonfinite_number(_value):
    raise RecoveryError("JSON numbers must be finite.")


def load_snapshot(source):
    """Bound file reads and reject ambiguous or malformed JSON before use."""
    try:
        with Path(source).open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise RecoveryError("Snapshot file exceeds the configured size limit.")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_nonfinite_number)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        if isinstance(error, RecoveryError):
            raise
        raise RecoveryError("Snapshot file is unreadable or contains invalid JSON.") from error
    validate_snapshot(value)
    return value


def write_snapshot(snapshot, destination):
    """Create a private local artifact exclusively; an existing file is refused."""
    if not isinstance(snapshot, dict) or set(snapshot) != _MANIFEST_FIELDS:
        raise RecoveryError("Only a versioned snapshot manifest can be exported.")
    validate_snapshot(snapshot)
    encoded = canonical_json(snapshot) + b"\n"
    if len(encoded) > MAX_INPUT_BYTES:
        raise RecoveryError("Snapshot file exceeds the configured size limit.")
    target = Path(destination)
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RecoveryError("Snapshot destination already exists; choose a new filename.") from error
    except OSError as error:
        raise RecoveryError("Snapshot destination could not be created.") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        target.unlink(missing_ok=True)
        raise RecoveryError("Snapshot artifact could not be written.") from error
    return target


def _export_arguments(table, region, profile, expected_account):
    if not isinstance(table, str) or re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", table) is None:
        raise RecoveryError("Provide an explicit DynamoDB table name.")
    if not isinstance(region, str) or re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region) is None:
        raise RecoveryError("Provide an explicit AWS region.")
    if (not isinstance(profile, str) or not profile or len(profile) > 256
            or profile != profile.strip() or any(ord(char) < 32 for char in profile)):
        raise RecoveryError("Provide an explicit named AWS profile.")
    if not isinstance(expected_account, str) or re.fullmatch(r"[0-9]{12}", expected_account) is None:
        raise RecoveryError("Provide an explicit expected 12-digit AWS account.")


def _check_table(table, description, region, expected_account):
    if not isinstance(description, dict) or description.get("TableStatus") != "ACTIVE":
        raise RecoveryError("Selected DynamoDB table is not ACTIVE.")
    arn = description.get("TableArn")
    expected_suffix = f":dynamodb:{region}:{expected_account}:table/{table}"
    if (description.get("TableName") != table or not isinstance(arn, str)
            or re.fullmatch(r"arn:[a-z0-9-]+" + re.escape(expected_suffix), arn) is None):
        raise RecoveryError("DynamoDB table identity does not match the explicit selection.")
    keys = description.get("KeySchema")
    if (not isinstance(keys, list) or len(keys) != 2
            or any(not isinstance(key, dict) for key in keys)
            or {(key.get("AttributeName"), key.get("KeyType")) for key in keys}
            != {("PK", "HASH"), ("SK", "RANGE")}):
        raise RecoveryError("DynamoDB table does not have the workspace PK/SK key schema.")
    definitions = description.get("AttributeDefinitions")
    if (not isinstance(definitions, list)
            or any(not isinstance(field, dict) for field in definitions)):
        raise RecoveryError("DynamoDB table key definitions are invalid.")
    attributes = {field.get("AttributeName"): field.get("AttributeType") for field in definitions}
    if len(attributes) != len(definitions) or attributes.get("PK") != "S" or attributes.get("SK") != "S":
        raise RecoveryError("DynamoDB workspace keys must both be strings.")


def export_table_snapshot(*, table, region, profile, expected_account,
                          session_factory=None, repository_factory=None):
    """Explicit AWS read with account verification before any DynamoDB client."""
    _export_arguments(table, region, profile, expected_account)
    options = {"region_name": region}
    try:
        if session_factory is None:
            import boto3
            from botocore.config import Config
            session_factory = boto3.Session
            options["config"] = Config(connect_timeout=5, read_timeout=10,
                                       retries={"mode": "standard", "total_max_attempts": 3})
        session = session_factory(profile_name=profile, region_name=region)
        identity = session.client("sts", **options).get_caller_identity()
        if not isinstance(identity, dict) or identity.get("Account") != expected_account:
            raise RecoveryError("AWS caller account does not match the explicit expected account.")
        client = session.client("dynamodb", **options)
        response = client.describe_table(TableName=table)
        _check_table(table, response.get("Table"), region, expected_account)
        if repository_factory is None:
            from aws.repository import DynamoRepository
            repository_factory = DynamoRepository
        return export_snapshot(repository_factory(table, client=client))
    except RecoveryError:
        raise
    except ImportError as error:
        raise RecoveryError("Install the optional AWS SDK to use the explicit export command.") from error
    except Exception as error:
        raise RecoveryError("AWS snapshot export failed; verify the selected profile and read permissions.") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a local manifest or raw canonical workspace JSON.")
    validate.add_argument("file")
    compare = commands.add_parser("compare", help="Compare local source and replacement snapshots without AWS access.")
    compare.add_argument("source")
    compare.add_argument("replacement")
    compare.add_argument("--require-revision-match", action="store_true",
                         help="Require both manifests to have the same META revision as well as identical state.")
    export = commands.add_parser("export", help="Explicitly read one AWS table into a new private local snapshot.")
    for name in ("table", "region", "profile", "expected-account"):
        export.add_argument("--" + name, required=True)
    export.add_argument("--output", required=True, help="New private JSON file; keep under ignored build/ or data/.")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_snapshot(load_snapshot(args.file))
        elif args.command == "compare":
            result = compare_snapshots(load_snapshot(args.source), load_snapshot(args.replacement),
                                       require_revision_match=args.require_revision_match)
        else:
            # Refuse an existing destination before credentials or AWS access.
            if Path(args.output).exists():
                raise RecoveryError("Snapshot destination already exists; choose a new filename.")
            snapshot = export_table_snapshot(table=args.table, region=args.region, profile=args.profile,
                                             expected_account=args.expected_account)
            write_snapshot(snapshot, args.output)
            result = {"exported": True, **validate_snapshot(snapshot)}
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 1 if args.command == "compare" and not result["matches"] else 0
    except (RecoveryError, OSError) as error:
        message = str(error) if isinstance(error, RecoveryError) else "Local snapshot operation failed."
        print(json.dumps({"code": "recovery_error", "error": message}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
