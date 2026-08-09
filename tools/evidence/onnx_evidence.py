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
    OnnxImportResult,
    VerificationStatus,
    decode_graph,
    import_onnx_model,
    verify_graph,
)
from unitsentinel.canonical import sha256_hex
from unitsentinel.version import VERSION

PROVENANCE_SCHEMA: Final = "unitsentinel.onnx-evidence/v1"
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


def _architecture_svg(result: OnnxImportResult) -> str:
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
            "Every number below is derived from the committed synthetic ModelProto.",
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
                f"{result.source_size} bytes",
                _short_digest(result.source_digest),
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
                "onnx 1.22.0",
                "full_check = true",
                "custom domains checked",
                "no runtime session",
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
                "metadata v1",
                _short_digest(result.contract_digest),
                "3 explicit values",
                "1 explicit node",
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
                "IR 8 · opset 13",
                "Div → divide",
                "static float32 [4,8]",
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
                result.graph.graph_id,
                _short_digest(result.graph.digest),
                "canonical JSON",
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
            title="FAIL-CLOSED SAFETY RAIL",
            lines=(
                "Rejected before lowering: initializers · external data · dynamic "
                "shapes · attributes",
                "Rejected semantics: custom domains · functions · training graphs · "
                "control flow · Pow",
                "Never performed: model execution · network access · unit inference "
                "from names",
                f"receipt {_short_digest(result.digest)}",
            ),
            accent=RED,
        ),
    ]
    return _document(
        width=1_440,
        height=900,
        title="UnitSentinel ONNX adapter architecture",
        description=(
            "The exact bounded parsing, official checking, versioned metadata, "
            "closed-subset lowering, and canonical graph verification boundary."
        ),
        body="\n".join(body),
    )


def _lowered_graph_svg(result: OnnxImportResult) -> str:
    body = [
        _text(
            55,
            65,
            "One real ModelProto, one explicit dimensional graph",
            size=31,
            weight=700,
        ),
        _text(
            55,
            102,
            "Source names are mapped by metadata; units are never guessed.",
            size=17,
            fill=MUTED,
        ),
        _box(
            70,
            210,
            310,
            205,
            title="distance",
            lines=(
                "ONNX input · float32 [4,8]",
                "value_id = distance",
                "unit_id = meter",
                "length¹",
            ),
            accent=CYAN,
        ),
        _box(
            70,
            520,
            310,
            205,
            title="duration",
            lines=(
                "ONNX input · float32 [4,8]",
                "value_id = duration",
                "unit_id = second",
                "time¹",
            ),
            accent=CYAN,
        ),
        _box(
            565,
            365,
            310,
            210,
            title="derive-speed",
            lines=(
                "ONNX Div",
                "↓ explicit reviewed mapping",
                "UnitSentinel divide",
                "node_id = derive-speed",
            ),
            accent=VIOLET,
        ),
        _box(
            1060,
            365,
            310,
            210,
            title="speed",
            lines=(
                "ONNX output · float32 [4,8]",
                "value_id = speed",
                "unit_id = meter-per-second",
                "length¹ time⁻¹",
            ),
            accent=GREEN,
        ),
        _arrow(380, 312, 565, 420, label="meter"),
        _arrow(380, 622, 565, 520, label="second"),
        _arrow(875, 470, 1060, 470, label="divide"),
        _text(
            55,
            825,
            (
                f"model {_short_digest(result.source_digest)} · "
                f"graph {_short_digest(result.graph.digest)} · "
                f"receipt {_short_digest(result.digest)}"
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
            "Distance in meters divided by duration in seconds lowers to a "
            "canonical speed value in meters per second."
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
    body.extend(
        (
            _box(
                180,
                740,
                1080,
                105,
                title="BOUNDARY RESULT",
                lines=(
                    "0 graphs published · 0 models executed · 0 external tensors read",
                ),
                accent=GREEN,
            ),
        )
    )
    return _document(
        width=1_440,
        height=900,
        title="ONNX fail-closed rejection matrix",
        description=(
            "Actual stable CLI failures for a dynamic dimension, runtime Pow "
            "tensor semantics, and an embedded initializer."
        ),
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

    architecture = _architecture_svg(result).encode()
    lowered = _lowered_graph_svg(result).encode()
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
                {"delay_ms": delay, "path": name}
                for name, delay, _ in demo_frames
            ],
            "schema": FRAME_SCHEMA,
        }
    )

    provenance = {
        "checker": result.canonical_record()["checker"],
        "contract": {
            "metadata_key": ONNX_CONTRACT_METADATA_KEY,
            "schema": ONNX_CONTRACT_SCHEMA,
            "sha256": result.contract_digest,
        },
        "execution_boundary": {
            "external_data": False,
            "model_executed": False,
            "network_used": False,
        },
        "graph": {
            "bytes": len(graph_payload),
            "schema": "unitsentinel.graph/v1",
            "sha256": result.graph.digest,
            "verification_result_sha256": verify_record["result"]["sha256"],
            "verification_status": "verified",
        },
        "import_receipt": {
            "schema": result.canonical_record()["schema"],
            "sha256": result.digest,
        },
        "model": {
            "bytes": len(model_payload),
            "format": "ONNX ModelProto",
            "ir_version": 8,
            "opset": 13,
            "sha256": result.source_digest,
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
