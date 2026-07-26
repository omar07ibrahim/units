"""Deterministic command-line verification and certificate replay."""

from __future__ import annotations

import argparse
import errno
import hmac
import os
import secrets
import stat
import sys
from collections.abc import Sequence
from contextlib import suppress
from fractions import Fraction
from typing import Final, NoReturn, cast

from .canonical import canonical_json_bytes
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
from .domain import UnitSentinelError
from .graph import GRAPH_SCHEMA, ComputationGraph
from .graph_codec import MAX_GRAPH_BYTES, GraphDecodeError, decode_graph
from .registry import BUILTIN_REGISTRY, REGISTRY_SCHEMA, SHA256_HEX
from .replay import (
    CertificateReplay,
    CertificateReplayError,
    ReplayStatus,
    replay_certificate,
)
from .verification import VerificationResult, VerificationStatus
from .version import VERSION

VERIFY_OUTPUT_SCHEMA: Final = "unitsentinel.cli.verify/v1"
REPLAY_OUTPUT_SCHEMA: Final = "unitsentinel.cli.replay/v1"

EXIT_SUCCESS: Final = 0
EXIT_CONFLICT: Final = 1
EXIT_UNDERCONSTRAINED: Final = 2
EXIT_INDETERMINATE: Final = 3
EXIT_INPUT: Final = 4
EXIT_MISMATCH: Final = 5
EXIT_USAGE: Final = 64
EXIT_INTERNAL: Final = 70
EXIT_INTERRUPTED: Final = 130

_READ_CHUNK_BYTES: Final = 65_536
_TEMP_ATTEMPTS: Final = 32


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


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="unitsentinel",
        description=(
            "Verify exact dimensional contracts and replay detached proof claims."
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
    return parser


def _read_bounded_file(path: str, *, label: str, max_bytes: int) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _CLIError(EXIT_INPUT, f"{label} could not be opened") from None

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


def _open_output_directory(path: str) -> tuple[int, str]:
    parent, name = os.path.split(path)
    if not name or name in {".", ".."}:
        raise _CLIError(EXIT_INPUT, "certificate output path is invalid")
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
            "certificate output directory could not be opened",
        ) from None
    return descriptor, name


def _create_output_temp(directory: int) -> tuple[int, str]:
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
                "certificate output could not be created",
            ) from None
    raise _CLIError(
        EXIT_INPUT,
        "certificate output could not be created",
    )


def _unlink_temp(directory: int, name: str | None) -> bool:
    if name is None:
        return True
    try:
        os.unlink(name, dir_fd=directory)
    except OSError:
        return False
    return True


def _atomic_write_new(path: str, payload: bytes) -> None:
    directory, target = _open_output_directory(path)
    temporary: str | None = None
    descriptor = -1
    try:
        descriptor, temporary = _create_output_temp(directory)
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        except OSError:
            raise _CLIError(
                EXIT_INPUT,
                "certificate output could not be written",
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
                "certificate output already exists",
            ) from None
        except OSError:
            raise _CLIError(
                EXIT_INPUT,
                "certificate output could not be published",
            ) from None

        if not _unlink_temp(directory, temporary):
            raise _CLIError(
                EXIT_INPUT,
                "certificate output cleanup could not be confirmed",
            )
        temporary = None
        try:
            os.fsync(directory)
        except OSError:
            raise _CLIError(
                EXIT_INPUT,
                "certificate output durability could not be confirmed",
            ) from None
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        _unlink_temp(directory, temporary)
        with suppress(OSError):
            os.close(directory)


def _decode_graph_file(path: str) -> ComputationGraph:
    payload = _read_bounded_file(
        path,
        label="graph input",
        max_bytes=MAX_GRAPH_BYTES,
    )
    try:
        return decode_graph(payload)
    except GraphDecodeError as error:
        raise _CLIError(
            EXIT_INPUT,
            f"graph input is invalid: {error}",
        ) from None


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


def _dispatch(arguments: argparse.Namespace) -> int:
    command = cast(str, arguments.command)
    if command == "verify":
        return _run_verify(arguments)
    if command == "replay":
        return _run_replay(arguments)
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
