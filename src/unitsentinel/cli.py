"""Deterministic verification, replay, comparison, and repair reporting."""

from __future__ import annotations

import argparse
import errno
import hmac
import os
import secrets
import stat
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from fractions import Fraction
from typing import Final, NoReturn, cast

from .canonical import canonical_json_bytes, sha256_hex
from .certificate import (
    CERTIFICATE_SCHEMA,
    MAX_CERTIFICATE_BYTES,
    CertificateDecodeError,
    CertificateError,
    ProofCertificate,
    _create_certificate_attempt,
    decode_certificate,
    encode_certificate,
)
from .comparison import (
    AUTHENTICATION_NOT_PROVIDED,
    COMPARISON_RESULT_SCHEMA,
    ComparisonError,
    ComparisonPolicy,
    ComparisonResult,
    ComparisonStatus,
    InterfaceSnapshot,
    compare_graphs,
)
from .comparison_codec import (
    MAX_COMPARISON_BYTES,
    ComparisonDecodeError,
    decode_comparison_plan,
)
from .comparison_contract import COMPARISON_SCHEMA, ComparisonPlan
from .comparison_result_codec import (
    ComparisonResultDecodeError,
    encode_comparison_result,
)
from .domain import UnitSentinelError
from .graph import GRAPH_SCHEMA, ComputationGraph
from .graph_codec import MAX_GRAPH_BYTES, GraphDecodeError, decode_graph, encode_graph
from .onnx_adapter import (
    MAX_ONNX_MODEL_BYTES,
    ONNX_CONTRACT_METADATA_KEY,
    ONNX_CONTRACT_SCHEMA,
    ONNX_IR_VERSION,
    ONNX_OPSET_VERSION,
    ONNX_RUNTIME_VERSION,
    OnnxAdapterError,
    OnnxDependencyError,
    OnnxImportResult,
    import_onnx_model,
)
from .registry import BUILTIN_REGISTRY, REGISTRY_SCHEMA, SHA256_HEX
from .repair import (
    MAX_REPAIR_CANDIDATES,
    MAX_REPAIR_SITES,
    MAX_REPAIR_TOTAL_TIMEOUT_MS,
    MAX_REPAIR_VERIFIER_CALLS,
    MAX_REPAIR_WORK_ITEMS,
    RepairError,
    RepairLimits,
    RepairStatus,
    UnitRepairResult,
    propose_unit_annotation_repair,
)
from .replay import (
    CertificateReplay,
    CertificateReplayError,
    ReplayStatus,
    replay_certificate,
)
from .verification import (
    MAX_CORE_SHRINK_CHECKS,
    MAX_SOLVER_MEMORY_MB,
    MAX_SOLVER_TIMEOUT_MS,
    MAX_TOTAL_TIMEOUT_MS,
    MAX_UNIQUENESS_CHECKS,
    SolverLimits,
    VerificationResult,
    VerificationStatus,
)
from .version import VERSION

VERIFY_OUTPUT_SCHEMA: Final = "unitsentinel.cli.verify/v1"
REPLAY_OUTPUT_SCHEMA: Final = "unitsentinel.cli.replay/v1"
COMPARE_OUTPUT_SCHEMA: Final = "unitsentinel.cli.compare/v1"
REPAIR_OUTPUT_SCHEMA: Final = "unitsentinel.cli.repair/v1"
IMPORT_ONNX_OUTPUT_SCHEMA: Final = "unitsentinel.cli.import-onnx/v1"

EXIT_SUCCESS: Final = 0
EXIT_CONFLICT: Final = 1
EXIT_UNDERCONSTRAINED: Final = 2
EXIT_INDETERMINATE: Final = 3
EXIT_INPUT: Final = 4
EXIT_MISMATCH: Final = 5
EXIT_ABSTAINED: Final = 6
EXIT_USAGE: Final = 64
EXIT_INTERNAL: Final = 70
EXIT_INTERRUPTED: Final = 130

_READ_CHUNK_BYTES: Final = 65_536
_TEMP_ATTEMPTS: Final = 32
_DEFAULT_REPAIR_LIMITS: Final = RepairLimits()
_DEFAULT_SOLVER_LIMITS: Final = SolverLimits()


class _CLIError(Exception):
    """One bounded user-facing CLI failure."""

    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _CLIError(
            EXIT_USAGE,
            "invalid command-line arguments; use --help",
        )


def _sha256_argument(value: str) -> str:
    if SHA256_HEX.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("expected a lowercase SHA-256 digest")
    return value


