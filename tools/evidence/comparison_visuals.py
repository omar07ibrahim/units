"""Build source-derived visuals from committed comparison evidence.

The generator reads a closed set of canonical plans, graphs, CLI captures,
strict comparison-result claims, provenance, and exact file-size records.  It
does not execute the comparison again and does not introduce benchmark data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from tools.evidence import comparison_evidence as comparison_records
from tools.evidence.generate import (
    ASSETS,
    EVIDENCE,
    EvidenceError,
    _atomic_write,
    _canonical_bytes,
    _check_files,
    _read_regular_file,
    _relative,
    _require_recording_environment,
    _transcript,
)
from tools.evidence.visuals import (
    AMBER,
    GREEN,
    RED,
    comparison_artifact_sizes_svg,
    comparison_lineage_drift_svg,
    comparison_terminal_svg,
    comparison_workflow_svg,
)
from unitsentinel.canonical import canonical_json_bytes
from unitsentinel.comparison import (
    AUTHENTICATION_NOT_PROVIDED,
    COMPARISON_SCOPE_UNDER_PLAN,
    ComparisonResult,
    ComparisonStatus,
)
from unitsentinel.comparison_codec import ComparisonDecodeError, decode_comparison_plan
from unitsentinel.comparison_result_codec import (
    ComparisonResultDecodeError,
    decode_comparison_result,
)
from unitsentinel.graph import ComputationGraph
from unitsentinel.graph_codec import GraphDecodeError, decode_graph
from unitsentinel.registry import BUILTIN_REGISTRY, REGISTRY_SCHEMA
from unitsentinel.version import VERSION

PROVENANCE_SCHEMA: Final = "unitsentinel.comparison-evidence-provenance/v1"
ARTIFACTS_SCHEMA: Final = "unitsentinel.comparison-artifacts/v1"
FRAME_SCHEMA: Final = "unitsentinel.comparison-demo-frames/v1"
MEASUREMENT_SCOPE: Final = (
    "exact committed artifact byte lengths; no latency or performance claim"
)
CASE_NAMES: Final = ("compatible", "drift", "indeterminate")
EXPECTED_LINE_COUNTS: Final = {
    "compatible": 39,
    "drift": 39,
    "indeterminate": 36,
}
EXPECTED_EXIT_CODES: Final = {
    "compatible": 0,
    "drift": 5,
    "indeterminate": 3,
}
EXPECTED_STATUSES: Final = {
    "compatible": ComparisonStatus.COMPATIBLE,
    "drift": ComparisonStatus.DRIFT,
    "indeterminate": ComparisonStatus.INDETERMINATE,
}
TERMINAL_ACCENTS: Final = {
    "compatible": GREEN,
    "drift": RED,
    "indeterminate": AMBER,
}

PROVENANCE_PATH: Final = EVIDENCE / "comparison-provenance.json"
ARTIFACTS_PATH: Final = EVIDENCE / "data" / "comparison-artifacts.json"
TRANSCRIPT_PATHS: Final = {
    name: EVIDENCE / "captures" / f"compare-{name}.txt" for name in CASE_NAMES
}
JSON_CAPTURE_PATHS: Final = {
    name: EVIDENCE / "captures" / f"compare-{name}.json" for name in CASE_NAMES
}
CLAIM_PATHS: Final = {
    name: EVIDENCE / "claims" / f"ratio-{name}.result.json" for name in CASE_NAMES
}
PLAN_PATHS: Final = {
    name: EVIDENCE / "plans" / f"ratio-{name}.plan.json" for name in CASE_NAMES
}
TRAINING_GRAPH_PATH: Final = EVIDENCE / "contracts" / "ratio-training.json"
SERVING_GRAPH_PATHS: Final = {
    "compatible": EVIDENCE / "contracts" / "ratio-serving-renamed.json",
    "drift": EVIDENCE / "contracts" / "ratio-serving-reversed.json",
    "indeterminate": (EVIDENCE / "contracts" / "ratio-serving-underconstrained.json"),
}

WORKFLOW_SVG_PATH: Final = ASSETS / "comparison-workflow.svg"
LINEAGE_SVG_PATH: Final = ASSETS / "comparison-lineage-drift.svg"
SIZES_SVG_PATH: Final = ASSETS / "comparison-artifact-sizes.svg"
TERMINAL_SVG_PATHS: Final = {
    name: ASSETS / f"compare-{name}-terminal.svg" for name in CASE_NAMES
}
FRAME_DIRECTORY: Final = EVIDENCE / "comparison-demo"
FRAME_SVG_PATHS: Final = {
    name: FRAME_DIRECTORY / f"frame-{name}.svg" for name in CASE_NAMES
}
FRAME_MANIFEST_PATH: Final = FRAME_DIRECTORY / "frames.json"
EXPECTED_OUTPUT_PATHS: Final = frozenset(
    {
        WORKFLOW_SVG_PATH,
        LINEAGE_SVG_PATH,
        SIZES_SVG_PATH,
        *TERMINAL_SVG_PATHS.values(),
        *FRAME_SVG_PATHS.values(),
        FRAME_MANIFEST_PATH,
    }
)

PROVENANCE_CASE_FIELDS: Final = {
    "captures",
    "claim",
    "exit_code",
    "graphs",
    "mismatches",
    "name",
    "normalization_lineage",
    "output_normalization",
    "plan",
    "reason",
    "status",
    "verification",
}
ARTIFACT_ROW_FIELDS: Final = {
    "capture_json_bytes",
    "capture_text_bytes",
    "claim_bytes",
    "claim_sha256",
    "exit_code",
    "graph_bytes",
    "name",
    "plan_bytes",
    "plan_sha256",
    "status",
}


@dataclass(frozen=True, slots=True)
class VisualCase:
    """One strictly decoded, cross-bound comparison evidence case."""

    name: str
    transcript: str
    transcript_digest: str
    result: ComparisonResult
    provenance: dict[str, Any]
    sizes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VisualSources:
    """The three closed comparison cases and their shared provenance."""

    cases: tuple[VisualCase, ...]
    measurement_scope: str
    registry_digest: str


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
    record = cast(dict[str, Any], value)
    if set(record) != fields:
        raise EvidenceError(f"{label} fields are not closed")
    return record


def _list(value: object, *, label: str) -> list[Any]:
    if type(value) is not list:
        raise EvidenceError(f"{label} must be an array")
    return value


def _canonical_document(path: Path, *, label: str) -> dict[str, Any]:
    payload = _read_regular_file(path, purpose=label)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EvidenceError(f"{label} is not canonical JSON") from None
    if payload != canonical_json_bytes(value) + b"\n" or type(value) is not dict:
        raise EvidenceError(f"{label} is not canonical JSON")
    return cast(dict[str, Any], value)


def _require_string(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise EvidenceError(f"{label} must be a string")
    return value


def _require_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise EvidenceError(f"{label} must be a non-negative integer")
    return value


def _require_digest(value: object, *, label: str) -> str:
    digest = _require_string(value, label=label)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise EvidenceError(f"{label} must be a lowercase SHA-256")
    return digest


def _nested_digest(
    case: dict[str, Any],
    *,
    group: str,
    side: str,
) -> str | None:
    envelope = _record(
        case[group],
        label=f"{group} provenance",
        fields={"serving_sha256", "training_sha256"},
    )
    value = envelope[f"{side}_sha256"]
    if value is None:
        return None
    return _require_digest(value, label=f"{group} {side} digest")


def _fixed_path_record(
    value: object,
    *,
    label: str,
    expected_path: Path,
    fields: set[str],
) -> dict[str, Any]:
    record = _record(value, label=label, fields=fields)
    if record["path"] != _relative(expected_path):
        raise EvidenceError(f"{label} path is outside the closed source set")
    return record


def _validate_capture(
    *,
    name: str,
    result: ComparisonResult,
    case: comparison_records.EvidenceCase,
    training: ComputationGraph,
    claim_payload: bytes,
) -> bytes:
    path = JSON_CAPTURE_PATHS[name]
    payload = _read_regular_file(path, purpose=f"{name} JSON CLI capture")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EvidenceError(f"{name} JSON CLI capture is not canonical") from None
    if payload != canonical_json_bytes(value) + b"\n":
        raise EvidenceError(f"{name} JSON CLI capture is not canonical")
    _, decoded = comparison_records._validate_capture(
        value,
        case=case,
        training=training,
        claim_payload=claim_payload,
    )
    if decoded != result:
        raise EvidenceError(f"{name} JSON CLI capture is not bound to its claim")
    return payload


def _validate_transcript(
    *,
    name: str,
    payload: bytes,
    provenance: dict[str, Any],
    expected: bytes,
) -> str:
    if payload != expected:
        raise EvidenceError(f"{name} text transcript is not the exact bound report")
    try:
        transcript = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise EvidenceError(f"{name} text transcript is not UTF-8") from None
    lines = transcript.rstrip("\n").splitlines()
    if (
        not transcript.endswith("\n")
        or len(lines) != EXPECTED_LINE_COUNTS[name]
        or any(not line.isprintable() for line in lines)
    ):
        raise EvidenceError(f"{name} text transcript line set is not exact")
    captures = _record(
        provenance["captures"],
        label=f"{name} capture provenance",
        fields={"json", "text"},
    )
    text_record = _fixed_path_record(
        captures["text"],
        label=f"{name} text capture provenance",
        expected_path=TRANSCRIPT_PATHS[name],
        fields={"path", "sha256"},
    )
    if _require_digest(
        text_record["sha256"],
        label=f"{name} text capture digest",
    ) != _sha256(payload):
        raise EvidenceError(f"{name} text transcript digest is stale")
    return transcript


def _validate_result_semantics(
    *,
    name: str,
    result: ComparisonResult,
    provenance: dict[str, Any],
) -> None:
    expected_status = EXPECTED_STATUSES[name]
    if (
        result.status is not expected_status
        or provenance["status"] != expected_status.value
        or provenance["exit_code"] != EXPECTED_EXIT_CODES[name]
    ):
        raise EvidenceError(f"{name} comparison status is stale")
    reason = None if result.reason is None else result.reason.value
    if provenance["reason"] != reason:
        raise EvidenceError(f"{name} comparison reason is stale")

    claim = _fixed_path_record(
        provenance["claim"],
        label=f"{name} claim provenance",
        expected_path=CLAIM_PATHS[name],
        fields={"authentication", "path", "scope", "sha256"},
    )
    if (
        _require_digest(claim["sha256"], label=f"{name} claim digest") != result.digest
        or claim["authentication"] != result.authentication
        or claim["scope"] != result.scope
    ):
        raise EvidenceError(f"{name} result trust binding is stale")

    graphs = _record(
        provenance["graphs"],
        label=f"{name} graph provenance",
        fields={"serving", "training"},
    )
    training_graph = _fixed_path_record(
        graphs["training"],
        label=f"{name} training graph provenance",
        expected_path=TRAINING_GRAPH_PATH,
        fields={"graph_id", "path", "sha256"},
    )
    serving_graph = _fixed_path_record(
        graphs["serving"],
        label=f"{name} serving graph provenance",
        expected_path=SERVING_GRAPH_PATHS[name],
        fields={"graph_id", "path", "sha256"},
    )
    if (
        _require_digest(
            training_graph["sha256"],
            label=f"{name} training graph digest",
        )
        != result.training_graph_digest
        or _require_digest(
            serving_graph["sha256"],
            label=f"{name} serving graph digest",
        )
        != result.serving_graph_digest
    ):
        raise EvidenceError(f"{name} graph result binding is stale")

    plan = _fixed_path_record(
        provenance["plan"],
        label=f"{name} plan provenance",
        expected_path=PLAN_PATHS[name],
        fields={"authentication", "path", "sha256"},
    )
    if (
        _require_digest(plan["sha256"], label=f"{name} plan digest")
        != result.plan_digest
    ):
        raise EvidenceError(f"{name} plan result binding is stale")

    verification = _record(
        provenance["verification"],
        label=f"{name} verification provenance",
        fields={"serving", "training"},
    )
    for side, decoded in (
        ("training", result.training_result),
        ("serving", result.serving_result),
    ):
        envelope = _record(
            verification[side],
            label=f"{name} {side} verification provenance",
            fields={"sha256", "status"},
        )
        if (
            decoded is None
            or envelope["status"] != decoded.status.value
            or _require_digest(
                envelope["sha256"],
                label=f"{name} {side} verification digest",
            )
            != decoded.digest
        ):
            raise EvidenceError(f"{name} {side} verification binding is stale")

    for side, lineage in (
        ("training", result.training_lineage),
        ("serving", result.serving_lineage),
    ):
        recorded = _nested_digest(
            provenance,
            group="normalization_lineage",
            side=side,
        )
        lineage_digest = None if lineage is None else lineage.digest
        if recorded != lineage_digest:
            raise EvidenceError(f"{name} {side} lineage binding is stale")

    output = next(
        (
            comparison.normalization
            for comparison in result.comparisons
            if comparison.contract_id == "output-00"
        ),
        None,
    )
    expected_output = (
        (None, None)
        if output is None
        else (output.training_digest, output.serving_digest)
    )
    recorded_output = (
        _nested_digest(provenance, group="output_normalization", side="training"),
        _nested_digest(provenance, group="output_normalization", side="serving"),
    )
    if recorded_output != expected_output:
        raise EvidenceError(f"{name} output normalization binding is stale")

    mismatches = tuple(
        mismatch.value
        for comparison in result.comparisons
        for mismatch in comparison.mismatches
    )
    recorded_mismatches = _list(
        provenance["mismatches"],
        label=f"{name} mismatches",
    )
    if recorded_mismatches != list(mismatches):
        raise EvidenceError(f"{name} mismatch provenance is stale")


def _validate_raw_inputs(
    *,
    name: str,
    result: ComparisonResult,
    sizes: dict[str, Any],
    expected_case: comparison_records.EvidenceCase,
    expected_training: ComputationGraph,
) -> bytes:
    claim_payload = _read_regular_file(
        CLAIM_PATHS[name],
        purpose=f"{name} strict result claim",
    )
    if (
        len(claim_payload)
        != _require_int(sizes["claim_bytes"], label=f"{name} claim bytes")
        or _require_digest(
            sizes["claim_sha256"],
            label=f"{name} size-record claim digest",
        )
        != _sha256(claim_payload)
        or result.digest != _sha256(claim_payload)
    ):
        raise EvidenceError(f"{name} claim size record is stale")

    plan_payload = _read_regular_file(
        PLAN_PATHS[name],
        purpose=f"{name} comparison plan",
    )
    training_payload = _read_regular_file(
        TRAINING_GRAPH_PATH,
        purpose=f"{name} training graph",
    )
    serving_payload = _read_regular_file(
        SERVING_GRAPH_PATHS[name],
        purpose=f"{name} serving graph",
    )
    try:
        plan = decode_comparison_plan(plan_payload)
        training = decode_graph(training_payload)
        serving = decode_graph(serving_payload)
    except (ComparisonDecodeError, GraphDecodeError):
        raise EvidenceError(f"{name} raw comparison inputs are not strict") from None
    graph_bytes = _record(
        sizes["graph_bytes"],
        label=f"{name} graph byte sizes",
        fields={"serving", "training"},
    )
    if (
        len(plan_payload)
        != _require_int(sizes["plan_bytes"], label=f"{name} plan bytes")
        or plan.digest != result.plan_digest
        or _require_digest(
            sizes["plan_sha256"],
            label=f"{name} size-record plan digest",
        )
        != plan.digest
        or len(training_payload)
        != _require_int(
            graph_bytes["training"],
            label=f"{name} training graph bytes",
        )
        or len(serving_payload)
        != _require_int(
            graph_bytes["serving"],
            label=f"{name} serving graph bytes",
        )
        or training.digest != result.training_graph_digest
        or serving.digest != result.serving_graph_digest
        or plan.training_graph_digest != training.digest
        or plan.serving_graph_digest != serving.digest
        or plan != expected_case.plan
        or training != expected_training
        or serving != expected_case.serving_graph
        or plan.registry_digest != BUILTIN_REGISTRY.digest
        or result.registry_digest != BUILTIN_REGISTRY.digest
        or result.limits != comparison_records.COMPARISON_LIMITS
        or result.comparison_id != expected_case.plan.comparison_id
    ):
        raise EvidenceError(f"{name} raw input size or digest binding is stale")

    json_capture = _validate_capture(
        name=name,
        result=result,
        case=expected_case,
        training=training,
        claim_payload=claim_payload,
    )
    if len(json_capture) != _require_int(
        sizes["capture_json_bytes"],
        label=f"{name} JSON capture bytes",
    ):
        raise EvidenceError(f"{name} JSON capture size is stale")
    return claim_payload


def _load_sources() -> VisualSources:
    expected_training, expected_cases = comparison_records._models()
    if tuple(case.name for case in expected_cases) != CASE_NAMES:
        raise EvidenceError("comparison model case ordering is stale")
    expected_by_name = {case.name: case for case in expected_cases}

    provenance = _record(
        _canonical_document(
            PROVENANCE_PATH,
            label="comparison provenance",
        ),
        label="comparison provenance",
        fields={"cases", "limits", "registry", "schema", "tool", "trust"},
    )
    artifacts = _record(
        _canonical_document(
            ARTIFACTS_PATH,
            label="comparison artifact sizes",
        ),
        label="comparison artifact sizes",
        fields={"artifacts", "measurement_scope", "schema"},
    )
    if provenance["schema"] != PROVENANCE_SCHEMA:
        raise EvidenceError("comparison provenance schema is stale")
    if (
        artifacts["schema"] != ARTIFACTS_SCHEMA
        or artifacts["measurement_scope"] != MEASUREMENT_SCOPE
    ):
        raise EvidenceError("comparison artifact measurement scope is stale")

    provenance_cases = _list(provenance["cases"], label="comparison cases")
    artifact_rows = _list(artifacts["artifacts"], label="comparison artifact rows")
    if len(provenance_cases) != 3 or len(artifact_rows) != 3:
        raise EvidenceError("comparison source case count is not closed")

    registry = _record(
        provenance["registry"],
        label="comparison registry provenance",
        fields={"schema", "sha256", "version"},
    )
    expected_registry = {
        "schema": REGISTRY_SCHEMA,
        "sha256": BUILTIN_REGISTRY.digest,
        "version": BUILTIN_REGISTRY.version,
    }
    expected_trust = {
        "claim_authentication": AUTHENTICATION_NOT_PROVIDED,
        "comparison_scope": COMPARISON_SCOPE_UNDER_PLAN,
        "plan_authentication": AUTHENTICATION_NOT_PROVIDED,
    }
    if (
        registry != expected_registry
        or provenance["limits"]
        != comparison_records.COMPARISON_LIMITS.canonical_record()
        or provenance["tool"] != {"name": "unitsentinel", "version": VERSION}
        or provenance["trust"] != expected_trust
    ):
        raise EvidenceError("comparison provenance trust bindings are stale")
    registry_digest = _require_digest(
        registry["sha256"],
        label="comparison registry digest",
    )

    cases: list[VisualCase] = []
    for index, name in enumerate(CASE_NAMES):
        expected_case = expected_by_name[name]
        case = _record(
            provenance_cases[index],
            label=f"{name} provenance",
            fields=PROVENANCE_CASE_FIELDS,
        )
        sizes = _record(
            artifact_rows[index],
            label=f"{name} artifact sizes",
            fields=ARTIFACT_ROW_FIELDS,
        )
        if (
            case["name"] != name
            or sizes["name"] != name
            or sizes["status"] != EXPECTED_STATUSES[name].value
            or sizes["exit_code"] != EXPECTED_EXIT_CODES[name]
        ):
            raise EvidenceError(f"{name} comparison source ordering is stale")

        claim_payload = _read_regular_file(
            CLAIM_PATHS[name],
            purpose=f"{name} strict result claim",
        )
        try:
            result = decode_comparison_result(claim_payload)
        except ComparisonResultDecodeError:
            raise EvidenceError(
                f"{name} result claim is not strictly decodable"
            ) from None
        _validate_result_semantics(
            name=name,
            result=result,
            provenance=case,
        )
        claim_payload = _validate_raw_inputs(
            name=name,
            result=result,
            sizes=sizes,
            expected_case=expected_case,
            expected_training=expected_training,
        )

        transcript_payload = _read_regular_file(
            TRANSCRIPT_PATHS[name],
            purpose=f"{name} text CLI capture",
        )
        transcript = _validate_transcript(
            name=name,
            payload=transcript_payload,
            provenance=case,
            expected=_transcript(
                command_lines=comparison_records._command_lines(expected_case),
                output=comparison_records._expected_text_output(
                    case=expected_case,
                    training=expected_training,
                    result=result,
                ),
                exit_code=expected_case.exit_code,
            ),
        )
        if len(transcript_payload) != _require_int(
            sizes["capture_text_bytes"],
            label=f"{name} text capture bytes",
        ):
            raise EvidenceError(f"{name} text capture size is stale")

        captures = _record(
            case["captures"],
            label=f"{name} captures",
            fields={"json", "text"},
        )
        json_provenance = _fixed_path_record(
            captures["json"],
            label=f"{name} JSON capture provenance",
            expected_path=JSON_CAPTURE_PATHS[name],
            fields={"path", "sha256"},
        )
        json_payload = _read_regular_file(
            JSON_CAPTURE_PATHS[name],
            purpose=f"{name} JSON CLI capture",
        )
        if _require_digest(
            json_provenance["sha256"],
            label=f"{name} JSON capture digest",
        ) != _sha256(json_payload):
            raise EvidenceError(f"{name} JSON capture digest is stale")

        cases.append(
            VisualCase(
                name=name,
                transcript=transcript,
                transcript_digest=_sha256(transcript_payload),
                result=result,
                provenance=case,
                sizes=sizes,
            )
        )

    return VisualSources(
        cases=tuple(cases),
        measurement_scope=MEASUREMENT_SCOPE,
        registry_digest=registry_digest,
    )


def _lineage_values(case: VisualCase) -> dict[str, str]:
    training = case.result.training_lineage
    serving = case.result.serving_lineage
    if training is None or serving is None:
        raise EvidenceError(f"{case.name} result has no normalization lineage")
    output = next(
        (
            comparison.normalization
            for comparison in case.result.comparisons
            if comparison.contract_id == "output-00"
        ),
        None,
    )
    if output is None:
        raise EvidenceError(f"{case.name} result has no output normalization")
    return {
        "content_serving": serving.digest,
        "content_training": training.digest,
        "output_serving": output.serving_digest,
        "output_training": output.training_digest,
        "semantic_serving": serving.semantic_digest,
        "semantic_training": training.semantic_digest,
    }


def _size_row(case: VisualCase) -> dict[str, int | str]:
    graph_bytes = _record(
        case.sizes["graph_bytes"],
        label=f"{case.name} graph byte sizes",
        fields={"serving", "training"},
    )
    return {
        "capture_json_bytes": _require_int(
            case.sizes["capture_json_bytes"],
            label=f"{case.name} JSON capture bytes",
        ),
        "capture_text_bytes": _require_int(
            case.sizes["capture_text_bytes"],
            label=f"{case.name} text capture bytes",
        ),
        "claim_bytes": _require_int(
            case.sizes["claim_bytes"],
            label=f"{case.name} claim bytes",
        ),
        "exit_code": _require_int(
            case.sizes["exit_code"],
            label=f"{case.name} exit code",
        ),
        "name": case.name,
        "plan_bytes": _require_int(
            case.sizes["plan_bytes"],
            label=f"{case.name} plan bytes",
        ),
        "serving_graph_bytes": _require_int(
            graph_bytes["serving"],
            label=f"{case.name} serving graph bytes",
        ),
        "status": case.result.status.value,
        "training_graph_bytes": _require_int(
            graph_bytes["training"],
            label=f"{case.name} training graph bytes",
        ),
    }


def _build_files() -> dict[Path, bytes]:
    sources = _load_sources()
    by_name = {case.name: case for case in sources.cases}
    compatible = by_name["compatible"]
    drift = by_name["drift"]

    terminal_files: dict[str, bytes] = {}
    for case in sources.cases:
        terminal_files[case.name] = comparison_terminal_svg(
            title=f"unitsentinel compare · {case.result.status.value}",
            transcript=case.transcript,
            transcript_digest=case.transcript_digest,
            source_path=_relative(TRANSCRIPT_PATHS[case.name]),
            expected_lines=EXPECTED_LINE_COUNTS[case.name],
            accent=TERMINAL_ACCENTS[case.name],
            description=(
                f"The full genuine {EXPECTED_LINE_COUNTS[case.name]}-line committed "
                f"CLI transcript for the {case.name} training-serving comparison."
            ),
        ).encode("utf-8")

    files = {
        WORKFLOW_SVG_PATH: comparison_workflow_svg(
            plan_digest=compatible.result.plan_digest,
            registry_digest=sources.registry_digest,
            training_graph_digest=compatible.result.training_graph_digest,
            serving_graph_digest=compatible.result.serving_graph_digest,
            result_digest=compatible.result.digest,
        ).encode("utf-8"),
        LINEAGE_SVG_PATH: comparison_lineage_drift_svg(
            compatible=_lineage_values(compatible),
            drift=_lineage_values(drift),
        ).encode("utf-8"),
        SIZES_SVG_PATH: comparison_artifact_sizes_svg(
            rows=tuple(_size_row(case) for case in sources.cases),
            measurement_scope=sources.measurement_scope,
        ).encode("utf-8"),
        FRAME_MANIFEST_PATH: _canonical_bytes(
            {
                "frames": [
                    {
                        "delay_ms": 3_000,
                        "path": f"frame-{name}.svg",
                    }
                    for name in CASE_NAMES
                ],
                "schema": FRAME_SCHEMA,
            }
        ),
    }
    for name in CASE_NAMES:
        files[TERMINAL_SVG_PATHS[name]] = terminal_files[name]
        files[FRAME_SVG_PATHS[name]] = terminal_files[name]
    if set(files) != EXPECTED_OUTPUT_PATHS:
        raise EvidenceError("comparison visual output allowlist is violated")
    return files


def _write_files(files: dict[Path, bytes]) -> None:
    if set(files) != EXPECTED_OUTPUT_PATHS:
        raise EvidenceError("comparison visual output allowlist is violated")
    for path in sorted(files):
        _atomic_write(path, files[path])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record or verify source-derived UnitSentinel comparison SVG evidence."
        ),
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--record",
        action="store_true",
        help="refresh only the fixed comparison SVG and frame source allowlist",
    )
    modes.add_argument(
        "--check",
        action="store_true",
        help="strictly decode sources and compare exact committed visual bytes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.record:
            _require_recording_environment()
        files = _build_files()
        if arguments.check:
            _check_files(files)
        else:
            _write_files(files)
    except EvidenceError as error:
        sys.stderr.write(f"comparison-visuals: error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
