"""Record and verify the closed UnitSentinel comparison evidence slice.

The recorder constructs all graph and plan inputs from immutable model values,
runs the public ``unitsentinel compare`` CLI for three real outcomes, and then
strictly decodes every emitted result claim.  Recorded byte counts describe the
exact artifacts only; this module does not make a performance claim.
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

from tools.evidence.generate import (
    EVIDENCE,
    PYTHON_DISPLAY,
    RUN_DIRECTORY,
    EvidenceError,
    _atomic_write,
    _canonical_bytes,
    _check_files,
    _managed_run_directory,
    _read_regular_file,
    _relative,
    _require_recording_environment,
    _run_cli,
    _transcript,
)
from unitsentinel.canonical import canonical_json_bytes
from unitsentinel.comparison import (
    AUTHENTICATION_NOT_PROVIDED,
    COMPARISON_RESULT_SCHEMA,
    COMPARISON_SCOPE_UNDER_PLAN,
    ComparisonReason,
    ComparisonResult,
    ComparisonStatus,
    InterfaceSnapshot,
    MismatchCode,
)
from unitsentinel.comparison_codec import encode_comparison_plan
from unitsentinel.comparison_contract import (
    COMPARISON_SCHEMA,
    ComparisonPlan,
    ContractBinding,
    InterfaceEndpoint,
    InterfaceRole,
)
from unitsentinel.comparison_result_codec import (
    ComparisonResultDecodeError,
    decode_comparison_result,
)
from unitsentinel.graph import (
    GRAPH_SCHEMA,
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from unitsentinel.graph_codec import encode_graph
from unitsentinel.registry import BUILTIN_REGISTRY, REGISTRY_SCHEMA
from unitsentinel.verification import SolverLimits, VerificationStatus
from unitsentinel.version import VERSION

COMPARE_CLI_SCHEMA: Final = "unitsentinel.cli.compare/v1"
COMPARISON_ARTIFACTS_SCHEMA: Final = "unitsentinel.comparison-artifacts/v1"
COMPARISON_PROVENANCE_SCHEMA: Final = "unitsentinel.comparison-evidence-provenance/v1"
MEASUREMENT_SCOPE: Final = (
    "exact canonical artifact byte lengths; no latency or performance claim"
)

CONTRACT_DIRECTORY: Final = EVIDENCE / "contracts"
PLAN_DIRECTORY: Final = EVIDENCE / "plans"
CAPTURE_DIRECTORY: Final = EVIDENCE / "captures"
CLAIM_DIRECTORY: Final = EVIDENCE / "claims"
DATA_PATH: Final = EVIDENCE / "data" / "comparison-artifacts.json"
PROVENANCE_PATH: Final = EVIDENCE / "comparison-provenance.json"

TRAINING_GRAPH_PATH: Final = CONTRACT_DIRECTORY / "ratio-training.json"
SERVING_RENAMED_GRAPH_PATH: Final = CONTRACT_DIRECTORY / "ratio-serving-renamed.json"
SERVING_REVERSED_GRAPH_PATH: Final = CONTRACT_DIRECTORY / "ratio-serving-reversed.json"
SERVING_UNDERCONSTRAINED_GRAPH_PATH: Final = (
    CONTRACT_DIRECTORY / "ratio-serving-underconstrained.json"
)

CASE_NAMES: Final = ("compatible", "drift", "indeterminate")
PLAN_PATHS: Final = {
    name: PLAN_DIRECTORY / f"ratio-{name}.plan.json" for name in CASE_NAMES
}
CAPTURE_JSON_PATHS: Final = {
    name: CAPTURE_DIRECTORY / f"compare-{name}.json" for name in CASE_NAMES
}
CAPTURE_TEXT_PATHS: Final = {
    name: CAPTURE_DIRECTORY / f"compare-{name}.txt" for name in CASE_NAMES
}
CLAIM_PATHS: Final = {
    name: CLAIM_DIRECTORY / f"ratio-{name}.result.json" for name in CASE_NAMES
}

INPUT_PATHS: Final = frozenset(
    {
        TRAINING_GRAPH_PATH,
        SERVING_RENAMED_GRAPH_PATH,
        SERVING_REVERSED_GRAPH_PATH,
        SERVING_UNDERCONSTRAINED_GRAPH_PATH,
        *PLAN_PATHS.values(),
    }
)
EXPECTED_OUTPUT_PATHS: Final = frozenset(
    {
        *INPUT_PATHS,
        *CAPTURE_JSON_PATHS.values(),
        *CAPTURE_TEXT_PATHS.values(),
        *CLAIM_PATHS.values(),
        DATA_PATH,
        PROVENANCE_PATH,
    }
)

COMPARISON_LIMITS: Final = SolverLimits(
    per_check_timeout_ms=250,
    total_timeout_ms=5_000,
    max_memory_mb=256,
    max_core_shrink_checks=64,
    max_uniqueness_checks=577,
)


@dataclass(frozen=True, slots=True)
class EvidenceCase:
    """One fixed CLI scenario and its exact expected semantic outcome."""

    name: str
    serving_graph: ComputationGraph
    serving_path: Path
    plan: ComparisonPlan
    status: ComparisonStatus
    reason: ComparisonReason | None
    exit_code: int


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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _value(value_id: str, unit_id: str | None) -> ValueSpec:
    return ValueSpec(
        value_id=value_id,
        dtype=ScalarType.FLOAT64,
        shape=("batch",),
        unit_id=unit_id,
    )


def _ratio_graph(
    graph_id: str,
    *,
    left_id: str,
    right_id: str,
    output_id: str,
    reversed_divide: bool = False,
    input_unit: str | None = "meter",
) -> ComputationGraph:
    operands = (right_id, left_id) if reversed_divide else (left_id, right_id)
    return ComputationGraph(
        graph_id=graph_id,
        values=tuple(
            sorted(
                (
                    _value(left_id, input_unit),
                    _value(right_id, input_unit),
                    _value(output_id, "one"),
                ),
                key=lambda item: item.value_id,
            )
        ),
        inputs=(left_id, right_id),
        nodes=(
            Node(
                node_id="normalize-ratio",
                operation=Operation.DIVIDE,
                inputs=operands,
                output=output_id,
            ),
        ),
        outputs=(output_id,),
    )


def _plan(
    comparison_id: str,
    *,
    training: ComputationGraph,
    serving: ComputationGraph,
) -> ComparisonPlan:
    bindings = (
        ContractBinding(
            contract_id="input-00",
            training=InterfaceEndpoint(InterfaceRole.INPUT, "numerator"),
            serving=InterfaceEndpoint(InterfaceRole.INPUT, "request-numerator"),
        ),
        ContractBinding(
            contract_id="input-01",
            training=InterfaceEndpoint(InterfaceRole.INPUT, "denominator"),
            serving=InterfaceEndpoint(InterfaceRole.INPUT, "request-denominator"),
        ),
        ContractBinding(
            contract_id="output-00",
            training=InterfaceEndpoint(InterfaceRole.OUTPUT, "ratio"),
            serving=InterfaceEndpoint(InterfaceRole.OUTPUT, "prediction"),
        ),
    )
    return ComparisonPlan(
        comparison_id=comparison_id,
        training_graph_digest=training.digest,
        serving_graph_digest=serving.digest,
        registry_digest=BUILTIN_REGISTRY.digest,
        bindings=bindings,
    )


def _models() -> tuple[ComputationGraph, tuple[EvidenceCase, ...]]:
    training = _ratio_graph(
        "ratio-training",
        left_id="numerator",
        right_id="denominator",
        output_id="ratio",
    )
    serving_renamed = _ratio_graph(
        "ratio-serving-renamed",
        left_id="request-numerator",
        right_id="request-denominator",
        output_id="prediction",
    )
    serving_reversed = _ratio_graph(
        "ratio-serving-reversed",
        left_id="request-numerator",
        right_id="request-denominator",
        output_id="prediction",
        reversed_divide=True,
    )
    serving_underconstrained = _ratio_graph(
        "ratio-serving-underconstrained",
        left_id="request-numerator",
        right_id="request-denominator",
        output_id="prediction",
        input_unit=None,
    )
    return (
        training,
        (
            EvidenceCase(
                name="compatible",
                serving_graph=serving_renamed,
                serving_path=SERVING_RENAMED_GRAPH_PATH,
                plan=_plan(
                    "ratio-compatible",
                    training=training,
                    serving=serving_renamed,
                ),
                status=ComparisonStatus.COMPATIBLE,
                reason=None,
                exit_code=0,
            ),
            EvidenceCase(
                name="drift",
                serving_graph=serving_reversed,
                serving_path=SERVING_REVERSED_GRAPH_PATH,
                plan=_plan(
                    "ratio-normalization-drift",
                    training=training,
                    serving=serving_reversed,
                ),
                status=ComparisonStatus.DRIFT,
                reason=None,
                exit_code=5,
            ),
            EvidenceCase(
                name="indeterminate",
                serving_graph=serving_underconstrained,
                serving_path=SERVING_UNDERCONSTRAINED_GRAPH_PATH,
                plan=_plan(
                    "ratio-serving-indeterminate",
                    training=training,
                    serving=serving_underconstrained,
                ),
                status=ComparisonStatus.INDETERMINATE,
                reason=ComparisonReason.SERVING_NOT_VERIFIED,
                exit_code=3,
            ),
        ),
    )


def _fixture_payloads(
    training: ComputationGraph,
    cases: tuple[EvidenceCase, ...],
) -> dict[Path, bytes]:
    files = {TRAINING_GRAPH_PATH: encode_graph(training)}
    for case in cases:
        files[case.serving_path] = encode_graph(case.serving_graph)
        files[PLAN_PATHS[case.name]] = encode_comparison_plan(case.plan)
    if set(files) != INPUT_PATHS:
        raise EvidenceError("comparison evidence input allowlist is violated")
    return files


def _prepare_or_check_inputs(
    files: dict[Path, bytes],
    *,
    prepare_inputs: bool,
) -> None:
    if set(files) != INPUT_PATHS:
        raise EvidenceError("comparison evidence input allowlist is violated")
    for path, payload in sorted(files.items()):
        if prepare_inputs:
            _atomic_write(path, payload)
            continue
        try:
            current = _read_regular_file(
                path,
                purpose=f"comparison evidence input {_relative(path)}",
            )
        except EvidenceError:
            raise EvidenceError(f"stale evidence input: {_relative(path)}") from None
        if current != payload:
            raise EvidenceError(f"stale evidence input: {_relative(path)}")


def _result_run_path(case: EvidenceCase, *, machine_readable: bool) -> Path:
    mode = "json" if machine_readable else "text"
    return RUN_DIRECTORY / f"compare-{case.name}-{mode}.result.json"


def _argument_groups(
    case: EvidenceCase,
    *,
    machine_readable: bool,
) -> tuple[tuple[str, ...], ...]:
    return (
        ("compare",),
        (_relative(PLAN_PATHS[case.name]),),
        ("--training-graph", _relative(TRAINING_GRAPH_PATH)),
        ("--serving-graph", _relative(case.serving_path)),
        ("--expect-plan-sha256", case.plan.digest),
        (
            "--result",
            _relative(
                _result_run_path(case, machine_readable=machine_readable),
            ),
        ),
        (
            "--per-check-timeout-ms",
            str(COMPARISON_LIMITS.per_check_timeout_ms),
        ),
        ("--total-timeout-ms", str(COMPARISON_LIMITS.total_timeout_ms)),
        ("--max-memory-mb", str(COMPARISON_LIMITS.max_memory_mb)),
        (
            "--max-core-shrink-checks",
            str(COMPARISON_LIMITS.max_core_shrink_checks),
        ),
        (
            "--max-uniqueness-checks",
            str(COMPARISON_LIMITS.max_uniqueness_checks),
        ),
    )


def _arguments(
    case: EvidenceCase,
    *,
    machine_readable: bool,
) -> tuple[str, ...]:
    arguments = tuple(
        token
        for group in _argument_groups(
            case,
            machine_readable=machine_readable,
        )
        for token in group
    )
    return (*arguments, "--json") if machine_readable else arguments


def _command_lines(case: EvidenceCase) -> tuple[str, ...]:
    groups = _argument_groups(case, machine_readable=False)
    lines = [
        f"$ {PYTHON_DISPLAY} -m unitsentinel {' '.join(groups[0])} \\",
    ]
    for index, group in enumerate(groups[1:], start=1):
        continuation = " \\" if index < len(groups) - 1 else ""
        lines.append(f"    {' '.join(group)}{continuation}")
    return tuple(lines)


def _detached_record(
    value: object,
    *,
    label: str,
) -> tuple[dict[str, Any], str]:
    envelope = _record(
        value,
        label=label,
        fields={"record", "sha256"},
    )
    record = _record(
        envelope["record"],
        label=f"{label} record",
        fields=set(cast(dict[str, Any], envelope["record"])),
    )
    digest = envelope["sha256"]
    if type(digest) is not str or digest != _sha256(canonical_json_bytes(record)):
        raise EvidenceError(f"{label} digest does not bind its record")
    return record, digest


def _verification_status(
    result_record: dict[str, Any],
    *,
    side: str,
) -> tuple[str, str]:
    verification = _record(
        result_record["verification"],
        label="comparison verification",
        fields={"serving", "training"},
    )
    record, digest = _detached_record(
        verification[side],
        label=f"{side} verification",
    )
    status = record.get("status")
    if type(status) is not str:
        raise EvidenceError(f"{side} verification status is malformed")
    return status, digest


def _lineage_digest(
    result_record: dict[str, Any],
    *,
    side: str,
) -> str | None:
    lineages = _record(
        result_record["normalization_lineage"],
        label="comparison normalization lineage",
        fields={"serving", "training"},
    )
    value = lineages[side]
    if value is None:
        return None
    _, digest = _detached_record(value, label=f"{side} normalization lineage")
    return digest


def _mismatch_codes(result_record: dict[str, Any]) -> tuple[str, ...]:
    bindings = result_record.get("bindings")
    if type(bindings) is not list:
        raise EvidenceError("comparison result bindings are malformed")
    codes: list[str] = []
    for binding_value in bindings:
        binding = _record(
            binding_value,
            label="comparison result binding",
            fields={
                "contract_id",
                "mismatches",
                "normalization",
                "serving",
                "training",
            },
        )
        mismatches = binding["mismatches"]
        if type(mismatches) is not list or any(
            type(code) is not str for code in mismatches
        ):
            raise EvidenceError("comparison mismatch codes are malformed")
        codes.extend(cast(list[str], mismatches))
    return tuple(codes)


def _output_normalization(
    result: ComparisonResult,
) -> tuple[str | None, str | None]:
    matches = [
        comparison
        for comparison in result.comparisons
        if comparison.contract_id == "output-00"
    ]
    if not matches:
        return None, None
    if len(matches) != 1 or matches[0].normalization is None:
        raise EvidenceError("output normalization comparison is incomplete")
    normalization = matches[0].normalization
    return normalization.training_digest, normalization.serving_digest


def _validate_capture(
    value: object,
    *,
    case: EvidenceCase,
    training: ComputationGraph,
    claim_payload: bytes,
) -> tuple[dict[str, Any], ComparisonResult]:
    capture = _record(
        value,
        label=f"{case.name} comparison CLI capture",
        fields={
            "exit_code",
            "graphs",
            "plan",
            "registry",
            "result",
            "result_output",
            "schema",
            "tool",
        },
    )
    if (
        capture["schema"] != COMPARE_CLI_SCHEMA
        or capture["exit_code"] != case.exit_code
        or capture["result_output"] != "written"
    ):
        raise EvidenceError(f"{case.name} comparison CLI outcome is stale")
    tool = _record(
        capture["tool"],
        label="comparison CLI tool",
        fields={"name", "version"},
    )
    if tool != {"name": "unitsentinel", "version": VERSION}:
        raise EvidenceError("comparison CLI tool identity is stale")

    graphs = _record(
        capture["graphs"],
        label="comparison CLI graphs",
        fields={"serving", "training"},
    )
    expected_graphs = {
        "serving": {
            "graph_id": case.serving_graph.graph_id,
            "schema": GRAPH_SCHEMA,
            "sha256": case.serving_graph.digest,
        },
        "training": {
            "graph_id": training.graph_id,
            "schema": GRAPH_SCHEMA,
            "sha256": training.digest,
        },
    }
    if graphs != expected_graphs:
        raise EvidenceError("comparison CLI graph bindings are stale")

    plan = _record(
        capture["plan"],
        label="comparison CLI plan",
        fields={"authentication", "expected_sha256", "schema", "sha256"},
    )
    if plan != {
        "authentication": AUTHENTICATION_NOT_PROVIDED,
        "expected_sha256": case.plan.digest,
        "schema": COMPARISON_SCHEMA,
        "sha256": case.plan.digest,
    }:
        raise EvidenceError("comparison CLI plan binding is stale")

    registry = _record(
        capture["registry"],
        label="comparison CLI registry",
        fields={"schema", "sha256", "version"},
    )
    if registry != {
        "schema": REGISTRY_SCHEMA,
        "sha256": BUILTIN_REGISTRY.digest,
        "version": BUILTIN_REGISTRY.version,
    }:
        raise EvidenceError("comparison CLI registry binding is stale")

    result_envelope = _record(
        capture["result"],
        label="comparison CLI result",
        fields={"authentication", "record", "schema", "sha256"},
    )
    if (
        result_envelope["authentication"] != AUTHENTICATION_NOT_PROVIDED
        or result_envelope["schema"] != COMPARISON_RESULT_SCHEMA
    ):
        raise EvidenceError("comparison CLI result trust metadata is stale")
    if type(result_envelope["record"]) is not dict:
        raise EvidenceError("comparison CLI result record is malformed")
    result_record = cast(dict[str, Any], result_envelope["record"])
    if claim_payload != canonical_json_bytes(result_record):
        raise EvidenceError("comparison result claim differs from CLI stdout")
    try:
        result = decode_comparison_result(claim_payload)
    except ComparisonResultDecodeError:
        raise EvidenceError(
            "comparison result claim is not strictly decodable"
        ) from None
    if (
        result_envelope["sha256"] != result.digest
        or _sha256(claim_payload) != result.digest
        or result.status is not case.status
        or result.reason is not case.reason
        or result.scope != COMPARISON_SCOPE_UNDER_PLAN
        or result.authentication != AUTHENTICATION_NOT_PROVIDED
        or result.comparison_id != case.plan.comparison_id
        or result.plan_digest != case.plan.digest
        or result.training_graph_digest != training.digest
        or result.serving_graph_digest != case.serving_graph.digest
        or result.registry_digest != BUILTIN_REGISTRY.digest
        or result.limits != COMPARISON_LIMITS
    ):
        raise EvidenceError("comparison result claim bindings are stale")

    training_status, _ = _verification_status(result_record, side="training")
    serving_status, _ = _verification_status(result_record, side="serving")
    if training_status != VerificationStatus.VERIFIED.value:
        raise EvidenceError("comparison training verification is not verified")
    expected_serving_status = (
        VerificationStatus.UNDERCONSTRAINED.value
        if case.status is ComparisonStatus.INDETERMINATE
        else VerificationStatus.VERIFIED.value
    )
    if serving_status != expected_serving_status:
        raise EvidenceError("comparison serving verification outcome is stale")

    training_lineage = _lineage_digest(result_record, side="training")
    serving_lineage = _lineage_digest(result_record, side="serving")
    mismatch_codes = _mismatch_codes(result_record)
    training_normalization, serving_normalization = _output_normalization(result)
    if case.status is ComparisonStatus.COMPATIBLE:
        if (
            len(result.comparisons) != 3
            or mismatch_codes
            or training_lineage is None
            or serving_lineage is None
            or training_normalization is None
            or training_normalization != serving_normalization
        ):
            raise EvidenceError("compatible comparison evidence is incomplete")
    elif case.status is ComparisonStatus.DRIFT:
        if (
            len(result.comparisons) != 3
            or mismatch_codes != (MismatchCode.NORMALIZATION_LINEAGE_DRIFT.value,)
            or training_lineage is None
            or serving_lineage is None
            or training_normalization is None
            or serving_normalization is None
            or training_normalization == serving_normalization
        ):
            raise EvidenceError("drift comparison evidence is not lineage-only")
    elif (
        result.comparisons
        or mismatch_codes
        or training_lineage is not None
        or serving_lineage is not None
        or training_normalization is not None
        or serving_normalization is not None
    ):
        raise EvidenceError("indeterminate evidence publishes an interface diff")
    return capture, result


def _snapshot_text(snapshot: InterfaceSnapshot | None) -> str:
    if snapshot is None:
        return "absent"
    return (
        f"{snapshot.endpoint.role.value}:{snapshot.endpoint.value_id}"
        f"@{snapshot.position}"
    )


def _expected_text_output(
    *,
    case: EvidenceCase,
    training: ComputationGraph,
    result: ComparisonResult,
) -> bytes:
    reason = "none" if result.reason is None else result.reason.value
    lines = [
        f"UnitSentinel comparison: {case.status.value.upper()}",
        f"exit code: {case.exit_code}",
        f"tool: unitsentinel {VERSION}",
        f"reason: {reason}",
        f"result scope: {COMPARISON_SCOPE_UNDER_PLAN}",
        f"comparison id: {case.plan.comparison_id}",
        f"plan sha256: {case.plan.digest}",
        f"expected plan sha256: {case.plan.digest}",
        f"plan authentication: {AUTHENTICATION_NOT_PROVIDED}",
        f"training graph id: {training.graph_id}",
        f"training graph sha256: {training.digest}",
        f"serving graph id: {case.serving_graph.graph_id}",
        f"serving graph sha256: {case.serving_graph.digest}",
        f"registry version: {BUILTIN_REGISTRY.version}",
        f"registry sha256: {BUILTIN_REGISTRY.digest}",
        (
            "solver limits per graph side: "
            f"check={result.limits.per_check_timeout_ms}ms "
            f"total={result.limits.total_timeout_ms}ms "
            f"memory={result.limits.max_memory_mb}MiB "
            f"core-shrink={result.limits.max_core_shrink_checks} "
            f"uniqueness={result.limits.max_uniqueness_checks}"
        ),
        f"result sha256: {result.digest}",
        f"result authentication: {AUTHENTICATION_NOT_PROVIDED}",
    ]
    for side, verification in (
        ("training", result.training_result),
        ("serving", result.serving_result),
    ):
        if verification is None:
            lines.append(f"{side} verification: unavailable")
        else:
            lines.append(
                f"{side} verification: {verification.status.value} "
                f"{verification.digest}"
            )
    for side, lineage in (
        ("training", result.training_lineage),
        ("serving", result.serving_lineage),
    ):
        lines.append(
            f"{side} normalization lineage: "
            + ("unavailable" if lineage is None else lineage.digest)
        )
    lines.append(
        f"bindings ({len(result.comparisons)}, mismatches={result.mismatch_count}):"
    )
    for comparison in result.comparisons:
        mismatch_text = (
            "compatible"
            if not comparison.mismatches
            else ",".join(code.value for code in comparison.mismatches)
        )
        line = (
            f"  {comparison.contract_id} | "
            f"{_snapshot_text(comparison.training)} -> "
            f"{_snapshot_text(comparison.serving)} | "
            f"{mismatch_text}"
        )
        if comparison.normalization is not None:
            line += (
                " | normalization="
                f"{comparison.normalization.training_digest}->"
                f"{comparison.normalization.serving_digest}"
            )
        lines.append(line)
    lines.append("comparison result output: written")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _validate_text_output(
    output: bytes,
    *,
    case: EvidenceCase,
    training: ComputationGraph,
    result: ComparisonResult,
) -> None:
    if output != _expected_text_output(
        case=case,
        training=training,
        result=result,
    ):
        raise EvidenceError("comparison text output is not the exact bound report")


def _artifact_row(
    *,
    case: EvidenceCase,
    training: ComputationGraph,
    capture_json: bytes,
    capture_text: bytes,
    claim: bytes,
) -> dict[str, object]:
    return {
        "capture_json_bytes": len(capture_json),
        "capture_text_bytes": len(capture_text),
        "claim_bytes": len(claim),
        "claim_sha256": _sha256(claim),
        "exit_code": case.exit_code,
        "graph_bytes": {
            "serving": len(case.serving_graph.canonical_bytes()),
            "training": len(training.canonical_bytes()),
        },
        "name": case.name,
        "plan_bytes": len(case.plan.canonical_bytes()),
        "plan_sha256": case.plan.digest,
        "status": case.status.value,
    }


def _provenance_case(
    *,
    case: EvidenceCase,
    training: ComputationGraph,
    capture_json: bytes,
    capture_text: bytes,
    claim: bytes,
    result: ComparisonResult,
) -> dict[str, object]:
    result_record = cast(dict[str, Any], json.loads(claim))
    training_normalization, serving_normalization = _output_normalization(result)
    training_status, training_verification_digest = _verification_status(
        result_record,
        side="training",
    )
    serving_status, serving_verification_digest = _verification_status(
        result_record,
        side="serving",
    )
    return {
        "captures": {
            "json": {
                "path": _relative(CAPTURE_JSON_PATHS[case.name]),
                "sha256": _sha256(capture_json),
            },
            "text": {
                "path": _relative(CAPTURE_TEXT_PATHS[case.name]),
                "sha256": _sha256(capture_text),
            },
        },
        "claim": {
            "authentication": AUTHENTICATION_NOT_PROVIDED,
            "path": _relative(CLAIM_PATHS[case.name]),
            "scope": COMPARISON_SCOPE_UNDER_PLAN,
            "sha256": result.digest,
        },
        "exit_code": case.exit_code,
        "graphs": {
            "serving": {
                "graph_id": case.serving_graph.graph_id,
                "path": _relative(case.serving_path),
                "sha256": case.serving_graph.digest,
            },
            "training": {
                "graph_id": training.graph_id,
                "path": _relative(TRAINING_GRAPH_PATH),
                "sha256": result.training_graph_digest,
            },
        },
        "mismatches": list(_mismatch_codes(result_record)),
        "name": case.name,
        "normalization_lineage": {
            "serving_sha256": _lineage_digest(result_record, side="serving"),
            "training_sha256": _lineage_digest(result_record, side="training"),
        },
        "output_normalization": {
            "serving_sha256": serving_normalization,
            "training_sha256": training_normalization,
        },
        "plan": {
            "authentication": AUTHENTICATION_NOT_PROVIDED,
            "path": _relative(PLAN_PATHS[case.name]),
            "sha256": case.plan.digest,
        },
        "reason": None if case.reason is None else case.reason.value,
        "status": case.status.value,
        "verification": {
            "serving": {
                "sha256": serving_verification_digest,
                "status": serving_status,
            },
            "training": {
                "sha256": training_verification_digest,
                "status": training_status,
            },
        },
    }


def _build_evidence(*, prepare_inputs: bool) -> dict[Path, bytes]:
    training, cases = _models()
    fixture_files = _fixture_payloads(training, cases)
    _prepare_or_check_inputs(fixture_files, prepare_inputs=prepare_inputs)

    files: dict[Path, bytes] = dict(fixture_files)
    artifact_rows: list[dict[str, object]] = []
    provenance_cases: list[dict[str, object]] = []
    for case in cases:
        json_output = _run_cli(
            _arguments(case, machine_readable=True),
            expected_exit=case.exit_code,
        )
        json_result_path = _result_run_path(case, machine_readable=True)
        claim = _read_regular_file(
            json_result_path,
            purpose=f"{case.name} comparison result claim",
        )
        try:
            decoded_json = json.loads(json_output)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EvidenceError(
                f"{case.name} comparison CLI did not emit canonical JSON"
            ) from None
        if json_output != _canonical_bytes(decoded_json):
            raise EvidenceError(f"{case.name} comparison CLI output is not canonical")
        capture, result = _validate_capture(
            decoded_json,
            case=case,
            training=training,
            claim_payload=claim,
        )
        if capture != decoded_json:
            raise EvidenceError("comparison capture validation changed its record")

        text_output = _run_cli(
            _arguments(case, machine_readable=False),
            expected_exit=case.exit_code,
        )
        text_result_path = _result_run_path(case, machine_readable=False)
        text_claim = _read_regular_file(
            text_result_path,
            purpose=f"{case.name} text comparison result claim",
        )
        if text_claim != claim:
            raise EvidenceError(
                f"{case.name} text and JSON runs emitted different claims"
            )
        _validate_text_output(
            text_output,
            case=case,
            training=training,
            result=result,
        )
        transcript = _transcript(
            command_lines=_command_lines(case),
            output=text_output,
            exit_code=case.exit_code,
        )

        files[CAPTURE_JSON_PATHS[case.name]] = json_output
        files[CAPTURE_TEXT_PATHS[case.name]] = transcript
        files[CLAIM_PATHS[case.name]] = claim
        artifact_rows.append(
            _artifact_row(
                case=case,
                training=training,
                capture_json=json_output,
                capture_text=transcript,
                claim=claim,
            )
        )
        provenance_cases.append(
            _provenance_case(
                case=case,
                training=training,
                capture_json=json_output,
                capture_text=transcript,
                claim=claim,
                result=result,
            )
        )

    for path, expected in fixture_files.items():
        current = _read_regular_file(
            path,
            purpose=f"comparison evidence input {_relative(path)}",
        )
        if current != expected:
            raise EvidenceError("comparison CLI changed an evidence input")

    artifacts = {
        "artifacts": artifact_rows,
        "measurement_scope": MEASUREMENT_SCOPE,
        "schema": COMPARISON_ARTIFACTS_SCHEMA,
    }
    provenance = {
        "cases": provenance_cases,
        "limits": COMPARISON_LIMITS.canonical_record(),
        "registry": {
            "schema": REGISTRY_SCHEMA,
            "sha256": BUILTIN_REGISTRY.digest,
            "version": BUILTIN_REGISTRY.version,
        },
        "schema": COMPARISON_PROVENANCE_SCHEMA,
        "tool": {"name": "unitsentinel", "version": VERSION},
        "trust": {
            "claim_authentication": AUTHENTICATION_NOT_PROVIDED,
            "comparison_scope": COMPARISON_SCOPE_UNDER_PLAN,
            "plan_authentication": AUTHENTICATION_NOT_PROVIDED,
        },
    }
    files[DATA_PATH] = _canonical_bytes(artifacts)
    files[PROVENANCE_PATH] = _canonical_bytes(provenance)
    if set(files) != EXPECTED_OUTPUT_PATHS:
        raise EvidenceError("comparison evidence output allowlist is violated")
    return files


def _write_files(files: dict[Path, bytes]) -> None:
    if set(files) != EXPECTED_OUTPUT_PATHS:
        raise EvidenceError("comparison evidence output allowlist is violated")
    for path in sorted(files):
        _atomic_write(path, files[path])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record or verify the closed UnitSentinel comparison evidence slice."
        ),
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--record",
        action="store_true",
        help="refresh only the fixed comparison fixtures, captures, claims, and data",
    )
    modes.add_argument(
        "--check",
        action="store_true",
        help="re-execute all fixed comparisons and compare exact committed bytes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.record:
            _require_recording_environment()
        with _managed_run_directory():
            files = _build_evidence(prepare_inputs=cast(bool, arguments.record))
        if arguments.check:
            _check_files(files)
        else:
            _write_files(files)
    except EvidenceError as error:
        sys.stderr.write(f"comparison-evidence: error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