def _bounded_integer_argument(
    minimum: int,
    maximum: int,
) -> Callable[[str], int]:
    def parse(value: str) -> int:
        if (
            len(value) > len(str(maximum))
            or not value.isascii()
            or not value.isdecimal()
        ):
            raise argparse.ArgumentTypeError("expected a bounded integer")
        parsed = int(value)
        if value != str(parsed) or parsed < minimum or parsed > maximum:
            raise argparse.ArgumentTypeError("expected a bounded integer")
        return parsed

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="unitsentinel",
        description=(
            "Verify exact dimensional contracts, import checked ONNX metadata, "
            "replay detached proof claims, compare training and serving graphs, "
            "and report bounded repair proposals."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser(
        "verify",
        help="verify one canonical computation graph",
    )
    verify.add_argument("graph", help="canonical graph JSON file")
    verify.add_argument(
        "--certificate",
        dest="certificate_output",
        metavar="FILE",
        help="atomically write a new positive certificate",
    )
    verify.add_argument(
        "--json",
        action="store_true",
        help="emit one canonical machine-readable record",
    )

    replay = commands.add_parser(
        "replay",
        help="replay one certificate against its graph",
    )
    replay.add_argument("certificate", help="canonical certificate JSON file")
    replay.add_argument(
        "--graph",
        required=True,
        help="canonical graph JSON file",
    )
    replay.add_argument(
        "--expect-sha256",
        type=_sha256_argument,
        metavar="DIGEST",
        help="reject the certificate before replay unless its digest matches",
    )
    replay.add_argument(
        "--strict-toolchain",
        action="store_true",
        help="require current verifier and solver versions to match the claim",
    )
    replay.add_argument(
        "--json",
        action="store_true",
        help="emit one canonical machine-readable record",
    )

    compare = commands.add_parser(
        "compare",
        help="compare canonical training and serving graph contracts",
    )
    compare.add_argument("plan", help="canonical comparison plan JSON file")
    compare.add_argument(
        "--training-graph",
        required=True,
        help="canonical training graph JSON file",
    )
    compare.add_argument(
        "--serving-graph",
        required=True,
        help="canonical serving graph JSON file",
    )
    compare.add_argument(
        "--expect-plan-sha256",
        required=True,
        type=_sha256_argument,
        metavar="DIGEST",
        help="reject the plan bytes before decoding unless this digest matches",
    )
    compare.add_argument(
        "--result",
        dest="result_output",
        metavar="FILE",
        help="atomically write a new canonical comparison-result claim",
    )
    compare.add_argument(
        "--per-check-timeout-ms",
        type=_bounded_integer_argument(1, MAX_SOLVER_TIMEOUT_MS),
        default=_DEFAULT_SOLVER_LIMITS.per_check_timeout_ms,
        metavar="MILLISECONDS",
        help=f"bound each solver check (1..{MAX_SOLVER_TIMEOUT_MS})",
    )
    compare.add_argument(
        "--total-timeout-ms",
        type=_bounded_integer_argument(1, MAX_TOTAL_TIMEOUT_MS),
        default=_DEFAULT_SOLVER_LIMITS.total_timeout_ms,
        metavar="MILLISECONDS",
        help=(
            "bound each graph-side verification; two sides may use twice this "
            f"budget (1..{MAX_TOTAL_TIMEOUT_MS})"
        ),
    )
    compare.add_argument(
        "--max-memory-mb",
        type=_bounded_integer_argument(32, MAX_SOLVER_MEMORY_MB),
        default=_DEFAULT_SOLVER_LIMITS.max_memory_mb,
        metavar="MEBIBYTES",
        help=f"bound solver memory per graph side (32..{MAX_SOLVER_MEMORY_MB})",
    )
    compare.add_argument(
        "--max-core-shrink-checks",
        type=_bounded_integer_argument(0, MAX_CORE_SHRINK_CHECKS),
        default=_DEFAULT_SOLVER_LIMITS.max_core_shrink_checks,
        metavar="COUNT",
        help=f"perform at most COUNT core-shrink checks (0..{MAX_CORE_SHRINK_CHECKS})",
    )
    compare.add_argument(
        "--max-uniqueness-checks",
        type=_bounded_integer_argument(1, MAX_UNIQUENESS_CHECKS),
        default=_DEFAULT_SOLVER_LIMITS.max_uniqueness_checks,
        metavar="COUNT",
        help=f"perform at most COUNT uniqueness checks (1..{MAX_UNIQUENESS_CHECKS})",
    )
    compare.add_argument(
        "--json",
        action="store_true",
        help="emit one canonical machine-readable record",
    )

    import_onnx = commands.add_parser(
        "import-onnx",
        help="lower one checked static ONNX metadata contract",
    )
    import_onnx.add_argument("model", help="bounded ONNX ModelProto file")
    import_onnx.add_argument(
        "--graph",
        dest="graph_output",
        required=True,
        metavar="FILE",
        help="atomically write a new canonical graph",
    )
    import_onnx.add_argument(
        "--json",
        action="store_true",
        help="emit one canonical machine-readable import receipt",
    )

    repair = commands.add_parser(
        "repair",
        help="report one bounded unit-annotation repair search",
    )
    repair.add_argument("graph", help="canonical graph JSON file")
    repair.add_argument(
        "--max-sites",
        type=_bounded_integer_argument(1, MAX_REPAIR_SITES),
        default=_DEFAULT_REPAIR_LIMITS.max_sites,
        metavar="COUNT",
        help=f"consider at most COUNT repair sites (1..{MAX_REPAIR_SITES})",
    )
    repair.add_argument(
        "--max-candidates",
        type=_bounded_integer_argument(1, MAX_REPAIR_CANDIDATES),
        default=_DEFAULT_REPAIR_LIMITS.max_candidates,
        metavar="COUNT",
        help=(
            f"consider at most COUNT semantic candidates (1..{MAX_REPAIR_CANDIDATES})"
        ),
    )
    repair.add_argument(
        "--max-verifier-calls",
        type=_bounded_integer_argument(1, MAX_REPAIR_VERIFIER_CALLS),
        default=_DEFAULT_REPAIR_LIMITS.max_verifier_calls,
        metavar="COUNT",
        help=(f"perform at most COUNT verifier calls (1..{MAX_REPAIR_VERIFIER_CALLS})"),
    )
    repair.add_argument(
        "--max-work-items",
        type=_bounded_integer_argument(1, MAX_REPAIR_WORK_ITEMS),
        default=_DEFAULT_REPAIR_LIMITS.max_work_items,
        metavar="COUNT",
        help=f"reserve at most COUNT work items (1..{MAX_REPAIR_WORK_ITEMS})",
    )
    repair.add_argument(
        "--total-timeout-ms",
        type=_bounded_integer_argument(1, MAX_REPAIR_TOTAL_TIMEOUT_MS),
        default=_DEFAULT_REPAIR_LIMITS.total_timeout_ms,
        metavar="MILLISECONDS",
        help=(
            "bound the complete search in milliseconds "
            f"(1..{MAX_REPAIR_TOTAL_TIMEOUT_MS})"
        ),
    )
    return parser


def _open_input_descriptor(path: str, *, label: str) -> int:
    parent, name = os.path.split(path)
    if not name or name in {".", ".."}:
        raise _CLIError(EXIT_INPUT, f"{label} could not be opened")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory = os.open(parent or ".", directory_flags)
    except OSError:
        raise _CLIError(EXIT_INPUT, f"{label} could not be opened") from None

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        try:
            return os.open(name, flags, dir_fd=directory)
        except OSError:
            raise _CLIError(EXIT_INPUT, f"{label} could not be opened") from None
    finally:
        with suppress(OSError):
            os.close(directory)


def _read_bounded_file(path: str, *, label: str, max_bytes: int) -> bytes:
    descriptor = _open_input_descriptor(path, label=label)

    try:
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise _CLIError(
                    EXIT_INPUT,
                    f"{label} must be a regular file",
                )
            if metadata.st_size > max_bytes:
                raise _CLIError(
                    EXIT_INPUT,
                    f"{label} exceeds the byte limit",
                )

            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                chunk = os.read(
                    descriptor,
                    min(_READ_CHUNK_BYTES, max_bytes + 1 - total),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > max_bytes:
                raise _CLIError(
                    EXIT_INPUT,
                    f"{label} exceeds the byte limit",
                )
            return b"".join(chunks)
        except _CLIError:
            raise
        except OSError:
            raise _CLIError(EXIT_INPUT, f"{label} could not be read") from None
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError(errno.EIO, "short write")
        written += count


def _open_output_directory(
    path: str,
    *,
    label: str = "certificate output",
) -> tuple[int, str]:
    parent, name = os.path.split(path)
    if not name or name in {".", ".."}:
        raise _CLIError(EXIT_INPUT, f"{label} path is invalid")
    directory = parent or "."
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        raise _CLIError(
            EXIT_INPUT,
            f"{label} directory could not be opened",
        ) from None
    return descriptor, name


def _create_output_temp(
    directory: int,
    *,
    label: str = "certificate output",
) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _ in range(_TEMP_ATTEMPTS):
        name = f".unitsentinel-{secrets.token_hex(12)}.tmp"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory), name
        except FileExistsError:
            continue
        except OSError:
            raise _CLIError(
                EXIT_INPUT,
                f"{label} could not be created",
            ) from None
    raise _CLIError(
        EXIT_INPUT,
        f"{label} could not be created",
    )


def _unlink_temp(directory: int, name: str | None) -> bool:
    if name is None:
        return True
    try:
        os.unlink(name, dir_fd=directory)
    except OSError:
        return False
    return True


def _atomic_write_new(
    path: str,
    payload: bytes,
    *,
    label: str = "certificate output",
) -> None:
    directory, target = _open_output_directory(path, label=label)
    temporary: str | None = None
    descriptor = -1
    try:
        descriptor, temporary = _create_output_temp(directory, label=label)
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        except OSError:
            raise _CLIError(
                EXIT_INPUT,
                f"{label} could not be written",
            ) from None
        finally:
            with suppress(OSError):
                os.close(descriptor)
            descriptor = -1

        try:
            os.link(
                temporary,
                target,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise _CLIError(
                EXIT_INPUT,
                f"{label} already exists",
            ) from None
        except OSError:
            raise _CLIError(
                EXIT_INPUT,
                f"{label} could not be published",
            ) from None

        if not _unlink_temp(directory, temporary):
            raise _CLIError(
                EXIT_INPUT,
                f"{label} cleanup could not be confirmed",
            )
        temporary = None
        try:
            os.fsync(directory)
        except OSError:
            raise _CLIError(
                EXIT_INPUT,
                f"{label} durability could not be confirmed",
            ) from None
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        _unlink_temp(directory, temporary)
        with suppress(OSError):
            os.close(directory)


def _decode_graph_file(
    path: str,
    *,
    label: str = "graph input",
    expected_digest: str | None = None,
) -> ComputationGraph:
    payload = _read_bounded_file(
        path,
        label=label,
        max_bytes=MAX_GRAPH_BYTES,
    )
    actual_digest = sha256_hex(payload)
    if expected_digest is not None and not hmac.compare_digest(
        actual_digest,
        expected_digest,
    ):
        raise _CLIError(
            EXIT_INPUT,
            f"{label} sha256 does not match the comparison plan",
        )
    try:
        graph = decode_graph(payload)
    except GraphDecodeError as error:
        raise _CLIError(
            EXIT_INPUT,
            f"{label} is invalid: {error}",
        ) from None
    if not hmac.compare_digest(graph.digest, actual_digest):
        raise _CLIError(
            EXIT_INTERNAL,
            f"{label} digest could not be confirmed",
        )
    return graph


def _decode_comparison_plan_file(
    path: str,
    *,
    expected_digest: str,
) -> ComparisonPlan:
    payload = _read_bounded_file(
        path,
        label="comparison plan input",
        max_bytes=MAX_COMPARISON_BYTES,
    )
    actual_digest = sha256_hex(payload)
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise _CLIError(
            EXIT_MISMATCH,
            "comparison plan sha256 does not match the expected digest",
        )
    try:
        plan = decode_comparison_plan(payload)
    except ComparisonDecodeError as error:
        raise _CLIError(
            EXIT_INPUT,
            f"comparison plan input is invalid: {error}",
        ) from None
    if not hmac.compare_digest(plan.digest, actual_digest):
        raise _CLIError(
            EXIT_INTERNAL,
            "comparison plan digest could not be confirmed",
        )
    return plan


def _decode_certificate_file(path: str) -> ProofCertificate:
    payload = _read_bounded_file(
        path,
        label="certificate input",
        max_bytes=MAX_CERTIFICATE_BYTES,
    )
    try:
        return decode_certificate(payload)
    except CertificateDecodeError as error:
        raise _CLIError(
            EXIT_INPUT,
            f"certificate input is invalid: {error}",
        ) from None


def _fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _dimension_text(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return "dimensionless"
    return " ".join(f"{base}^{exponent}" for base, exponent in pairs)


def _verification_text(
    graph: ComputationGraph,
    result: VerificationResult,
    certificate: ProofCertificate | None,
    *,
    certificate_requested: bool,
    certificate_written: bool,
    exit_code: int,
) -> str:
    lines = [
        f"UnitSentinel verification: {result.status.value.upper()}",
        f"exit code: {exit_code}",
        f"tool: unitsentinel {VERSION}",
        f"graph id: {graph.graph_id}",
        f"graph sha256: {result.graph_digest}",
        f"result sha256: {result.digest}",
        f"registry version: {BUILTIN_REGISTRY.version}",
        f"registry sha256: {result.registry_digest}",
        f"solver: z3 {result.solver_version} ({result.checks_performed} checks)",
    ]
    if result.status is VerificationStatus.VERIFIED:
        lines.append(f"contracts ({len(result.contracts)}):")
        for contract in result.contracts:
            lines.append(
                "  "
                f"{contract.value_id} | "
                f"{_dimension_text(contract.dimension.canonical_pairs())} | "
                f"{contract.kind.value} | "
                f"scale={_fraction_text(contract.scale)} "
                f"offset={_fraction_text(contract.offset)}"
            )
    elif result.status is VerificationStatus.UNDERCONSTRAINED:
        lines.append(
            "underconstrained values: " + ", ".join(result.underconstrained_values)
        )
    elif result.status is VerificationStatus.CONFLICT:
        minimal = "yes" if result.core_minimal else "no"
        lines.append(f"conflict core ({len(result.conflict_core)}, minimal={minimal}):")
        for witness in result.conflict_core:
            lines.append(
                "  "
                f"{witness.constraint_id} | "
                f"{witness.source.value}:{witness.source_id} | "
                f"{witness.rule}"
            )
    else:
        reason = result.unknown_reason
        lines.append(
            "unknown reason: " + ("unknown" if reason is None else reason.value)
        )
    if certificate is not None:
        lines.append(f"certificate sha256: {certificate.digest}")
        lines.append("certificate authentication: not-provided")
    else:
        lines.append("certificate: not-issued")
    if certificate_written:
        certificate_output = "written"
    elif certificate_requested and certificate is None:
        certificate_output = "not-issued"
    else:
        certificate_output = "not-requested"
    lines.append(f"certificate output: {certificate_output}")
    return "\n".join(lines) + "\n"


def _verification_json(
    graph: ComputationGraph,
    result: VerificationResult,
    certificate: ProofCertificate | None,
    *,
    certificate_requested: bool,
    certificate_written: bool,
    exit_code: int,
) -> str:
    certificate_record: dict[str, str] | None = None
    if certificate is not None:
        certificate_record = {
            "authentication": "not-provided",
            "schema": CERTIFICATE_SCHEMA,
            "sha256": certificate.digest,
        }
    if certificate_written:
        certificate_output = "written"
    elif certificate_requested and certificate is None:
        certificate_output = "not-issued"
    else:
        certificate_output = "not-requested"
    record = {
        "certificate": certificate_record,
        "certificate_output": certificate_output,
        "exit_code": exit_code,
        "graph": {
            "graph_id": graph.graph_id,
            "schema": GRAPH_SCHEMA,
            "sha256": graph.digest,
        },
        "registry": {
            "schema": REGISTRY_SCHEMA,
            "sha256": result.registry_digest,
            "version": BUILTIN_REGISTRY.version,
        },
        "result": {
            "record": result.canonical_record(),
            "sha256": result.digest,
        },
        "schema": VERIFY_OUTPUT_SCHEMA,
        "tool": {"name": "unitsentinel", "version": VERSION},
    }
    return canonical_json_bytes(record).decode("utf-8") + "\n"


def _replay_text(
    graph: ComputationGraph,
    report: CertificateReplay,
    *,
    exit_code: int,
) -> str:
    reason = "none" if report.reason is None else report.reason.value
    lines = [
        f"UnitSentinel replay: {report.status.value.upper()}",
        f"exit code: {exit_code}",
        f"tool: unitsentinel {VERSION}",
        f"reason: {reason}",
        f"certificate sha256: {report.certificate_digest}",
        "certificate authentication: not-provided",
        f"graph id: {graph.graph_id}",
        f"graph sha256: {report.graph_digest}",
        f"registry version: {report.registry_version}",
        f"registry sha256: {report.registry_digest}",
        f"replay sha256: {report.digest}",
        (
            "toolchain match: "
            + ("yes" if report.toolchain_match else "no")
            + f" (strict={'yes' if report.strict_toolchain else 'no'})"
        ),
        (
            "certificate toolchain: "
            f"unitsentinel {report.certificate_verifier_version}, "
            f"z3 {report.certificate_solver_version}"
        ),
        (
            "current toolchain: "
            f"unitsentinel {report.current_verifier_version}, "
            f"z3 {report.current_solver_version}"
        ),
    ]
    if report.fresh_result is not None:
        lines.append(
            "fresh result: "
            f"{report.fresh_result.status.value} "
            f"{report.fresh_result.digest}"
        )
    return "\n".join(lines) + "\n"


def _replay_json(
    certificate: ProofCertificate,
    graph: ComputationGraph,
    report: CertificateReplay,
    *,
    exit_code: int,
) -> str:
    record = {
        "certificate": {
            "authentication": "not-provided",
            "schema": CERTIFICATE_SCHEMA,
            "sha256": certificate.digest,
        },
        "exit_code": exit_code,
        "graph": {
            "graph_id": graph.graph_id,
            "schema": GRAPH_SCHEMA,
            "sha256": graph.digest,
        },
        "report": {
            "record": report.canonical_record(),
            "sha256": report.digest,
        },
        "schema": REPLAY_OUTPUT_SCHEMA,
        "tool": {"name": "unitsentinel", "version": VERSION},
    }
    return canonical_json_bytes(record).decode("utf-8") + "\n"


def _comparison_snapshot_text(snapshot: InterfaceSnapshot | None) -> str:
    if snapshot is None:
        return "absent"
    return (
        f"{snapshot.endpoint.role.value}:{snapshot.endpoint.value_id}"
        f"@{snapshot.position}"
    )


def _comparison_text(
    plan: ComparisonPlan,
    training_graph: ComputationGraph,
    serving_graph: ComputationGraph,
    result: ComparisonResult,
    *,
    expected_plan_digest: str,
    result_written: bool,
    exit_code: int,
) -> str:
    reason = "none" if result.reason is None else result.reason.value
    lines = [
        f"UnitSentinel comparison: {result.status.value.upper()}",
        f"exit code: {exit_code}",
        f"tool: unitsentinel {VERSION}",
        f"reason: {reason}",
        f"result scope: {result.scope}",
        f"comparison id: {plan.comparison_id}",
        f"plan sha256: {plan.digest}",
        f"expected plan sha256: {expected_plan_digest}",
        f"plan authentication: {AUTHENTICATION_NOT_PROVIDED}",
        f"training graph id: {training_graph.graph_id}",
        f"training graph sha256: {training_graph.digest}",
        f"serving graph id: {serving_graph.graph_id}",
        f"serving graph sha256: {serving_graph.digest}",
        f"registry version: {BUILTIN_REGISTRY.version}",
        f"registry sha256: {result.registry_digest}",
        (
            "solver limits per graph side: "
            f"check={result.limits.per_check_timeout_ms}ms "
            f"total={result.limits.total_timeout_ms}ms "
            f"memory={result.limits.max_memory_mb}MiB "
            f"core-shrink={result.limits.max_core_shrink_checks} "
            f"uniqueness={result.limits.max_uniqueness_checks}"
        ),
        f"result sha256: {result.digest}",
        f"result authentication: {result.authentication}",
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
            f"{_comparison_snapshot_text(comparison.training)} -> "
            f"{_comparison_snapshot_text(comparison.serving)} | "
            f"{mismatch_text}"
        )
        if comparison.normalization is not None:
            line += (
                " | normalization="
                f"{comparison.normalization.training_digest}->"
                f"{comparison.normalization.serving_digest}"
            )
        lines.append(line)
    output_state = "written" if result_written else "not-requested"
    lines.append(f"comparison result output: {output_state}")
    return "\n".join(lines) + "\n"


def _comparison_json(
    plan: ComparisonPlan,
    training_graph: ComputationGraph,
    serving_graph: ComputationGraph,
    result: ComparisonResult,
    *,
    expected_plan_digest: str,
    result_written: bool,
    exit_code: int,
) -> str:
    record = {
        "exit_code": exit_code,
        "graphs": {
            "serving": {
                "graph_id": serving_graph.graph_id,
                "schema": GRAPH_SCHEMA,
                "sha256": serving_graph.digest,
            },
            "training": {
                "graph_id": training_graph.graph_id,
                "schema": GRAPH_SCHEMA,
                "sha256": training_graph.digest,
            },
        },
        "plan": {
            "authentication": AUTHENTICATION_NOT_PROVIDED,
            "expected_sha256": expected_plan_digest,
            "schema": COMPARISON_SCHEMA,
            "sha256": plan.digest,
        },
        "registry": {
            "schema": REGISTRY_SCHEMA,
            "sha256": result.registry_digest,
            "version": BUILTIN_REGISTRY.version,
        },
        "result": {
            "authentication": result.authentication,
            "record": result.canonical_record(),
            "schema": COMPARISON_RESULT_SCHEMA,
            "sha256": result.digest,
        },
        "result_output": "written" if result_written else "not-requested",
        "schema": COMPARE_OUTPUT_SCHEMA,
        "tool": {"name": "unitsentinel", "version": VERSION},
    }
    return canonical_json_bytes(record).decode("utf-8") + "\n"


def _repair_json(
    graph: ComputationGraph,
    report: UnitRepairResult,
    *,
    exit_code: int,
) -> str:
    source_verification: dict[str, object] | None = None
    if report.source_verification is not None:
        source_verification = {
            "record": report.source_verification.canonical_record(),
            "sha256": report.source_verification.digest,
        }
    proposal: dict[str, object] | None = None
    if report.candidate is not None:
        candidate = report.candidate
        proposal = {
            "candidate_sha256": candidate.digest,
            "relaxed_graph": {
                "record": candidate.relaxed_graph.canonical_record(),
                "sha256": candidate.relaxed_graph.digest,
            },
            "relaxed_verification": {
                "record": candidate.relaxed_verification.canonical_record(),
                "sha256": candidate.relaxed_verification.digest,
            },
            "repaired_graph": {
                "record": candidate.repaired_graph.canonical_record(),
                "sha256": candidate.repaired_graph.digest,
            },
            "repaired_verification": {
                "record": candidate.repaired_verification.canonical_record(),
                "sha256": candidate.repaired_verification.digest,
            },
        }
    record = {
        "application": "not-performed",
        "exit_code": exit_code,
        "graph": {
            "graph_id": graph.graph_id,
            "schema": GRAPH_SCHEMA,
            "sha256": graph.digest,
        },
        "proposal": proposal,
        "registry": {
            "schema": REGISTRY_SCHEMA,
            "sha256": report.registry.digest,
            "version": report.registry.version,
        },
        "report": {
            "record": report.canonical_record(),
            "sha256": report.digest,
        },
        "schema": REPAIR_OUTPUT_SCHEMA,
        "source_verification": source_verification,
        "tool": {"name": "unitsentinel", "version": VERSION},
    }
    return canonical_json_bytes(record).decode("utf-8") + "\n"


def _verification_exit(status: VerificationStatus) -> int:
    return {
        VerificationStatus.VERIFIED: EXIT_SUCCESS,
        VerificationStatus.CONFLICT: EXIT_CONFLICT,
        VerificationStatus.UNDERCONSTRAINED: EXIT_UNDERCONSTRAINED,
        VerificationStatus.UNKNOWN: EXIT_INDETERMINATE,
    }[status]


def _replay_exit(status: ReplayStatus) -> int:
    return {
        ReplayStatus.REPRODUCED: EXIT_SUCCESS,
        ReplayStatus.MISMATCH: EXIT_MISMATCH,
        ReplayStatus.INDETERMINATE: EXIT_INDETERMINATE,
    }[status]


def _comparison_exit(status: ComparisonStatus) -> int:
    return {
        ComparisonStatus.COMPATIBLE: EXIT_SUCCESS,
        ComparisonStatus.DRIFT: EXIT_MISMATCH,
        ComparisonStatus.INDETERMINATE: EXIT_INDETERMINATE,
    }[status]


def _repair_exit(status: RepairStatus) -> int:
    return {
        RepairStatus.PROPOSED: EXIT_SUCCESS,
        RepairStatus.ABSTAINED: EXIT_ABSTAINED,
        RepairStatus.INDETERMINATE: EXIT_INDETERMINATE,
    }[status]


def _run_verify(arguments: argparse.Namespace) -> int:
    graph_path = cast(str, arguments.graph)
    graph = _decode_graph_file(graph_path)
    try:
        result, certificate = _create_certificate_attempt(graph)
    except CertificateError:
        raise _CLIError(
            EXIT_INTERNAL,
            "verification could not be completed safely",
        ) from None

    output_path = cast(str | None, arguments.certificate_output)
    certificate_requested = output_path is not None
    certificate_written = False
    if output_path is not None and certificate is not None:
        _atomic_write_new(output_path, encode_certificate(certificate))
        certificate_written = True

    machine_readable = cast(bool, arguments.json)
    exit_code = _verification_exit(result.status)
    if machine_readable:
        sys.stdout.write(
            _verification_json(
                graph,
                result,
                certificate,
                certificate_requested=certificate_requested,
                certificate_written=certificate_written,
                exit_code=exit_code,
            )
        )
    else:
        sys.stdout.write(
            _verification_text(
                graph,
                result,
                certificate,
                certificate_requested=certificate_requested,
                certificate_written=certificate_written,
                exit_code=exit_code,
            )
        )
    return exit_code


def _run_replay(arguments: argparse.Namespace) -> int:
    certificate_path = cast(str, arguments.certificate)
    certificate = _decode_certificate_file(certificate_path)
    expected_digest = cast(str | None, arguments.expect_sha256)
    if expected_digest is not None and not hmac.compare_digest(
        certificate.digest,
        expected_digest,
    ):
        raise _CLIError(
            EXIT_MISMATCH,
            "certificate sha256 does not match the expected digest",
        )

    graph_path = cast(str, arguments.graph)
    graph = _decode_graph_file(graph_path)
    strict_toolchain = cast(bool, arguments.strict_toolchain)
    try:
        report = replay_certificate(
            certificate,
            graph,
            strict_toolchain=strict_toolchain,
        )
    except CertificateReplayError:
        raise _CLIError(
            EXIT_INTERNAL,
            "certificate replay could not be completed safely",
        ) from None

    machine_readable = cast(bool, arguments.json)
    exit_code = _replay_exit(report.status)
    if machine_readable:
        sys.stdout.write(
            _replay_json(
                certificate,
                graph,
                report,
                exit_code=exit_code,
            )
        )
    else:
        sys.stdout.write(_replay_text(graph, report, exit_code=exit_code))
    return exit_code


def _run_compare(arguments: argparse.Namespace) -> int:
    per_check_timeout_ms = cast(int, arguments.per_check_timeout_ms)
    total_timeout_ms = cast(int, arguments.total_timeout_ms)
    if total_timeout_ms < per_check_timeout_ms:
        raise _CLIError(
            EXIT_USAGE,
            "invalid command-line arguments; use --help",
        )
    try:
        limits = SolverLimits(
            per_check_timeout_ms=per_check_timeout_ms,
            total_timeout_ms=total_timeout_ms,
            max_memory_mb=cast(int, arguments.max_memory_mb),
            max_core_shrink_checks=cast(int, arguments.max_core_shrink_checks),
            max_uniqueness_checks=cast(int, arguments.max_uniqueness_checks),
        )
    except UnitSentinelError:
        raise _CLIError(
            EXIT_USAGE,
            "invalid command-line arguments; use --help",
        ) from None

    expected_plan_digest = cast(str, arguments.expect_plan_sha256)
    plan = _decode_comparison_plan_file(
        cast(str, arguments.plan),
        expected_digest=expected_plan_digest,
    )
    if not hmac.compare_digest(plan.registry_digest, BUILTIN_REGISTRY.digest):
        raise _CLIError(
            EXIT_INPUT,
            "comparison plan registry does not match the current registry",
        )

    training_graph = _decode_graph_file(
        cast(str, arguments.training_graph),
        label="training graph input",
        expected_digest=plan.training_graph_digest,
    )
    serving_graph = _decode_graph_file(
        cast(str, arguments.serving_graph),
        label="serving graph input",
        expected_digest=plan.serving_graph_digest,
    )
    policy = ComparisonPolicy(expected_plan_digest=expected_plan_digest)
    try:
        result = compare_graphs(
            plan,
            training_graph=training_graph,
            serving_graph=serving_graph,
            limits=limits,
            policy=policy,
        )
    except ComparisonError:
        raise _CLIError(
            EXIT_INPUT,
            "comparison inputs are inconsistent",
        ) from None
    except UnitSentinelError:
        raise _CLIError(
            EXIT_INTERNAL,
            "comparison could not be completed safely",
        ) from None
    if type(result) is not ComparisonResult:
        raise _CLIError(
            EXIT_INTERNAL,
            "comparison returned an unexpected result",
        )
    try:
        result.validate()
        result_is_bound = (
            result.comparison_id == plan.comparison_id
            and hmac.compare_digest(result.plan_digest, plan.digest)
            and hmac.compare_digest(
                result.training_graph_digest,
                training_graph.digest,
            )
            and hmac.compare_digest(
                result.serving_graph_digest,
                serving_graph.digest,
            )
            and hmac.compare_digest(
                result.registry_digest,
                BUILTIN_REGISTRY.digest,
            )
            and result.limits == limits
        )
    except UnitSentinelError:
        result_is_bound = False
    if not result_is_bound:
        raise _CLIError(
            EXIT_INTERNAL,
            "comparison result does not bind the CLI request",
        )
    try:
        result_payload = encode_comparison_result(result)
    except ComparisonResultDecodeError:
        raise _CLIError(
            EXIT_INTERNAL,
            "comparison result could not be encoded safely",
        ) from None
    if not hmac.compare_digest(sha256_hex(result_payload), result.digest):
        raise _CLIError(
            EXIT_INTERNAL,
            "comparison result digest could not be confirmed",
        )

    output_path = cast(str | None, arguments.result_output)
    result_written = output_path is not None
    exit_code = _comparison_exit(result.status)
    if cast(bool, arguments.json):
        report = _comparison_json(
            plan,
            training_graph,
            serving_graph,
            result,
            expected_plan_digest=expected_plan_digest,
            result_written=result_written,
            exit_code=exit_code,
        )
    else:
        report = _comparison_text(
            plan,
            training_graph,
            serving_graph,
            result,
            expected_plan_digest=expected_plan_digest,
            result_written=result_written,
            exit_code=exit_code,
        )
    if output_path is not None:
        _atomic_write_new(
            output_path,
            result_payload,
            label="comparison result output",
        )
    sys.stdout.write(report)
    return exit_code


def _onnx_import_text(
    result: OnnxImportResult,
    *,
    exit_code: int,
) -> str:
    result.validate()
    lines = [
        "UnitSentinel ONNX import: IMPORTED",
        f"exit code: {exit_code}",
        f"tool: unitsentinel {VERSION}",
        (
            "checker: onnx.checker.check_model "
            f"{ONNX_RUNTIME_VERSION} (full=yes, custom-domains=yes)"
        ),
        f"model sha256: {result.source_digest}",
        f"model bytes: {result.source_size}",
        f"model contract: IR {ONNX_IR_VERSION}, default opset {ONNX_OPSET_VERSION}",
        "model executed: no",
        "external tensor data: rejected",
        f"metadata key: {ONNX_CONTRACT_METADATA_KEY}",
        f"metadata schema: {ONNX_CONTRACT_SCHEMA}",
        f"metadata sha256: {result.contract_digest}",
        f"operators ({len(result.operator_bindings)}):",
    ]
    for binding in result.operator_bindings:
        lines.append(
            "  "
            f"{binding.onnx_name} | "
            f"{binding.onnx_op_type} -> {binding.operation.value} | "
            f"node={binding.node_id}"
        )
    lines.extend(
        (
            f"graph id: {result.graph.graph_id}",
            f"graph sha256: {result.graph.digest}",
            (
                "graph shape: "
                f"{len(result.graph.inputs)} inputs, "
                f"{len(result.graph.nodes)} nodes, "
                f"{len(result.graph.outputs)} outputs"
            ),
            f"import receipt sha256: {result.digest}",
            "graph output: written",
            "next step: unitsentinel verify <graph.json>",
        )
    )
    return "\n".join(lines) + "\n"


def _onnx_import_json(
    result: OnnxImportResult,
    *,
    exit_code: int,
) -> str:
    result.validate()
    record = {
        "exit_code": exit_code,
        "graph_output": "written",
        "import": {
            "record": result.canonical_record(),
            "sha256": result.digest,
        },
        "schema": IMPORT_ONNX_OUTPUT_SCHEMA,
        "tool": {"name": "unitsentinel", "version": VERSION},
    }
    return canonical_json_bytes(record).decode("utf-8") + "\n"


def _run_import_onnx(arguments: argparse.Namespace) -> int:
    model_payload = _read_bounded_file(
        cast(str, arguments.model),
        label="ONNX model input",
        max_bytes=MAX_ONNX_MODEL_BYTES,
    )
    try:
        result = import_onnx_model(model_payload)
    except OnnxDependencyError as error:
        raise _CLIError(EXIT_INPUT, str(error)) from None
    except OnnxAdapterError as error:
        raise _CLIError(
            EXIT_INPUT,
            f"ONNX model input is invalid: {error}",
        ) from None
    if type(result) is not OnnxImportResult:
        raise _CLIError(
            EXIT_INTERNAL,
            "ONNX import returned an unexpected result",
        )
    try:
        result.validate()
        graph_payload = encode_graph(result.graph)
    except UnitSentinelError:
        raise _CLIError(
            EXIT_INTERNAL,
            "ONNX import result could not be encoded safely",
        ) from None
    if not hmac.compare_digest(sha256_hex(graph_payload), result.graph.digest):
        raise _CLIError(
            EXIT_INTERNAL,
            "ONNX import graph digest could not be confirmed",
        )

    if cast(bool, arguments.json):
        report = _onnx_import_json(result, exit_code=EXIT_SUCCESS)
    else:
        report = _onnx_import_text(result, exit_code=EXIT_SUCCESS)
    _atomic_write_new(
        cast(str, arguments.graph_output),
        graph_payload,
        label="graph output",
    )
    sys.stdout.write(report)
    return EXIT_SUCCESS


def _run_repair(arguments: argparse.Namespace) -> int:
    graph_path = cast(str, arguments.graph)
    graph = _decode_graph_file(graph_path)
    limits = RepairLimits(
        max_sites=cast(int, arguments.max_sites),
        max_candidates=cast(int, arguments.max_candidates),
        max_verifier_calls=cast(int, arguments.max_verifier_calls),
        max_work_items=cast(int, arguments.max_work_items),
        total_timeout_ms=cast(int, arguments.total_timeout_ms),
    )
    solver_limits = _DEFAULT_SOLVER_LIMITS
    try:
        report = propose_unit_annotation_repair(
            graph,
            repair_limits=limits,
            solver_limits=solver_limits,
        )
        if type(report) is not UnitRepairResult:
            raise RepairError("repair returned an unexpected result")
        report.validate()
        if (
            not hmac.compare_digest(
                report.source_graph.digest,
                graph.digest,
            )
            or not hmac.compare_digest(
                report.registry.digest,
                BUILTIN_REGISTRY.digest,
            )
            or report.repair_limits != limits
            or report.solver_limits != solver_limits
        ):
            raise RepairError("repair result does not bind the CLI request")
    except RepairError:
        raise _CLIError(
            EXIT_INTERNAL,
            "repair could not be completed safely",
        ) from None

    exit_code = _repair_exit(report.status)
    sys.stdout.write(_repair_json(graph, report, exit_code=exit_code))
    return exit_code


def _dispatch(arguments: argparse.Namespace) -> int:
    command = cast(str, arguments.command)
    if command == "verify":
        return _run_verify(arguments)
    if command == "replay":
        return _run_replay(arguments)
    if command == "compare":
        return _run_compare(arguments)
    if command == "import-onnx":
        return _run_import_onnx(arguments)
    if command == "repair":
        return _run_repair(arguments)
    raise _CLIError(EXIT_USAGE, "command is required; use --help")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its stable process exit code."""

    try:
        arguments = _parser().parse_args(argv)
        return _dispatch(arguments)
    except _CLIError as error:
        sys.stderr.write(f"unitsentinel: error: {error.message}\n")
        return error.exit_code
    except KeyboardInterrupt:
        sys.stderr.write("unitsentinel: interrupted\n")
        return EXIT_INTERRUPTED
    except UnitSentinelError:
        sys.stderr.write("unitsentinel: error: internal contract failure\n")
        return EXIT_INTERNAL
    except Exception:
        sys.stderr.write("unitsentinel: error: internal failure\n")
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
