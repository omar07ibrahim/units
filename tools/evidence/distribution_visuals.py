"""Build reproducible visuals for the hosted distribution contract.

The two committed sources are deliberately stable: one canonical description
of the implemented verifier boundary and one normalized transcript that the
hosted job reconstructs from its actual environment and verifier output.  No
timestamp, runner path, run identifier, commit identity, benchmark, or
self-digest enters this evidence slice.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from tools import verify_distribution as distribution
from tools.evidence.generate import (
    ASSETS,
    EVIDENCE,
    EvidenceError,
    _atomic_write,
    _canonical_bytes,
    _check_files,
    _require_recording_environment,
)
from tools.evidence.generate import (
    _read_regular_file as _shared_read_regular_file,
)
from tools.evidence.visuals import (
    distribution_contract_svg,
    distribution_terminal_svg,
)
from unitsentinel.canonical import canonical_json_bytes

OFFLINE_PIP_FLAGS: Final = (
    distribution.OFFLINE_PIP_FLAGS[0],
    f"{distribution.OFFLINE_PIP_FLAGS[1]}=<hash-pinned-wheelhouse>",
    *distribution.OFFLINE_PIP_FLAGS[2:],
)
CONTRACT_SCHEMA: Final = "unitsentinel.distribution-contract/v1"

CONTRACT_PATH: Final = EVIDENCE / "data" / "distribution-contract.json"
TRANSCRIPT_PATH: Final = EVIDENCE / "captures" / "distribution.txt"
CONTRACT_SVG_PATH: Final = ASSETS / "distribution-contract.svg"
TERMINAL_SVG_PATH: Final = ASSETS / "distribution-terminal.svg"
EXPECTED_OUTPUT_PATHS: Final = frozenset(
    {
        CONTRACT_PATH,
        TRANSCRIPT_PATH,
        CONTRACT_SVG_PATH,
        TERMINAL_SVG_PATH,
    }
)


@dataclass(frozen=True, slots=True)
class DistributionSources:
    """Strictly checked canonical contract and complete stable transcript."""

    contract: dict[str, Any]
    contract_payload: bytes
    transcript: str


def _read_regular_file(path: Path, *, purpose: str) -> bytes:
    """Expose the shared bounded reader as this slice's patchable boundary."""

    return _shared_read_regular_file(path, purpose=purpose)


def _python_version() -> str:
    return ".".join(str(component) for component in distribution.EXPECTED_PYTHON)


def _expected_contract() -> dict[str, Any]:
    return {
        "backend": {
            "build_backend": distribution.BUILD_BACKEND,
            "requirement": distribution.EXPECTED_BACKEND,
        },
        "host": {
            "implementation": "CPython",
            "machine": "x86_64",
            "operating_system": "Linux",
            "python_version": _python_version(),
        },
        "network_boundary": {
            "acquisition": (
                "hash-required binary-only dependency download occurs before "
                "the no-index install boundary"
            ),
            "verification": (
                "resolver index access is disabled; validation, builds, and smoke "
                "checks require no network"
            ),
        },
        "offline_install": {
            "environment": (
                "new isolated virtual environment with an empty working directory"
            ),
            "pip_flags": list(OFFLINE_PIP_FLAGS),
            "smoke_checks": [
                {
                    "expected_stdout": distribution.IMPORT_SMOKE_STDOUT,
                    "kind": "isolated import",
                    "verifies": [
                        "UnitSentinel package and metadata version 0.1.0",
                        "z3-solver metadata version 4.16.0.0",
                        "Z3 runtime version 4.16.0",
                        "installed libz3.so starts with ELF magic",
                    ],
                },
                {
                    "expected_stdout": distribution.CONSOLE_SMOKE_STDOUT,
                    "kind": "console version",
                    "verifies": ["installed unitsentinel entry point"],
                },
            ],
        },
        "reproducibility": {
            "canonical_sdist": {
                "builds": 2,
                "canonicalization": "normalized gzip and POSIX tar metadata",
                "equality": "byte-for-byte",
                "filename": f"{distribution.SDIST_ROOT}.tar.gz",
                "inputs": "two isolated copies of the tracked checkout",
            },
            "wheel": {
                "builds": 2,
                "equality": "byte-for-byte",
                "filename": distribution.WHEEL_NAME,
                "inputs": "two extractions of the canonical sdist",
                "native_payloads": "forbidden",
                "tag": distribution.WHEEL_TAG,
            },
        },
        "schema": CONTRACT_SCHEMA,
        "tool": {"name": distribution.NAME, "version": distribution.VERSION},
        "trust": {
            "not_claimed": [
                "artifact signing, publisher identity, or provenance attestation",
                (
                    "byte equality on other Python, backend, operating-system, "
                    "or architecture versions"
                ),
                "publication to an index or deployment to a runtime service",
                "runtime correctness beyond the two isolated smoke checks",
            ],
            "verified": [
                (
                    "tracked checkout is preserved in the canonical sdist with a "
                    "closed generated-file surface"
                ),
                "two canonical sdists and two wheels are byte-for-byte equal",
                (
                    "the locked Z3 wheel matches its filename, outer hash, size, "
                    "tag, RECORD, and three x86-64 ELF payloads"
                ),
                (
                    "a clean local-wheel resolver install and both smoke checks "
                    "complete without index access"
                ),
            ],
        },
        "z3_solver": {
            "elf_paths": list(distribution.Z3_ELF_PATHS),
            "filename": distribution.Z3_WHEEL_NAME,
            "record": {
                "entries": (
                    "every non-self row matches its SHA-256 digest and byte size"
                ),
                "self_row": "digest and size are empty",
            },
            "sha256": distribution.Z3_SHA256,
            "size_bytes": distribution.Z3_SIZE,
            "tag": distribution.Z3_WHEEL_TAG,
        },
    }


