"""Generate reproducible, source-derived ONNX adapter evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, cast

import onnx
from onnx import TensorProto, helper

from tools.evidence.generate import (
    ASSETS,
    CLI_TIMEOUT_SECONDS,
    EVIDENCE,
    MAX_CLI_STDERR_BYTES,
    MAX_CLI_STDOUT_BYTES,
    PYTHON_DISPLAY,
    ROOT,
    RUN_DIRECTORY,
    EvidenceError,
    _atomic_write,
    _canonical_bytes,
    _capture_bounded_process,
    _check_files,
    _managed_run_directory,
    _transcript,
)
from tools.evidence.visuals import (
    AMBER,
    CYAN,
    GREEN,
    MUTED,
    RED,
    VIOLET,
    _arrow,
    _box,
    _document,
    _text,
    terminal_svg,
)
from unitsentinel import (
    ONNX_CONTRACT_METADATA_KEY,
    ONNX_CONTRACT_SCHEMA,
    ComputationGraph,
    ValueSpec,
    VerificationStatus,
    decode_graph,
    import_onnx_model,
    verify_graph,
)
from unitsentinel.canonical import sha256_hex
from unitsentinel.version import VERSION

PROVENANCE_SCHEMA: Final = "unitsentinel.onnx-evidence/v2"
REJECTIONS_SCHEMA: Final = "unitsentinel.onnx-rejections/v1"
FRAME_SCHEMA: Final = "unitsentinel.onnx-demo-frames/v1"
MODEL_NAME: Final = "speed-contract.onnx"
GRAPH_NAME: Final = "onnx-speed.graph.json"
MODEL_RELATIVE: Final = f"docs/evidence/models/{MODEL_NAME}"
GRAPH_RELATIVE: Final = f"docs/evidence/contracts/{GRAPH_NAME}"


def _contract() -> dict[str, object]:
    return {
        "graph_id": "onnx-speed-contract",
        "nodes": [
            {
                "node_id": "derive-speed",
                "onnx_name": "derive-speed",
            }
        ],
        "schema": ONNX_CONTRACT_SCHEMA,
        "values": [
            {
                "onnx_name": "distance",
                "unit_id": "meter",
                "value_id": "distance",
            },
            {
                "onnx_name": "duration",
                "unit_id": "second",
                "value_id": "duration",
            },
            {
                "onnx_name": "speed",
                "unit_id": "meter-per-second",
                "value_id": "speed",
            },
        ],
    }


def _model() -> onnx.ModelProto:
    graph = helper.make_graph(
        [
            helper.make_node(
                "Div",
                ["distance", "duration"],
                ["speed"],
                name="derive-speed",
            )
        ],
        "unitsentinel-speed-model",
        [
            helper.make_tensor_value_info(
                "distance",
                TensorProto.FLOAT,
                [4, 8],
            ),
            helper.make_tensor_value_info(
                "duration",
                TensorProto.FLOAT,
                [4, 8],
            ),
        ],
        [
            helper.make_tensor_value_info(
                "speed",
                TensorProto.FLOAT,
                [4, 8],
            )
        ],
    )
    model = helper.make_model(
        graph,
        producer_name="unitsentinel-evidence",
        opset_imports=[helper.make_opsetid("", 13)],
        ir_version=8,
    )
    metadata = model.metadata_props.add()
    metadata.key = ONNX_CONTRACT_METADATA_KEY
    metadata.value = _canonical_bytes(_contract()).rstrip(b"\n").decode()
    return model


def _serialize(model: onnx.ModelProto) -> bytes:
    payload = cast(
        bytes,
        cast(Any, model).SerializeToString(deterministic=True),
    )
    if not payload:
        raise EvidenceError("ONNX evidence model serialization is empty")
    return payload


def _capture_cli(
    arguments: Sequence[str],
) -> tuple[int, bytes, bytes]:
    environment = {
        "HOME": str(RUN_DIRECTORY),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(ROOT / "src"),
    }
    return _capture_bounded_process(
        [sys.executable, "-m", "unitsentinel", *arguments],
        cwd=ROOT,
        environment=environment,
        timeout_seconds=CLI_TIMEOUT_SECONDS,
        stdout_limit=MAX_CLI_STDOUT_BYTES,
        stderr_limit=MAX_CLI_STDERR_BYTES,
    )


def _positive_cli(
    arguments: Sequence[str],
) -> bytes:
    return_code, stdout, stderr = _capture_cli(arguments)
    if return_code != 0 or stderr or not stdout.endswith(b"\n"):
        raise EvidenceError("positive ONNX CLI evidence capture failed")
    return stdout


def _rejected_cli(
    arguments: Sequence[str],
) -> str:
    return_code, stdout, stderr = _capture_cli(arguments)
    if return_code != 4 or stdout or not stderr.endswith(b"\n"):
        raise EvidenceError("negative ONNX CLI evidence capture failed")
    diagnostic = stderr.decode("utf-8", errors="strict").rstrip("\n")
    if str(ROOT) in diagnostic or diagnostic.count("\n") != 0:
        raise EvidenceError(
            "negative ONNX CLI evidence leaked a path or multiline detail"
        )
    return diagnostic


def _relative_run(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _dynamic_shape_payload(payload: bytes) -> bytes:
    model = onnx.load_model_from_string(payload)
    dimension = model.graph.input[0].type.tensor_type.shape.dim[0]
    dimension.ClearField("dim_value")
    dimension.dim_param = "batch"
    return _serialize(model)


def _pow_payload(payload: bytes) -> bytes:
    model = onnx.load_model_from_string(payload)
    model.graph.node[0].op_type = "Pow"
    return _serialize(model)


def _initializer_payload(payload: bytes) -> bytes:
    model = onnx.load_model_from_string(payload)
    model.graph.initializer.append(
        helper.make_tensor(
            "embedded-weight",
            TensorProto.FLOAT,
            [1],
            [1.0],
        )
    )
    return _serialize(model)


def _short_digest(value: str) -> str:
    return f"{value[:16]}…"


def _architecture_svg(
    receipt: dict[str, Any],
    *,
    receipt_digest: str,
    rejections: list[dict[str, object]],
) -> str:
    checker = cast(dict[str, Any], receipt["checker"])
    contract = cast(dict[str, Any], receipt["contract"])
    graph = cast(dict[str, Any], receipt["graph"])
    model = cast(dict[str, Any], receipt["model"])
    opset = cast(dict[str, Any], model["opset"])
    operators = cast(list[dict[str, Any]], receipt["operators"])
    if len(operators) != 1:
        raise EvidenceError("ONNX architecture evidence requires one operator")
    operator = operators[0]
    rejection_names = " · ".join(str(item["case"]) for item in rejections)
    published = sum(item["graph_published"] is True for item in rejections)
    body = [
        _text(
            55,
            66,
            "Checked ONNX metadata → canonical dimensional proof",
            size=31,
            weight=700,
        ),
        _text(
            55,
            104,
            "Every displayed count and identity comes from the actual CLI receipt.",
            size=17,
            fill=MUTED,
        ),
        _box(
            45,
            185,
            235,
            210,
            title="1 · BYTES",
            lines=(
                f"{model['bytes']} bytes",
                _short_digest(str(model["sha256"])),
                "bounded regular file",
                "protobuf parse only",
            ),
            accent=CYAN,
        ),
        _box(
            330,
            185,
            235,
            210,
            title="2 · CHECKER",
            lines=(
                f"onnx {checker['runtime_version']}",
                f"full_check = {str(checker['full_check']).lower()}",
                (
                    "custom domains = "
                    f"{str(checker['custom_domain_check']).lower()}"
                ),
                f"model_executed = {str(model['model_executed']).lower()}",
            ),
            accent=GREEN,
        ),
        _box(
            615,
            185,
            235,
            210,
            title="3 · CONTRACT",
            lines=(
                str(contract["schema"]).rsplit("/", maxsplit=1)[-1],
                _short_digest(str(contract["sha256"])),
                f"{graph['values']} explicit values",
                f"{graph['nodes']} explicit nodes",
            ),
            accent=VIOLET,
        ),
        _box(
            900,
            185,
            235,
            210,
            title="4 · LOWER",
            lines=(
                f"IR {model['ir_version']} · opset {opset['version']}",
                (
                    f"{operator['onnx_op_type']} → "
                    f"{operator['unitsentinel_operation']}"
                ),
                "static tensor contract",
                "closed subset",
            ),
            accent=AMBER,
        ),
        _box(
            1185,
            185,
            210,
            210,
            title="5 · GRAPH",
            lines=(
                str(graph["graph_id"]),
                _short_digest(str(graph["sha256"])),
                str(graph["schema"]),
                "ready to verify",
            ),
            accent=GREEN,
        ),
        _arrow(280, 290, 330, 290),
        _arrow(565, 290, 615, 290),
        _arrow(850, 290, 900, 290),
        _arrow(1135, 290, 1185, 290),
        _box(
            70,
            535,
            1300,
            230,
            title="RECORDED FAIL-CLOSED BOUNDARY",
            lines=(
                f"Recorded CLI rejection cases ({len(rejections)}):",
                rejection_names,
                f"Published graph outputs: {published}",
                (
                    f"receipt {_short_digest(receipt_digest)} · "
                    f"external_data={str(model['external_data']).lower()}"
                ),
            ),
            accent=RED,
        ),
    ]
    return _document(
        width=1_440,
        height=900,
        title="UnitSentinel ONNX adapter architecture",
        description=(
            "The receipt-derived bounded parsing, official checking, versioned "
            "metadata, closed-subset lowering, and canonical graph boundary."
        ),
        body="\n".join(body),
    )


def _shape_text(shape: tuple[int | str, ...]) -> str:
    return "[" + ",".join(str(dimension) for dimension in shape) + "]"


def _lowered_graph_svg(
    graph: ComputationGraph,
    receipt: dict[str, Any],
    *,
    receipt_digest: str,
) -> str:
    operators = cast(list[dict[str, Any]], receipt["operators"])
    receipt_graph = cast(dict[str, Any], receipt["graph"])
    receipt_model = cast(dict[str, Any], receipt["model"])
    if (
        len(graph.inputs) != 2
        or len(graph.nodes) != 1
        or len(graph.outputs) != 1
        or len(operators) != 1
    ):
        raise EvidenceError("ONNX lowered-graph visual requires a 2:1:1 fixture")
    by_id = {value.value_id: value for value in graph.values}
    try:
        left = by_id[graph.inputs[0]]
        right = by_id[graph.inputs[1]]
        output = by_id[graph.outputs[0]]
    except KeyError:
        raise EvidenceError("ONNX visual graph references a missing value") from None
    node = graph.nodes[0]
    operator = operators[0]
    if (
        operator["node_id"] != node.node_id
        or operator["unitsentinel_operation"] != node.operation.value
    ):
        raise EvidenceError("ONNX receipt operator does not match the lowered graph")

    def value_lines(value: ValueSpec, *, role: str) -> tuple[str, ...]:
        return (
            role,
            f"{value.dtype.value} {_shape_text(value.shape)}",
            f"value_id = {value.value_id}",
            f"unit_id = {value.unit_id if value.unit_id is not None else 'null'}",
        )

    body = [
        _text(
            55,
            65,
            "One synthetic ModelProto, one explicit dimensional graph",
            size=31,
            weight=700,
        ),
        _text(
            55,
            102,
            "Canonical IDs, tensor contracts, units, and mapping are receipt-derived.",
            size=17,
            fill=MUTED,
        ),
        _box(
            70,
            210,
            310,
            205,
            title=left.value_id,
            lines=value_lines(left, role="canonical input"),
            accent=CYAN,
        ),
        _box(
            70,
            520,
            310,
            205,
            title=right.value_id,
            lines=value_lines(right, role="canonical input"),
            accent=CYAN,
        ),
        _box(
            565,
            365,
            310,
            210,
            title=node.node_id,
            lines=(
                f"ONNX {operator['onnx_op_type']}",
                "↓ explicit receipt mapping",
                f"UnitSentinel {operator['unitsentinel_operation']}",
                f"node_id = {node.node_id}",
            ),
            accent=VIOLET,
        ),
        _box(
            1060,
            365,
            310,
            210,
            title=output.value_id,
            lines=value_lines(output, role="canonical output"),
            accent=GREEN,
        ),
        _arrow(380, 312, 565, 420, label=str(left.unit_id)),
        _arrow(380, 622, 565, 520, label=str(right.unit_id)),
        _arrow(875, 470, 1060, 470, label=node.operation.value),
        _text(
            55,
            825,
            (
                f"model {_short_digest(str(receipt_model['sha256']))} · "
                f"graph {_short_digest(str(receipt_graph['sha256']))} · "
                f"receipt {_short_digest(receipt_digest)}"
            ),
            size=15,
            fill=MUTED,
        ),
    ]
    return _document(
        width=1_440,
        height=900,
        title="Lowered ONNX speed graph",
        description=(
            "Two explicitly annotated canonical inputs pass through the single "
            "receipt-bound operator to one canonical output."
        ),
        body="\n".join(body),
    )


def _rejection_svg(rejections: list[dict[str, object]]) -> str:
    body = [
        _text(
            55,
            65,
            "Unsupported ONNX semantics fail closed",
            size=31,
            weight=700,
        ),
        _text(
            55,
            103,
            "These diagnostics were captured from actual CLI invocations.",
            size=17,
            fill=MUTED,
        ),
    ]
    x_positions = (45, 505, 965)
    accents = (AMBER, RED, VIOLET)
    for x, accent, rejection in zip(
        x_positions,
        accents,
        rejections,
        strict=True,
    ):
        diagnostic = str(rejection["diagnostic"])
        wrapped = tuple(textwrap.wrap(diagnostic, width=38))
        lines = (
            f"exit {rejection['exit_code']}",
            f"{rejection['bytes']} source bytes",
            _short_digest(str(rejection["sha256"])),
            "",
            *wrapped,
        )
        body.append(
            _box(
                x,
                205,
                430,
                455,
                title=str(rejection["case"]),
                lines=lines,
                accent=accent,
            )
        )
    published = sum(record["graph_published"] is True for record in rejections)
    body.append(
        _box(
            180,
            740,
            1080,
            105,
            title="RECORDED BOUNDARY RESULT",
            lines=(
                f"{len(rejections)} CLI cases · {published} graphs published",
            ),
            accent=GREEN,
        )
    )
    cases = ", ".join(str(record["case"]) for record in rejections)
    return _document(
        width=1_440,
        height=900,
        title="ONNX fail-closed rejection matrix",
        description=f"Actual stable CLI failures for: {cases}.",
        body="\n".join(body),
    )


def _build_evidence() -> dict[Path, bytes]:
    model_payload = _serialize(_model())
    result = import_onnx_model(model_payload)
    verification = verify_graph(result.graph)
    if verification.status is not VerificationStatus.VERIFIED:
        raise EvidenceError("ONNX evidence graph did not verify")

    model_run = RUN_DIRECTORY / MODEL_NAME
    graph_run = RUN_DIRECTORY / GRAPH_NAME
    graph_json_run = RUN_DIRECTORY / "onnx-speed-json.graph.json"
    model_run.write_bytes(model_payload)
    model_argument = _relative_run(model_run)
    graph_argument = _relative_run(graph_run)
    graph_json_argument = _relative_run(graph_json_run)

    import_arguments = (
        "import-onnx",
        model_argument,
        "--graph",
        graph_argument,
    )
    import_output = _positive_cli(import_arguments)
    import_json_output = _positive_cli(
        (
            "import-onnx",
            model_argument,
            "--graph",
            graph_json_argument,
            "--json",
        )
    )
    graph_payload = graph_run.read_bytes()
    if graph_payload != graph_json_run.read_bytes():
        raise EvidenceError("text and JSON ONNX imports produced different graphs")
    graph = decode_graph(graph_payload)
    if graph.digest != result.graph.digest:
        raise EvidenceError("CLI and library ONNX graph digests disagree")

    verify_output = _positive_cli(("verify", graph_argument))
    verify_json_output = _positive_cli(("verify", graph_argument, "--json"))
    import_record = cast(dict[str, Any], json.loads(import_json_output))
    verify_record = cast(dict[str, Any], json.loads(verify_json_output))
    if import_record["import"]["sha256"] != result.digest:
        raise EvidenceError("CLI and library ONNX import digests disagree")
    receipt = cast(dict[str, Any], import_record["import"]["record"])
    if receipt != result.canonical_record():
        raise EvidenceError("CLI and library ONNX import receipts disagree")
    receipt_checker = cast(dict[str, Any], receipt["checker"])
    receipt_contract = cast(dict[str, Any], receipt["contract"])
    receipt_graph = cast(dict[str, Any], receipt["graph"])
    receipt_model = cast(dict[str, Any], receipt["model"])
    if (
        receipt_model["external_data"] is not False
        or receipt_model["model_executed"] is not False
    ):
        raise EvidenceError("ONNX evidence crossed its execution boundary")
    if verify_record["result"]["record"]["status"] != "verified":
        raise EvidenceError("recorded ONNX graph verification is not positive")

    import_transcript = _transcript(
        command_lines=(
            f"$ {PYTHON_DISPLAY} -m unitsentinel import-onnx \\",
            f"    {MODEL_RELATIVE} \\",
            f"    --graph {GRAPH_RELATIVE}",
        ),
        output=import_output,
        exit_code=0,
    )
    verify_transcript = _transcript(
        command_lines=(
            f"$ {PYTHON_DISPLAY} -m unitsentinel verify \\",
            f"    {GRAPH_RELATIVE}",
        ),
        output=verify_output,
        exit_code=0,
    )

    rejection_sources = (
        (
            "dynamic-shape",
            _dynamic_shape_payload(model_payload),
            "dimensions must be static",
        ),
        (
            "runtime-pow-tensor",
            _pow_payload(model_payload),
            "not in the reviewed subset",
        ),
        (
            "embedded-initializer",
            _initializer_payload(model_payload),
            "initializers and external tensor data",
        ),
    )
    rejections: list[dict[str, object]] = []
    rejection_transcript_parts: list[str] = []
    for case, payload, expected in rejection_sources:
        case_model = RUN_DIRECTORY / f"{case}.onnx"
        case_graph = RUN_DIRECTORY / f"{case}.graph.json"
        case_model.write_bytes(payload)
        diagnostic = _rejected_cli(
            (
                "import-onnx",
                _relative_run(case_model),
                "--graph",
                _relative_run(case_graph),
            )
        )
        if expected not in diagnostic or case_graph.exists():
            raise EvidenceError("ONNX rejection evidence does not match its case")
        record = {
            "bytes": len(payload),
            "case": case,
            "diagnostic": diagnostic,
            "exit_code": 4,
            "graph_published": False,
            "sha256": sha256_hex(payload),
        }
        rejections.append(record)
        rejection_transcript_parts.extend(
            (
                f"$ {PYTHON_DISPLAY} -m unitsentinel import-onnx \\",
                f"    <generated:{case}.onnx> \\",
                f"    --graph <new:{case}.graph.json>",
                diagnostic,
                "[exit 4]",
                "",
            )
        )
    rejection_document = {
        "cases": rejections,
        "schema": REJECTIONS_SCHEMA,
    }
    rejection_transcript = (
        "\n".join(rejection_transcript_parts).rstrip() + "\n"
    ).encode()

    receipt_digest = str(import_record["import"]["sha256"])
    architecture = _architecture_svg(
        receipt,
        receipt_digest=receipt_digest,
        rejections=rejections,
    ).encode()
    lowered = _lowered_graph_svg(
        graph,
        receipt,
        receipt_digest=receipt_digest,
    ).encode()
    rejection_visual = _rejection_svg(rejections).encode()
    terminal = terminal_svg(
        title="real ONNX import · canonical graph receipt",
        transcript=import_transcript.decode(),
        accent=GREEN,
        description=(
            "Actual UnitSentinel CLI import output for the committed synthetic "
            "ONNX speed contract."
        ),
    ).encode()

    files: dict[Path, bytes] = {
        EVIDENCE / "models" / MODEL_NAME: model_payload,
        EVIDENCE / "contracts" / GRAPH_NAME: graph_payload,
        EVIDENCE / "captures" / "onnx-import.txt": import_transcript,
        EVIDENCE / "captures" / "onnx-import.json": import_json_output,
        EVIDENCE / "captures" / "onnx-verify.txt": verify_transcript,
        EVIDENCE / "captures" / "onnx-verify.json": verify_json_output,
        EVIDENCE / "captures" / "onnx-rejections.txt": rejection_transcript,
        EVIDENCE / "captures" / "onnx-rejections.json": _canonical_bytes(
            rejection_document
        ),
        ASSETS / "onnx-adapter-architecture.svg": architecture,
        ASSETS / "onnx-lowered-graph.svg": lowered,
        ASSETS / "onnx-rejection-matrix.svg": rejection_visual,
        ASSETS / "onnx-import-terminal.svg": terminal,
    }
    demo_frames = (
        ("frame-import.svg", 2_600, terminal),
        ("frame-lowering.svg", 2_600, lowered),
        ("frame-rejections.svg", 2_800, rejection_visual),
    )
    demo_directory = EVIDENCE / "onnx-demo"
    for name, _, payload in demo_frames:
        files[demo_directory / name] = payload
    files[demo_directory / "frames.json"] = _canonical_bytes(
        {
            "frames": [
                {"delay_ms": delay, "path": name} for name, delay, _ in demo_frames
            ],
            "schema": FRAME_SCHEMA,
        }
    )

    receipt_opset = cast(dict[str, Any], receipt_model["opset"])
    provenance = {
        "checker": receipt_checker,
        "contract": receipt_contract,
        "execution_boundary": {
            "external_data": receipt_model["external_data"],
            "model_executed": receipt_model["model_executed"],
            "network_access_required": False,
        },
        "graph": {
            "bytes": len(graph_payload),
            "inputs": receipt_graph["inputs"],
            "nodes": receipt_graph["nodes"],
            "outputs": receipt_graph["outputs"],
            "schema": receipt_graph["schema"],
            "sha256": receipt_graph["sha256"],
            "values": receipt_graph["values"],
            "verification_result_sha256": verify_record["result"]["sha256"],
            "verification_status": "verified",
        },
        "import_receipt": {
            "schema": receipt["schema"],
            "sha256": receipt_digest,
        },
        "model": {
            "bytes": receipt_model["bytes"],
            "format": "ONNX ModelProto",
            "ir_version": receipt_model["ir_version"],
            "opset": receipt_opset["version"],
            "sha256": receipt_model["sha256"],
        },
        "rejections_sha256": sha256_hex(
            files[EVIDENCE / "captures" / "onnx-rejections.json"]
        ),
        "schema": PROVENANCE_SCHEMA,
        "tool": {"name": "unitsentinel", "version": VERSION},
    }
    files[EVIDENCE / "onnx-provenance.json"] = _canonical_bytes(provenance)
    return files


def _write_files(files: dict[Path, bytes]) -> None:
    for path, payload in sorted(files.items()):
        _atomic_write(path, payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record or verify genuine ONNX adapter evidence.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--record",
        action="store_true",
        help="write deterministic ONNX evidence",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="rebuild and compare deterministic ONNX evidence",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        with _managed_run_directory():
            files = _build_evidence()
        if arguments.check:
            _check_files(files)
        else:
            _write_files(files)
    except (EvidenceError, OSError, UnicodeError, ValueError) as error:
        sys.stderr.write(f"onnx evidence: error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
