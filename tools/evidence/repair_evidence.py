"""Record and verify the closed UnitSentinel repair evidence slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, cast

from tools.evidence.generate import (
    ASSETS,
    EVIDENCE,
    PYTHON_DISPLAY,
    EvidenceError,
    _atomic_write,
    _canonical_bytes,
    _check_files,
    _managed_run_directory,
    _read_regular_file,
    _relative,
    _require_recording_environment,
    _run_cli,
)
from tools.evidence.visuals import unit_repair_lineage_svg
from unitsentinel.canonical import canonical_json_bytes
from unitsentinel.graph import GRAPH_SCHEMA, ComputationGraph
from unitsentinel.graph_codec import decode_graph, encode_graph
from unitsentinel.registry import BUILTIN_REGISTRY, REGISTRY_SCHEMA
from unitsentinel.repair import REPAIR_SCHEMA, RepairLimits
from unitsentinel.verification import SolverLimits
from unitsentinel.version import VERSION

REPAIR_CLI_SCHEMA: Final = "unitsentinel.cli.repair/v1"
REPAIR_PROVENANCE_SCHEMA: Final = "unitsentinel.repair-evidence-provenance/v1"
SOURCE_GRAPH_PATH: Final = EVIDENCE / "contracts" / "wheel-anomaly-conflict.json"
CAPTURE_JSON_PATH: Final = EVIDENCE / "captures" / "repair.json"
CAPTURE_TEXT_PATH: Final = EVIDENCE / "captures" / "repair.txt"
PROVENANCE_PATH: Final = EVIDENCE / "repair-provenance.json"
LINEAGE_SVG_PATH: Final = ASSETS / "unit-repair-lineage.svg"
EXPECTED_OUTPUT_PATHS: Final = frozenset(
    {
        CAPTURE_JSON_PATH,
        CAPTURE_TEXT_PATH,
        PROVENANCE_PATH,
        LINEAGE_SVG_PATH,
    }
)

VALUE_ID: Final = "acceleration-si"
CONSTRAINT_ID: Final = f"declaration/{VALUE_ID}/unit"
PREVIOUS_UNIT_ID: Final = "meter-per-second"
REPLACEMENT_UNIT_ID: Final = "meter-per-second-squared"
REPAIR_LIMITS: Final = RepairLimits(
    max_sites=1,
    max_candidates=1,
    max_verifier_calls=3,
    max_work_items=64,
    total_timeout_ms=30_000,
)
REPAIR_ARGUMENTS: Final = (
    "repair",
    _relative(SOURCE_GRAPH_PATH),
    "--max-sites",
    str(REPAIR_LIMITS.max_sites),
    "--max-candidates",
    str(REPAIR_LIMITS.max_candidates),
    "--max-verifier-calls",
    str(REPAIR_LIMITS.max_verifier_calls),
    "--max-work-items",
    str(REPAIR_LIMITS.max_work_items),
    "--total-timeout-ms",
    str(REPAIR_LIMITS.total_timeout_ms),
)
TRANSCRIPT_COMMAND_LINES: Final = (
    f"$ {PYTHON_DISPLAY} -m unitsentinel repair \\",
    f"    {_relative(SOURCE_GRAPH_PATH)} \\",
    f"    --max-sites {REPAIR_LIMITS.max_sites} \\",
    f"    --max-candidates {REPAIR_LIMITS.max_candidates} \\",
    f"    --max-verifier-calls {REPAIR_LIMITS.max_verifier_calls} \\",
    f"    --max-work-items {REPAIR_LIMITS.max_work_items} \\",
    f"    --total-timeout-ms {REPAIR_LIMITS.total_timeout_ms}",
)
EXPECTED_CONTRACT: Final = {
    "dimension": [
        {"base": "length", "exponent": "1"},
        {"base": "time", "exponent": "-2"},
    ],
    "kind": "linear",
    "offset": "0",
    "scale": "1",
    "value_id": VALUE_ID,
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _record(
    value: object,
    *,
    label: str,
    fields: set[str],
) -> dict[str, Any]:
    if type(value) is not dict:
        raise EvidenceError(f"{label} must be an object")
    result = cast(dict[str, Any], value)
    if set(result) != fields:
        raise EvidenceError(f"{label} fields are not closed")
    return result


def _digest_record(envelope: dict[str, Any], *, label: str) -> dict[str, Any]:
    closed = _record(
        envelope,
        label=label,
        fields={"record", "sha256"},
    )
    record_value = closed["record"]
    if type(record_value) is not dict:
        raise EvidenceError(f"{label} record must be an object")
    record = cast(dict[str, Any], record_value)
    digest = closed["sha256"]
    if type(digest) is not str or digest != _sha256(canonical_json_bytes(record)):
        raise EvidenceError(f"{label} digest does not bind its record")
    return record


def _graph_envelope(
    value: object,
    *,
    label: str,
) -> tuple[dict[str, Any], ComputationGraph]:
    envelope = _record(
        value,
        label=label,
        fields={"record", "sha256"},
    )
    graph_record = _record(
        envelope["record"],
        label=f"{label} record",
        fields={"graph_id", "inputs", "nodes", "outputs", "schema", "values"},
    )
    try:
        graph = decode_graph(canonical_json_bytes(graph_record))
    except Exception:
        raise EvidenceError(f"{label} is not a canonical graph") from None
    digest = envelope["sha256"]
    if type(digest) is not str or digest != graph.digest:
        raise EvidenceError(f"{label} digest does not bind its graph")
    return envelope, graph


def _same_graph_except_target_units(
    source: ComputationGraph,
    relaxed: ComputationGraph,
    repaired: ComputationGraph,
) -> bool:
    if (
        source.graph_id != relaxed.graph_id
        or source.graph_id != repaired.graph_id
        or source.inputs != relaxed.inputs
        or source.inputs != repaired.inputs
        or source.nodes != relaxed.nodes
        or source.nodes != repaired.nodes
        or source.outputs != relaxed.outputs
        or source.outputs != repaired.outputs
        or len(source.values) != len(relaxed.values)
        or len(source.values) != len(repaired.values)
    ):
        return False
    found = False
    for source_value, relaxed_value, repaired_value in zip(
        source.values,
        relaxed.values,
        repaired.values,
        strict=True,
    ):
        if (
            source_value.value_id != relaxed_value.value_id
            or source_value.value_id != repaired_value.value_id
            or source_value.dtype is not relaxed_value.dtype
            or source_value.dtype is not repaired_value.dtype
            or source_value.shape != relaxed_value.shape
            or source_value.shape != repaired_value.shape
        ):
            return False
        if source_value.value_id == VALUE_ID:
            found = True
            if (
                source_value.unit_id != PREVIOUS_UNIT_ID
                or relaxed_value.unit_id is not None
                or repaired_value.unit_id != REPLACEMENT_UNIT_ID
            ):
                return False
        elif (
            source_value.unit_id != relaxed_value.unit_id
            or source_value.unit_id != repaired_value.unit_id
        ):
            return False
    return found


def _contract(
    verification: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    contracts = verification.get("contracts")
    if type(contracts) is not list:
        raise EvidenceError(f"{label} contracts are malformed")
    matches = [
        item
        for item in contracts
        if type(item) is dict and item.get("value_id") == VALUE_ID
    ]
    if matches != [EXPECTED_CONTRACT]:
        raise EvidenceError(f"{label} target contract is not exact")
    return cast(dict[str, Any], matches[0])


def _validate_record(
    value: object,
    *,
    source_graph: ComputationGraph,
) -> dict[str, Any]:
    record = _record(
        value,
        label="repair CLI record",
        fields={
            "application",
            "exit_code",
            "graph",
            "proposal",
            "registry",
            "report",
            "schema",
            "source_verification",
            "tool",
        },
    )
    if (
        record["schema"] != REPAIR_CLI_SCHEMA
        or record["application"] != "not-performed"
        or record["exit_code"] != 0
    ):
        raise EvidenceError("repair CLI outcome is not a non-applied proposal")

    tool = _record(
        record["tool"],
        label="repair CLI tool",
        fields={"name", "version"},
    )
    if tool != {"name": "unitsentinel", "version": VERSION}:
        raise EvidenceError("repair CLI tool identity is stale")

    graph = _record(
        record["graph"],
        label="repair CLI graph",
        fields={"graph_id", "schema", "sha256"},
    )
    if graph != {
        "graph_id": source_graph.graph_id,
        "schema": GRAPH_SCHEMA,
        "sha256": source_graph.digest,
    }:
        raise EvidenceError("repair CLI source graph binding is stale")

    registry = _record(
        record["registry"],
        label="repair CLI registry",
        fields={"schema", "sha256", "version"},
    )
    if registry != {
        "schema": REGISTRY_SCHEMA,
        "sha256": BUILTIN_REGISTRY.digest,
        "version": BUILTIN_REGISTRY.version,
    }:
        raise EvidenceError("repair CLI registry binding is stale")

    source_envelope = _record(
        record["source_verification"],
        label="source verification",
        fields={"record", "sha256"},
    )
    source_verification = _digest_record(
        source_envelope,
        label="source verification",
    )
    if (
        source_verification.get("status") != "conflict"
        or source_verification.get("core_minimal") is not True
        or source_verification.get("graph_digest") != source_graph.digest
        or source_verification.get("registry_digest") != BUILTIN_REGISTRY.digest
    ):
        raise EvidenceError("repair source verification is not the expected conflict")
    conflict_core = source_verification.get("conflict_core")
    if type(conflict_core) is not list or not any(
        type(witness) is dict
        and witness.get("constraint_id") == CONSTRAINT_ID
        and witness.get("source") == "declaration"
        and witness.get("source_id") == VALUE_ID
        and witness.get("rule") == "unit-annotation"
        for witness in conflict_core
    ):
        raise EvidenceError("repair source conflict does not contain the target site")

    report_envelope = _record(
        record["report"],
        label="repair report",
        fields={"record", "sha256"},
    )
    report = _digest_record(report_envelope, label="repair report")
    expected_report_fields = {
        "candidate",
        "candidates_considered",
        "reason",
        "registry_digest",
        "repair_limits",
        "schema",
        "sites_considered",
        "solver_limits",
        "source_graph_digest",
        "source_verification_digest",
        "status",
        "verification_calls",
        "work_items",
    }
    if set(report) != expected_report_fields:
        raise EvidenceError("repair report fields are not closed")
    if (
        report["schema"] != REPAIR_SCHEMA
        or report["status"] != "proposed"
        or report["reason"] is not None
        or report["source_graph_digest"] != source_graph.digest
        or report["source_verification_digest"] != source_envelope["sha256"]
        or report["registry_digest"] != BUILTIN_REGISTRY.digest
        or report["repair_limits"] != REPAIR_LIMITS.canonical_record()
        or report["solver_limits"] != SolverLimits().canonical_record()
        or report["sites_considered"] != 1
        or report["candidates_considered"] != 1
        or report["verification_calls"] != 3
        or type(report["work_items"]) is not int
        or not 1 <= report["work_items"] <= REPAIR_LIMITS.max_work_items
    ):
        raise EvidenceError("repair report does not match the pinned search")

    candidate = _record(
        report["candidate"],
        label="repair candidate",
        fields={
            "constraint_id",
            "previous_unit_id",
            "relaxed_graph_digest",
            "relaxed_verification_digest",
            "repaired_graph_digest",
            "repaired_verification_digest",
            "replacement_unit_id",
            "value_id",
        },
    )
    if (
        candidate["constraint_id"] != CONSTRAINT_ID
        or candidate["value_id"] != VALUE_ID
        or candidate["previous_unit_id"] != PREVIOUS_UNIT_ID
        or candidate["replacement_unit_id"] != REPLACEMENT_UNIT_ID
    ):
        raise EvidenceError("repair candidate changes the wrong annotation")

    proposal = _record(
        record["proposal"],
        label="repair proposal",
        fields={
            "candidate_sha256",
            "relaxed_graph",
            "relaxed_verification",
            "repaired_graph",
            "repaired_verification",
        },
    )
    candidate_digest = _sha256(canonical_json_bytes(candidate))
    if proposal["candidate_sha256"] != candidate_digest:
        raise EvidenceError("repair candidate digest is stale")

    relaxed_envelope, relaxed_graph = _graph_envelope(
        proposal["relaxed_graph"],
        label="relaxed graph",
    )
    repaired_envelope, repaired_graph = _graph_envelope(
        proposal["repaired_graph"],
        label="repaired graph",
    )
    if not _same_graph_except_target_units(
        source_graph,
        relaxed_graph,
        repaired_graph,
    ):
        raise EvidenceError("repair graph lineage changes more than one annotation")

    relaxed_verification_envelope = _record(
        proposal["relaxed_verification"],
        label="relaxed verification",
        fields={"record", "sha256"},
    )
    repaired_verification_envelope = _record(
        proposal["repaired_verification"],
        label="repaired verification",
        fields={"record", "sha256"},
    )
    relaxed_verification = _digest_record(
        relaxed_verification_envelope,
        label="relaxed verification",
    )
    repaired_verification = _digest_record(
        repaired_verification_envelope,
        label="repaired verification",
    )
    for label, verification, graph_envelope in (
        ("relaxed", relaxed_verification, relaxed_envelope),
        ("repaired", repaired_verification, repaired_envelope),
    ):
        if (
            verification.get("status") != "verified"
            or verification.get("graph_digest") != graph_envelope["sha256"]
            or verification.get("registry_digest") != BUILTIN_REGISTRY.digest
        ):
            raise EvidenceError(f"{label} verification is not graph-bound")
        _contract(verification, label=label)

    digest_bindings = {
        "relaxed_graph_digest": relaxed_envelope["sha256"],
        "relaxed_verification_digest": relaxed_verification_envelope["sha256"],
        "repaired_graph_digest": repaired_envelope["sha256"],
        "repaired_verification_digest": repaired_verification_envelope["sha256"],
    }
    if any(candidate[key] != value for key, value in digest_bindings.items()):
        raise EvidenceError("repair candidate lineage digests are stale")
    return record


def _provenance(record: dict[str, Any]) -> dict[str, object]:
    proposal = cast(dict[str, Any], record["proposal"])
    report_envelope = cast(dict[str, Any], record["report"])
    report = cast(dict[str, Any], report_envelope["record"])
    candidate = cast(dict[str, Any], report["candidate"])
    source_verification = cast(dict[str, Any], record["source_verification"])
    relaxed_graph = cast(dict[str, Any], proposal["relaxed_graph"])
    repaired_graph = cast(dict[str, Any], proposal["repaired_graph"])
    relaxed_verification = cast(dict[str, Any], proposal["relaxed_verification"])
    repaired_verification = cast(dict[str, Any], proposal["repaired_verification"])
    return {
        "application": record["application"],
        "candidate": {
            "constraint_id": candidate["constraint_id"],
            "previous_unit_id": candidate["previous_unit_id"],
            "replacement_unit_id": candidate["replacement_unit_id"],
            "sha256": proposal["candidate_sha256"],
            "value_id": candidate["value_id"],
        },
        "capture": {
            "path": _relative(CAPTURE_JSON_PATH),
            "record_sha256": _sha256(canonical_json_bytes(record)),
            "schema": record["schema"],
        },
        "graphs": {
            "relaxed": {
                "graph_id": relaxed_graph["record"]["graph_id"],
                "sha256": relaxed_graph["sha256"],
            },
            "repaired": {
                "graph_id": repaired_graph["record"]["graph_id"],
                "sha256": repaired_graph["sha256"],
            },
            "source": {
                "graph_id": record["graph"]["graph_id"],
                "sha256": record["graph"]["sha256"],
            },
        },
        "registry": record["registry"],
        "report": {
            "sha256": report_envelope["sha256"],
            "status": report["status"],
        },
        "schema": REPAIR_PROVENANCE_SCHEMA,
        "search": {
            "candidates_considered": report["candidates_considered"],
            "limits": report["repair_limits"],
            "sites_considered": report["sites_considered"],
            "verification_calls": report["verification_calls"],
            "work_items": report["work_items"],
        },
        "verifications": {
            "relaxed": {
                "sha256": relaxed_verification["sha256"],
                "status": relaxed_verification["record"]["status"],
            },
            "repaired": {
                "sha256": repaired_verification["sha256"],
                "status": repaired_verification["record"]["status"],
            },
            "source": {
                "core_minimal": source_verification["record"]["core_minimal"],
                "sha256": source_verification["sha256"],
                "status": source_verification["record"]["status"],
            },
        },
    }


def _transcript(output: bytes) -> bytes:
    if not output.endswith(b"\n"):
        raise EvidenceError("repair CLI capture lacks its transport newline")
    return (
        "\n".join(TRANSCRIPT_COMMAND_LINES).encode("utf-8")
        + b"\n"
        + output
        + b"[exit 0]\n"
    )


def _build_evidence() -> dict[Path, bytes]:
    source_before = _read_regular_file(
        SOURCE_GRAPH_PATH,
        purpose="repair evidence source graph",
    )
    try:
        source_graph = decode_graph(source_before)
    except Exception:
        raise EvidenceError("repair evidence source graph is malformed") from None
    if encode_graph(source_graph) != source_before:
        raise EvidenceError("repair evidence source graph is not canonical")

    output = _run_cli(REPAIR_ARGUMENTS, expected_exit=0)
    source_after = _read_regular_file(
        SOURCE_GRAPH_PATH,
        purpose="repair evidence source graph",
    )
    if source_after != source_before:
        raise EvidenceError("repair CLI changed its source graph")
    try:
        decoded = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EvidenceError("repair CLI did not emit canonical JSON") from None
    if output != _canonical_bytes(decoded):
        raise EvidenceError("repair CLI output is not canonical")
    record = _validate_record(decoded, source_graph=source_graph)
    provenance = _provenance(record)
    proposal = cast(dict[str, Any], record["proposal"])
    report_envelope = cast(dict[str, Any], record["report"])
    report = cast(dict[str, Any], report_envelope["record"])
    candidate = cast(dict[str, Any], report["candidate"])
    source_verification = cast(dict[str, Any], record["source_verification"])
    relaxed_graph = cast(dict[str, Any], proposal["relaxed_graph"])
    repaired_graph = cast(dict[str, Any], proposal["repaired_graph"])
    relaxed_verification = cast(dict[str, Any], proposal["relaxed_verification"])
    repaired_verification = cast(dict[str, Any], proposal["repaired_verification"])

    files = {
        CAPTURE_JSON_PATH: output,
        CAPTURE_TEXT_PATH: _transcript(output),
        PROVENANCE_PATH: _canonical_bytes(provenance),
        LINEAGE_SVG_PATH: unit_repair_lineage_svg(
            candidate_digest=cast(str, proposal["candidate_sha256"]),
            candidates_considered=cast(int, report["candidates_considered"]),
            constraint_id=cast(str, candidate["constraint_id"]),
            max_work_items=REPAIR_LIMITS.max_work_items,
            previous_unit_id=cast(str, candidate["previous_unit_id"]),
            registry_digest=BUILTIN_REGISTRY.digest,
            relaxed_graph_digest=cast(str, relaxed_graph["sha256"]),
            relaxed_verification_digest=cast(
                str,
                relaxed_verification["sha256"],
            ),
            repaired_graph_digest=cast(str, repaired_graph["sha256"]),
            repaired_verification_digest=cast(
                str,
                repaired_verification["sha256"],
            ),
            replacement_unit_id=cast(str, candidate["replacement_unit_id"]),
            report_digest=cast(str, report_envelope["sha256"]),
            sites_considered=cast(int, report["sites_considered"]),
            source_graph_digest=source_graph.digest,
            source_verification_digest=cast(str, source_verification["sha256"]),
            value_id=cast(str, candidate["value_id"]),
            verification_calls=cast(int, report["verification_calls"]),
            work_items=cast(int, report["work_items"]),
        ).encode("utf-8"),
    }
    if set(files) != EXPECTED_OUTPUT_PATHS:
        raise EvidenceError("repair evidence output allowlist is violated")
    return files


def _write_files(files: dict[Path, bytes]) -> None:
    if set(files) != EXPECTED_OUTPUT_PATHS:
        raise EvidenceError("repair evidence output allowlist is violated")
    for path in sorted(files):
        _atomic_write(path, files[path])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record or verify the closed UnitSentinel repair evidence slice.",
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--record",
        action="store_true",
        help="refresh only the fixed repair capture, provenance, and SVG",
    )
    modes.add_argument(
        "--check",
        action="store_true",
        help="re-execute the fixed repair CLI and compare exact committed bytes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.record:
            _require_recording_environment()
        with _managed_run_directory():
            files = _build_evidence()
        if arguments.check:
            _check_files(files)
        else:
            _write_files(files)
    except EvidenceError as error:
        sys.stderr.write(f"repair-evidence: error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