def _expected_transcript() -> str:
    return (
        "$ python -c 'import platform; print(platform.python_version())'\n"
        f"{_python_version()}\n"
        "$ uname -s\n"
        "Linux\n"
        "$ uname -m\n"
        "x86_64\n"
        "$ python -I tools/verify_distribution.py --wheelhouse "
        '"$RUNNER_TEMP/unitsentinel-wheelhouse"\n'
        f"{distribution.DISTRIBUTION_SUCCESS_TEXT}\n"
        "[exit 0]\n"
    )


def _validate_contract_payload(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EvidenceError("distribution contract is not canonical JSON") from None
    if type(value) is not dict or payload != canonical_json_bytes(value) + b"\n":
        raise EvidenceError("distribution contract is not canonical JSON")
    expected = _canonical_bytes(_expected_contract())
    if payload != expected:
        raise EvidenceError("distribution contract has drifted from the verifier")
    return cast(dict[str, Any], value)


def _validate_transcript_payload(payload: bytes) -> str:
    try:
        transcript = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise EvidenceError("distribution transcript is not UTF-8") from None
    if transcript != _expected_transcript():
        raise EvidenceError("distribution transcript is not the exact hosted report")
    lines = transcript.rstrip("\n").splitlines()
    if len(lines) != 9 or any(not line.isprintable() for line in lines):
        raise EvidenceError("distribution transcript line set is not exact")
    return transcript


def _expected_sources() -> DistributionSources:
    contract_payload = _canonical_bytes(_expected_contract())
    transcript_payload = _expected_transcript().encode("utf-8")
    return DistributionSources(
        contract=_validate_contract_payload(contract_payload),
        contract_payload=contract_payload,
        transcript=_validate_transcript_payload(transcript_payload),
    )


def _load_sources() -> DistributionSources:
    contract_payload = _read_regular_file(
        CONTRACT_PATH,
        purpose="distribution contract",
    )
    transcript_payload = _read_regular_file(
        TRANSCRIPT_PATH,
        purpose="distribution hosted transcript",
    )
    return DistributionSources(
        contract=_validate_contract_payload(contract_payload),
        contract_payload=contract_payload,
        transcript=_validate_transcript_payload(transcript_payload),
    )


def _build_files(*, use_committed_sources: bool) -> dict[Path, bytes]:
    sources = _load_sources() if use_committed_sources else _expected_sources()
    files = {
        CONTRACT_PATH: sources.contract_payload,
        TRANSCRIPT_PATH: sources.transcript.encode("utf-8"),
        CONTRACT_SVG_PATH: distribution_contract_svg(
            backend=distribution.EXPECTED_BACKEND,
            sdist_filename=f"{distribution.SDIST_ROOT}.tar.gz",
            wheel_filename=distribution.WHEEL_NAME,
            wheel_tag=distribution.WHEEL_TAG,
            z3_filename=distribution.Z3_WHEEL_NAME,
            z3_sha256=distribution.Z3_SHA256,
            z3_size_bytes=distribution.Z3_SIZE,
            z3_tag=distribution.Z3_WHEEL_TAG,
            z3_elf_paths=distribution.Z3_ELF_PATHS,
            pip_flags=OFFLINE_PIP_FLAGS,
            import_smoke=distribution.IMPORT_SMOKE_STDOUT.rstrip("\n"),
            console_smoke=distribution.CONSOLE_SMOKE_STDOUT.rstrip("\n"),
        ).encode("utf-8"),
        TERMINAL_SVG_PATH: distribution_terminal_svg(
            transcript=sources.transcript,
        ).encode("utf-8"),
    }
    if set(files) != EXPECTED_OUTPUT_PATHS:
        raise EvidenceError("distribution visual output allowlist is violated")
    return files


def _write_files(files: dict[Path, bytes]) -> None:
    if set(files) != EXPECTED_OUTPUT_PATHS:
        raise EvidenceError("distribution visual output allowlist is violated")
    for path in sorted(files):
        _atomic_write(path, files[path])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record or verify the fixed UnitSentinel distribution evidence slice."
        )
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--record",
        action="store_true",
        help="refresh only the canonical contract, capture, and two SVGs",
    )
    modes.add_argument(
        "--check",
        action="store_true",
        help="validate the committed sources and compare all four exact outputs",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.record:
            _require_recording_environment()
        files = _build_files(use_committed_sources=arguments.check)
        if arguments.check:
            _check_files(files)
        else:
            _write_files(files)
    except EvidenceError as error:
        sys.stderr.write(f"distribution-visuals: error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
