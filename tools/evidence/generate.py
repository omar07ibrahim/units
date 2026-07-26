"""Record, render, and verify repository-backed UnitSentinel evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast

from examples.build_wheel_anomaly_contract import build_graph
from tools.evidence.visuals import (
    GREEN,
    RED,
    conflict_core_svg,
    contract_flow_svg,
    lineage_svg,
    scaling_svg,
    terminal_svg,
    workflow_svg,
)
from unitsentinel import (
    BUILTIN_REGISTRY,
    ComputationGraph,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
    create_certificate,
    decode_certificate,
    encode_graph,
    replay_certificate,
)
from unitsentinel.canonical import canonical_json_bytes
from unitsentinel.version import VERSION

ROOT: Final = Path(__file__).resolve().parents[2]
EVIDENCE: Final = ROOT / "docs" / "evidence"
ASSETS: Final = ROOT / "docs" / "assets"
RUN_DIRECTORY: Final = ROOT / ".unitsentinel" / "evidence-run"
BENCHMARK_PATH: Final = EVIDENCE / "data" / "scaling.json"
MANIFEST_PATH: Final = EVIDENCE / "manifest.json"
MANIFEST_SCHEMA: Final = "unitsentinel.evidence-manifest/v1"
PROVENANCE_SCHEMA: Final = "unitsentinel.evidence-provenance/v1"
BENCHMARK_SCHEMA: Final = "unitsentinel.scaling-benchmark/v1"
FRAME_SCHEMA: Final = "unitsentinel.demo-frames/v1"
BENCHMARK_SIZES: Final = (1, 8, 32, 128, 256)
BENCHMARK_REPETITIONS: Final = 3
PYTHON_DISPLAY: Final = ".venv/bin/python"
MAX_EVIDENCE_FILE_BYTES: Final = 134_217_728
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
TIMESTAMP_PATTERN: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")


class EvidenceError(RuntimeError):
    """Raised when recorded evidence would be incomplete or misleading."""


def _canonical_bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _ensure_safe_directory(path: Path, *, create: bool) -> None:
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        raise EvidenceError("evidence path escapes the repository") from None

    current = ROOT
    for component in relative.parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                raise EvidenceError(
                    f"evidence directory is missing: {_relative(current)}"
                ) from None
            try:
                current.mkdir(mode=0o755)
                metadata = current.lstat()
            except OSError:
                raise EvidenceError(
                    f"evidence directory could not be created: {_relative(current)}"
                ) from None
        except OSError:
            raise EvidenceError(
                f"evidence directory is unavailable: {_relative(current)}"
            ) from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise EvidenceError(f"evidence directory is unsafe: {_relative(current)}")


def _read_regular_file(path: Path, *, purpose: str) -> bytes:
    _ensure_safe_directory(path.parent, create=False)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 0
            or before.st_size > MAX_EVIDENCE_FILE_BYTES
        ):
            raise EvidenceError(f"{purpose} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise EvidenceError(f"{purpose} changed while it was read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise EvidenceError(f"{purpose} changed while it was read")
        after = os.fstat(descriptor)
        snapshot = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in snapshot):
            raise EvidenceError(f"{purpose} changed while it was read")
        return b"".join(chunks)
    except EvidenceError:
        raise
    except OSError:
        raise EvidenceError(f"{purpose} could not be read safely") from None
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    _ensure_safe_directory(path.parent, create=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise EvidenceError("evidence writer made no progress")
            written += count
        os.fsync(descriptor)
    except BaseException:
        with suppress(OSError):
            temporary.unlink()
        raise
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
    try:
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            temporary.unlink()
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def _managed_run_directory() -> Iterator[None]:
    root = ROOT / ".unitsentinel"
    try:
        if root.is_symlink():
            raise EvidenceError("evidence workspace cannot be a symlink")
        root.mkdir(mode=0o700, exist_ok=True)
        if not root.is_dir():
            raise EvidenceError("evidence workspace is not a directory")
        RUN_DIRECTORY.mkdir(mode=0o700)
    except FileExistsError:
        raise EvidenceError(
            "evidence run directory already exists; preserve it and retry manually"
        ) from None
    except OSError:
        raise EvidenceError(
            "evidence run directory could not be created safely"
        ) from None

    cleanup_error: OSError | None = None
    try:
        yield
    finally:
        try:
            shutil.rmtree(RUN_DIRECTORY)
        except OSError as error:
            cleanup_error = error
        with suppress(OSError):
            root.rmdir()
        if cleanup_error is not None:
            raise EvidenceError(
                "evidence run directory cleanup failed"
            ) from cleanup_error


def _run_cli(arguments: Sequence[str], *, expected_exit: int) -> bytes:
    environment = {
        "HOME": str(RUN_DIRECTORY),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(ROOT / "src"),
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "unitsentinel", *arguments],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceError("CLI evidence capture could not complete safely") from None
    if completed.returncode != expected_exit:
        raise EvidenceError(
            f"CLI returned {completed.returncode}, expected {expected_exit}"
        )
    if completed.stderr:
        raise EvidenceError("CLI wrote unexpected diagnostics during evidence capture")
    return completed.stdout


def _transcript(
    *,
    command_lines: tuple[str, ...],
    output: bytes,
    exit_code: int,
) -> bytes:
    decoded = output.decode("utf-8")
    if not decoded.endswith("\n"):
        raise EvidenceError("CLI capture must end with one transport newline")
    return (
        "\n".join(command_lines).encode("utf-8")
        + b"\n"
        + output
        + f"[exit {exit_code}]\n".encode()
    )


def _chain_graph(nodes: int) -> ComputationGraph:
    if nodes < 1 or nodes > 512:
        raise EvidenceError("benchmark node count is out of bounds")
    width = max(4, len(str(nodes)))
    value_ids = tuple(f"value-{index:0{width}d}" for index in range(nodes + 1))
    values = tuple(
        ValueSpec(
            value_id,
            ScalarType.FLOAT32,
            ("batch",),
            "meter" if index == 0 else None,
        )
        for index, value_id in enumerate(value_ids)
    )
    steps = tuple(
        Node(
            f"identity-{index:0{width}d}",
            Operation.IDENTITY,
            (value_ids[index - 1],),
            value_ids[index],
        )
        for index in range(1, nodes + 1)
    )
    return ComputationGraph(
        graph_id=f"identity-chain-{nodes:0{width}d}",
        values=values,
        inputs=(value_ids[0],),
        nodes=steps,
        outputs=(value_ids[-1],),
    )


def _milliseconds(start: int, end: int) -> float:
    return round((end - start) / 1_000_000, 6)


def record_benchmark() -> dict[str, object]:
    """Measure bounded issuance and strict replay without host identifiers."""

    warmup_graph = _chain_graph(1)
    warmup_certificate = create_certificate(warmup_graph)
    replay_certificate(
        warmup_certificate,
        warmup_graph,
        strict_toolchain=True,
    )

    rows: list[dict[str, object]] = []
    solver_version = warmup_certificate.result.solver_version
    for nodes in BENCHMARK_SIZES:
        graph = _chain_graph(nodes)
        verify_runs: list[float] = []
        replay_runs: list[float] = []
        certificate = None
        for _ in range(BENCHMARK_REPETITIONS):
            started = time.perf_counter_ns()
            certificate = create_certificate(graph)
            verified = time.perf_counter_ns()
            report = replay_certificate(
                certificate,
                graph,
                strict_toolchain=True,
            )
            replayed = time.perf_counter_ns()
            if report.status.value != "reproduced":
                raise EvidenceError("benchmark replay was not reproduced")
            verify_runs.append(_milliseconds(started, verified))
            replay_runs.append(_milliseconds(verified, replayed))
        if certificate is None:
            raise EvidenceError("benchmark did not issue a certificate")
        rows.append(
            {
                "certificate_bytes": len(certificate.canonical_bytes()),
                "constraints": len(certificate.constraints),
                "graph_bytes": len(graph.canonical_bytes()),
                "nodes": nodes,
                "replay_median_ms": round(statistics.median(replay_runs), 6),
                "replay_runs_ms": replay_runs,
                "verify_median_ms": round(statistics.median(verify_runs), 6),
                "verify_runs_ms": verify_runs,
            }
        )
    return {
        "environment": {
            "architecture": platform.machine().lower(),
            "python": platform.python_version(),
            "solver": solver_version,
            "system": platform.system().lower(),
            "unitsentinel": VERSION,
        },
        "recorded_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "repetitions": BENCHMARK_REPETITIONS,
        "rows": rows,
        "schema": BENCHMARK_SCHEMA,
    }


def _validate_benchmark(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise EvidenceError("benchmark document must be an object")
    benchmark = cast(dict[str, Any], value)
    if set(benchmark) != {
        "environment",
        "recorded_at_utc",
        "repetitions",
        "rows",
        "schema",
    }:
        raise EvidenceError("benchmark document fields are not closed")
    if benchmark["schema"] != BENCHMARK_SCHEMA:
        raise EvidenceError("benchmark schema is not supported")
    if benchmark["repetitions"] != BENCHMARK_REPETITIONS:
        raise EvidenceError("benchmark repetition count is stale")
    recorded_at = benchmark["recorded_at_utc"]
    if type(recorded_at) is not str or TIMESTAMP_PATTERN.fullmatch(recorded_at) is None:
        raise EvidenceError("benchmark timestamp is malformed")
    try:
        datetime.fromisoformat(recorded_at)
    except ValueError:
        raise EvidenceError("benchmark timestamp is malformed") from None
    environment = benchmark["environment"]
    if type(environment) is not dict or set(environment) != {
        "architecture",
        "python",
        "solver",
        "system",
        "unitsentinel",
    }:
        raise EvidenceError("benchmark environment fields are malformed")
    if any(
        type(environment[field]) is not str or not environment[field]
        for field in environment
    ):
        raise EvidenceError("benchmark environment values are malformed")
    if environment["unitsentinel"] != VERSION:
        raise EvidenceError("benchmark UnitSentinel version is stale")
    rows = benchmark["rows"]
    if type(rows) is not list or len(rows) != len(BENCHMARK_SIZES):
        raise EvidenceError("benchmark rows are incomplete")
    expected_fields = {
        "certificate_bytes",
        "constraints",
        "graph_bytes",
        "nodes",
        "replay_median_ms",
        "replay_runs_ms",
        "verify_median_ms",
        "verify_runs_ms",
    }
    for index, (row, nodes) in enumerate(zip(rows, BENCHMARK_SIZES, strict=True)):
        if type(row) is not dict or set(row) != expected_fields:
            raise EvidenceError(f"benchmark row {index} fields are malformed")
        if row["nodes"] != nodes:
            raise EvidenceError("benchmark node sequence is stale")
        for key in ("certificate_bytes", "constraints", "graph_bytes"):
            if type(row[key]) is not int or row[key] <= 0:
                raise EvidenceError("benchmark size or constraint count is malformed")
        for key in ("replay_runs_ms", "verify_runs_ms"):
            runs = row[key]
            if (
                type(runs) is not list
                or len(runs) != BENCHMARK_REPETITIONS
                or any(
                    type(item) not in {int, float}
                    or not math.isfinite(item)
                    or item <= 0
                    for item in runs
                )
            ):
                raise EvidenceError("benchmark timing runs are malformed")
            median_key = key.removesuffix("_runs_ms") + "_median_ms"
            expected_median = round(statistics.median(runs), 6)
            if row[median_key] != expected_median:
                raise EvidenceError("benchmark median does not match its runs")
        for key in ("replay_median_ms", "verify_median_ms"):
            if (
                type(row[key]) not in {int, float}
                or not math.isfinite(row[key])
                or row[key] <= 0
            ):
                raise EvidenceError("benchmark median is malformed")
    return benchmark


def _load_benchmark() -> dict[str, Any]:
    try:
        payload = _read_regular_file(
            BENCHMARK_PATH,
            purpose="recorded benchmark",
        )
        value = json.loads(payload)
    except (EvidenceError, UnicodeDecodeError, json.JSONDecodeError):
        raise EvidenceError(
            "recorded benchmark is missing or malformed; use --record-benchmark"
        ) from None
    if payload != _canonical_bytes(value):
        raise EvidenceError("recorded benchmark is not canonical")
    return _validate_benchmark(value)


def _require_recording_environment() -> None:
    expected = ROOT / ".venv"
    try:
        current = Path(sys.prefix).resolve()
        expected_resolved = expected.resolve(strict=True)
    except OSError:
        raise EvidenceError(
            "record evidence from the repository .venv/bin/python"
        ) from None
    if current != expected_resolved:
        raise EvidenceError("record evidence from the repository .venv/bin/python")


def _check_rendered_assets() -> None:
    node = shutil.which("node", path=os.defpath)
    if node is None:
        raise EvidenceError("Node.js is required to verify rendered evidence")
    try:
        completed = subprocess.run(
            [node, str(ROOT / "tools" / "evidence" / "render.mjs"), "--check"],
            cwd=ROOT,
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceError(
            "rendered evidence check could not complete safely"
        ) from None
    if completed.returncode != 0 or completed.stderr:
        raise EvidenceError(
            "rendered PNG or GIF evidence is missing, stale, or unverifiable"
        )


def _build_evidence(
    benchmark: dict[str, Any],
    *,
    prepare_inputs: bool,
) -> dict[Path, bytes]:
    verified_graph = build_graph("verified")
    conflict_graph = build_graph("conflict")
    graph_paths = {
        "verified": EVIDENCE / "contracts" / "wheel-anomaly-verified.json",
        "conflict": EVIDENCE / "contracts" / "wheel-anomaly-conflict.json",
    }
    graph_payloads = {
        graph_paths["verified"]: encode_graph(verified_graph),
        graph_paths["conflict"]: encode_graph(conflict_graph),
    }
    for path, payload in graph_payloads.items():
        if prepare_inputs:
            _atomic_write(path, payload)
        else:
            try:
                current = path.read_bytes()
            except OSError:
                raise EvidenceError(
                    f"stale evidence input: {_relative(path)}"
                ) from None
            if current != payload:
                raise EvidenceError(f"stale evidence input: {_relative(path)}")

    certificate_run_path = RUN_DIRECTORY / "wheel-anomaly.cert.json"
    conflict_certificate_path = RUN_DIRECTORY / "conflict.cert.json"
    verified_relative = _relative(graph_paths["verified"])
    conflict_relative = _relative(graph_paths["conflict"])
    certificate_relative = _relative(certificate_run_path)
    conflict_certificate_relative = _relative(conflict_certificate_path)
    python = PYTHON_DISPLAY

    verify_arguments = (
        "verify",
        verified_relative,
        "--certificate",
        certificate_relative,
    )
    verify_output = _run_cli(verify_arguments, expected_exit=0)
    if not certificate_run_path.is_file():
        raise EvidenceError("positive CLI capture did not write its certificate")
    certificate_bytes = certificate_run_path.read_bytes()
    certificate = decode_certificate(certificate_bytes)
    if certificate.result.graph_digest != verified_graph.digest:
        raise EvidenceError("captured certificate is bound to the wrong graph")
    if benchmark["environment"]["solver"] != certificate.result.solver_version:
        raise EvidenceError("benchmark solver version is stale")

    conflict_arguments = (
        "verify",
        conflict_relative,
        "--certificate",
        conflict_certificate_relative,
    )
    conflict_output = _run_cli(conflict_arguments, expected_exit=1)
    if conflict_certificate_path.exists():
        raise EvidenceError("conflict CLI capture wrote a positive certificate")

    replay_arguments = (
        "replay",
        certificate_relative,
        "--graph",
        verified_relative,
        "--expect-sha256",
        certificate.digest,
        "--strict-toolchain",
    )
    replay_output = _run_cli(replay_arguments, expected_exit=0)
    replay_report = replay_certificate(
        certificate,
        verified_graph,
        strict_toolchain=True,
    )
    if replay_report.status.value != "reproduced":
        raise EvidenceError("captured certificate did not reproduce")

    verify_json_output = _run_cli(
        ("verify", verified_relative, "--json"),
        expected_exit=0,
    )
    conflict_json_output = _run_cli(
        ("verify", conflict_relative, "--json"),
        expected_exit=1,
    )
    replay_json_output = _run_cli(
        (*replay_arguments, "--json"),
        expected_exit=0,
    )
    verify_record = cast(dict[str, Any], json.loads(verify_json_output))
    conflict_record = cast(dict[str, Any], json.loads(conflict_json_output))
    replay_record = cast(dict[str, Any], json.loads(replay_json_output))
    result_record = cast(dict[str, Any], verify_record["result"]["record"])
    conflict_result = cast(dict[str, Any], conflict_record["result"]["record"])
    replay_result = cast(dict[str, Any], replay_record["report"]["record"])
    if replay_record["report"]["sha256"] != replay_report.digest:
        raise EvidenceError("CLI and library replay digests disagree")

    verify_transcript = _transcript(
        command_lines=(
            f"$ {python} -m unitsentinel verify \\",
            f"    {verified_relative} \\",
            f"    --certificate {certificate_relative}",
        ),
        output=verify_output,
        exit_code=0,
    )
    conflict_transcript = _transcript(
        command_lines=(
            f"$ {python} -m unitsentinel verify \\",
            f"    {conflict_relative} \\",
            f"    --certificate {conflict_certificate_relative}",
        ),
        output=conflict_output,
        exit_code=1,
    )
    replay_transcript = _transcript(
        command_lines=(
            f"$ {python} -m unitsentinel replay \\",
            f"    {certificate_relative} \\",
            f"    --graph {verified_relative} \\",
            f"    --expect-sha256 {certificate.digest} \\",
            "    --strict-toolchain",
        ),
        output=replay_output,
        exit_code=0,
    )
    core_ids = [
        str(witness["constraint_id"])
        for witness in cast(list[dict[str, Any]], conflict_result["conflict_core"])
    ]

    files: dict[Path, bytes] = dict(graph_payloads)
    files.update(
        {
            EVIDENCE / "claims" / "wheel-anomaly.cert.json": certificate_bytes,
            EVIDENCE / "captures" / "verify.txt": verify_transcript,
            EVIDENCE / "captures" / "conflict.txt": conflict_transcript,
            EVIDENCE / "captures" / "replay.txt": replay_transcript,
            EVIDENCE / "captures" / "verify.json": verify_json_output,
            EVIDENCE / "captures" / "conflict.json": conflict_json_output,
            EVIDENCE / "captures" / "replay.json": replay_json_output,
            BENCHMARK_PATH: _canonical_bytes(benchmark),
            ASSETS / "verification-pipeline.svg": workflow_svg(
                graph_digest=verified_graph.digest,
                result_digest=certificate.result.digest,
            ).encode(),
            ASSETS / "wheel-anomaly-contract.svg": contract_flow_svg(
                cast(list[dict[str, Any]], result_record["contracts"])
            ).encode(),
            ASSETS / "conflict-core.svg": conflict_core_svg(
                core_ids=core_ids,
                graph_digest=conflict_graph.digest,
                result_digest=str(conflict_record["result"]["sha256"]),
            ).encode(),
            ASSETS / "certificate-lineage.svg": lineage_svg(
                certificate_bytes=len(certificate_bytes),
                graph_digest=verified_graph.digest,
                graph_bytes=len(verified_graph.canonical_bytes()),
                registry_digest=BUILTIN_REGISTRY.digest,
                registry_units=len(BUILTIN_REGISTRY.units),
                registry_version=BUILTIN_REGISTRY.version,
                result_digest=certificate.result.digest,
                contract_count=len(certificate.result.contracts),
                constraint_count=len(certificate.constraints),
                checks_performed=certificate.result.checks_performed,
                certificate_digest=certificate.digest,
                replay_digest=replay_report.digest,
                verifier_version=certificate.verifier_version,
                solver_version=certificate.result.solver_version,
            ).encode(),
            ASSETS / "scaling.svg": scaling_svg(benchmark).encode(),
            ASSETS / "verify-terminal.svg": terminal_svg(
                title="verified graph · positive certificate",
                transcript=verify_transcript.decode(),
                accent=GREEN,
                description=(
                    "Real UnitSentinel CLI output for the verified wheel anomaly "
                    "feature graph and certificate issuance."
                ),
            ).encode(),
            ASSETS / "conflict-terminal.svg": terminal_svg(
                title="serving contract bug · fail closed",
                transcript=conflict_transcript.decode(),
                accent=RED,
                description=(
                    "Real UnitSentinel CLI output for the shape-valid but "
                    "dimensionally conflicting wheel anomaly graph."
                ),
            ).encode(),
            ASSETS / "replay-terminal.svg": terminal_svg(
                title="detached certificate · strict semantic replay",
                transcript=replay_transcript.decode(),
                accent=GREEN,
                description=(
                    "Real UnitSentinel CLI output for strict replay of the "
                    "recorded unsigned certificate claim."
                ),
            ).encode(),
        }
    )

    demo_directory = EVIDENCE / "demo"
    demo_frames = (
        ("frame-01.svg", 2_400, files[ASSETS / "conflict-terminal.svg"]),
        ("frame-02.svg", 2_400, files[ASSETS / "verify-terminal.svg"]),
        ("frame-03.svg", 2_800, files[ASSETS / "replay-terminal.svg"]),
    )
    frame_manifest = {
        "frames": [{"delay_ms": delay, "path": name} for name, delay, _ in demo_frames],
        "schema": FRAME_SCHEMA,
    }
    for name, _, payload in demo_frames:
        files[demo_directory / name] = payload
    files[demo_directory / "frames.json"] = _canonical_bytes(frame_manifest)

    provenance = {
        "certificate": {
            "authentication": "not-provided",
            "bytes": len(certificate_bytes),
            "sha256": certificate.digest,
        },
        "conflict": {
            "checks_performed": conflict_result["checks_performed"],
            "core": core_ids,
            "core_minimal": conflict_result["core_minimal"],
            "graph_sha256": conflict_graph.digest,
            "result_sha256": conflict_record["result"]["sha256"],
            "status": conflict_result["status"],
        },
        "registry": {
            "sha256": BUILTIN_REGISTRY.digest,
            "version": BUILTIN_REGISTRY.version,
        },
        "replay": {
            "authentication": "not-established",
            "report_sha256": replay_report.digest,
            "status": replay_result["status"],
            "strict_toolchain": replay_result["strict_toolchain"],
        },
        "schema": PROVENANCE_SCHEMA,
        "verified": {
            "checks_performed": result_record["checks_performed"],
            "contracts": len(result_record["contracts"]),
            "graph_sha256": verified_graph.digest,
            "result_sha256": verify_record["result"]["sha256"],
            "status": result_record["status"],
        },
    }
    files[EVIDENCE / "provenance.json"] = _canonical_bytes(provenance)
    return files


def _write_files(files: dict[Path, bytes]) -> None:
    for path in sorted(files):
        _atomic_write(path, files[path])


def _check_files(files: dict[Path, bytes]) -> None:
    stale: list[str] = []
    for path, expected in sorted(files.items()):
        try:
            actual = _read_regular_file(
                path, purpose=f"evidence file {_relative(path)}"
            )
        except EvidenceError:
            stale.append(f"{_relative(path)} (missing)")
            continue
        if actual != expected:
            stale.append(f"{_relative(path)} (content differs)")
    if stale:
        raise EvidenceError("stale evidence:\n  " + "\n  ".join(stale))


def _evidence_files() -> list[Path]:
    files: list[Path] = []
    for directory in (EVIDENCE, ASSETS):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise EvidenceError(
                f"evidence directory is unavailable: {_relative(directory)}"
            ) from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise EvidenceError(f"evidence directory is unsafe: {_relative(directory)}")
        _ensure_safe_directory(directory, create=False)
        for path in directory.rglob("*"):
            try:
                entry = path.lstat()
            except OSError:
                raise EvidenceError(
                    f"evidence entry is unavailable: {_relative(path)}"
                ) from None
            if stat.S_ISLNK(entry.st_mode):
                raise EvidenceError(f"evidence entry is a symlink: {_relative(path)}")
            if stat.S_ISDIR(entry.st_mode):
                continue
            if not stat.S_ISREG(entry.st_mode):
                raise EvidenceError(
                    f"evidence entry is not a regular file: {_relative(path)}"
                )
            if path != MANIFEST_PATH:
                files.append(path)
    return sorted(files)


def write_manifest() -> None:
    _check_rendered_assets()
    files = _evidence_files()
    if not files:
        raise EvidenceError("cannot write an empty evidence manifest")
    records = []
    for path in files:
        payload = _read_regular_file(
            path,
            purpose=f"evidence file {_relative(path)}",
        )
        records.append(
            {
                "bytes": len(payload),
                "path": _relative(path),
                "sha256": _sha256(payload),
            }
        )
    _atomic_write(
        MANIFEST_PATH,
        _canonical_bytes({"files": records, "schema": MANIFEST_SCHEMA}),
    )


def check_manifest() -> None:
    try:
        manifest_payload = _read_regular_file(
            MANIFEST_PATH,
            purpose="evidence manifest",
        )
        document = json.loads(manifest_payload)
    except (EvidenceError, UnicodeDecodeError, json.JSONDecodeError):
        raise EvidenceError("evidence manifest is missing or malformed") from None
    if type(document) is not dict or set(document) != {"files", "schema"}:
        raise EvidenceError("evidence manifest fields are malformed")
    if manifest_payload != _canonical_bytes(document):
        raise EvidenceError("evidence manifest is not canonical")
    if document["schema"] != MANIFEST_SCHEMA or type(document["files"]) is not list:
        raise EvidenceError("evidence manifest schema is not supported")
    expected_paths = {_relative(path) for path in _evidence_files()}
    recorded_paths: list[str] = []
    for record in document["files"]:
        if type(record) is not dict or set(record) != {"bytes", "path", "sha256"}:
            raise EvidenceError("evidence manifest record is malformed")
        relative = record["path"]
        if type(relative) is not str or relative in recorded_paths:
            raise EvidenceError("evidence manifest path is malformed or repeated")
        relative_path = PurePosixPath(relative)
        if (
            relative_path.is_absolute()
            or relative_path.as_posix() != relative
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise EvidenceError("evidence manifest path escapes the repository")
        byte_count = record["bytes"]
        digest = record["sha256"]
        if type(byte_count) is not int or byte_count <= 0:
            raise EvidenceError("evidence manifest byte count is malformed")
        if type(digest) is not str or SHA256_PATTERN.fullmatch(digest) is None:
            raise EvidenceError("evidence manifest digest is malformed")
        path = ROOT / relative
        try:
            payload = _read_regular_file(
                path,
                purpose=f"manifest file {relative}",
            )
        except EvidenceError:
            raise EvidenceError(f"manifest file is missing: {relative}") from None
        if len(payload) != byte_count or _sha256(payload) != digest:
            raise EvidenceError(f"manifest digest mismatch: {relative}")
        recorded_paths.append(relative)
    if recorded_paths != sorted(recorded_paths):
        raise EvidenceError("evidence manifest records are not sorted")
    if set(recorded_paths) != expected_paths:
        raise EvidenceError("evidence manifest file set is stale")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record or verify genuine UnitSentinel portfolio evidence.",
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--record",
        action="store_true",
        help="refresh deterministic evidence using the committed benchmark",
    )
    modes.add_argument(
        "--record-benchmark",
        action="store_true",
        help="measure a new benchmark snapshot and refresh all evidence",
    )
    modes.add_argument(
        "--write-manifest",
        action="store_true",
        help="hash every current evidence and asset file",
    )
    modes.add_argument(
        "--check",
        action="store_true",
        help="reproduce deterministic evidence and verify the manifest",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.write_manifest:
            write_manifest()
            return 0
        if not arguments.check:
            _require_recording_environment()
        if arguments.record_benchmark:
            benchmark = record_benchmark()
        else:
            benchmark = _load_benchmark()
        with _managed_run_directory():
            files = _build_evidence(
                benchmark,
                prepare_inputs=not arguments.check,
            )
        if arguments.check:
            _check_files(files)
            check_manifest()
        else:
            _write_files(files)
    except EvidenceError as error:
        sys.stderr.write(f"evidence: error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
