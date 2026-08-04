"""Verify UnitSentinel's source-to-offline-install release contract.

The verifier deliberately treats the build backend, both archives, the pinned
native solver wheel, and the installed environment as separate trust
boundaries.  It is a release check, not a general-purpose archive extractor.
"""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import os
import platform
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesParser
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import BinaryIO, Final, NoReturn

ROOT: Final = Path(__file__).resolve().parents[1]
NAME: Final = "unitsentinel"
VERSION: Final = "0.1.0"
SDIST_ROOT: Final = f"{NAME}-{VERSION}"
WHEEL_NAME: Final = f"{NAME}-{VERSION}-py3-none-any.whl"
DIST_INFO: Final = f"{NAME}-{VERSION}.dist-info"
EXPECTED_PYTHON: Final = (3, 12, 3)
EXPECTED_BACKEND: Final = "setuptools==83.0.0"
SOURCE_DATE_EPOCH: Final = 1_722_470_400

Z3_WHEEL_NAME: Final = "z3_solver-4.16.0.0-py3-none-manylinux_2_27_x86_64.whl"
Z3_SHA256: Final = "afae2551f795670f0522cfce82132d129c408a2694adff71eb01ba0f2ece44f9"
Z3_SIZE: Final = 31_741_807
Z3_DIST_INFO: Final = "z3_solver-4.16.0.0.dist-info"
LOCK_TEXT: Final = f"z3-solver==4.16.0.0 \\\n    --hash=sha256:{Z3_SHA256}\n"

MAX_ARCHIVE_BYTES: Final = 64 << 20
MAX_MEMBER_BYTES: Final = 64 << 20
MAX_EXPANDED_BYTES: Final = 128 << 20
MAX_MEMBERS: Final = 8_192
MAX_PATH_CHARS: Final = 512
MAX_PATH_DEPTH: Final = 32
MAX_OUTPUT_BYTES: Final = 1 << 20
PROCESS_DRAIN_SECONDS: Final = 2.0

GENERATED_SDIST_FILES: Final = frozenset(
    {
        "PKG-INFO",
        "setup.cfg",
        "src/unitsentinel.egg-info/PKG-INFO",
        "src/unitsentinel.egg-info/SOURCES.txt",
        "src/unitsentinel.egg-info/dependency_links.txt",
        "src/unitsentinel.egg-info/entry_points.txt",
        "src/unitsentinel.egg-info/requires.txt",
        "src/unitsentinel.egg-info/top_level.txt",
    }
)
WHEEL_METADATA_FILES: Final = frozenset(
    {"METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD"}
)
NATIVE_SUFFIXES: Final = (".so", ".dylib", ".dll", ".pyd", ".a", ".lib")


class DistributionVerificationError(ValueError):
    """One deterministic release-contract violation."""


@dataclass(frozen=True, slots=True)
class BuiltArtifacts:
    sdist: Path
    wheel: Path


def _reject(message: str) -> NoReturn:
    raise DistributionVerificationError(message)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _record_digest(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return digest.rstrip(b"=").decode("ascii")


def _is_native_payload(path: str, payload: bytes) -> bool:
    lowered = path.casefold()
    return lowered.endswith(NATIVE_SUFFIXES) or payload.startswith(
        (b"\x7fELF", b"MZ", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")
    )


def _is_x86_64_elf(payload: bytes) -> bool:
    return (
        len(payload) >= 20
        and payload[:4] == b"\x7fELF"
        and payload[4] == 2
        and payload[5] == 1
        and int.from_bytes(payload[18:20], "little") == 62
    )


def _safe_path(raw: str, *, directory: bool = False) -> str:
    if directory and raw.endswith("/"):
        raw = raw[:-1]
    if not raw or len(raw) > MAX_PATH_CHARS:
        _reject("archive path is empty or too long")
    if raw.startswith("/") or "\\" in raw or ":" in raw:
        _reject("archive path is absolute or uses a nonportable separator")
    if raw != unicodedata.normalize("NFC", raw):
        _reject("archive path is not NFC-normalized")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        _reject("archive path contains a control character")
    parts = raw.split("/")
    if len(parts) > MAX_PATH_DEPTH or any(
        part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts
    ):
        _reject("archive path has an unsafe component or depth")
    return "/".join(parts)


def _collision_key(path: str) -> str:
    return unicodedata.normalize("NFKC", path).casefold()


def _register_path(
    path: str,
    *,
    exact: set[str],
    portable: dict[str, str],
) -> None:
    if path in exact:
        _reject("archive contains a duplicate path")
    key = _collision_key(path)
    previous = portable.get(key)
    if previous is not None and previous != path:
        _reject("archive contains a portable path collision")
    exact.add(path)
    portable[key] = path


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        status = path.stat(follow_symlinks=False)
    except OSError as error:
        raise DistributionVerificationError(f"{label} is unavailable") from error
    if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_ARCHIVE_BYTES:
        _reject(f"{label} is not one bounded regular file")


def _read_gzip_bounded(path: Path) -> bytes:
    expanded = bytearray()
    try:
        with gzip.open(path, "rb") as stream:
            while chunk := stream.read(64 << 10):
                if len(expanded) + len(chunk) > MAX_EXPANDED_BYTES:
                    _reject("sdist expands beyond the accepted bound")
                expanded.extend(chunk)
    except (OSError, EOFError) as error:
        raise DistributionVerificationError(
            "sdist is not one valid gzip stream"
        ) from error
    return bytes(expanded)


def _read_sdist(path: Path) -> dict[str, bytes]:
    _require_regular_file(path, label="sdist")
    exact: set[str] = set()
    portable: dict[str, str] = {}
    payloads: dict[str, bytes] = {}
    total = 0
    raw_tar = _read_gzip_bounded(path)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_MEMBERS:
                _reject("sdist member count is outside the accepted bound")
            for member in members:
                path_name = _safe_path(member.name, directory=member.isdir())
                _register_path(path_name, exact=exact, portable=portable)
                if member.sparse:
                    _reject("sdist contains a sparse member")
                if member.isdir():
                    continue
                if (
                    not member.isfile()
                    or member.size < 0
                    or member.size > MAX_MEMBER_BYTES
                ):
                    _reject("sdist contains an unsupported or oversized member")
                total += member.size
                if total > MAX_EXPANDED_BYTES:
                    _reject("sdist payloads exceed the accepted bound")
                stream = archive.extractfile(member)
                if stream is None:
                    _reject("sdist member could not be read")
                payload = stream.read(MAX_MEMBER_BYTES + 1)
                if len(payload) != member.size:
                    _reject("sdist member size does not match its payload")
                payloads[path_name] = payload
    except (tarfile.TarError, OSError) as error:
        raise DistributionVerificationError(
            "sdist is not one valid tar archive"
        ) from error
    return payloads


def _read_wheel(path: Path, *, label: str) -> dict[str, bytes]:
    _require_regular_file(path, label=label)
    exact: set[str] = set()
    portable: dict[str, str] = {}
    payloads: dict[str, bytes] = {}
    total = 0
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_MEMBERS:
                _reject(f"{label} member count is outside the accepted bound")
            for info in infos:
                path_name = _safe_path(info.filename, directory=info.is_dir())
                _register_path(path_name, exact=exact, portable=portable)
                if info.flag_bits & 1:
                    _reject(f"{label} contains an encrypted member")
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    _reject(f"{label} contains a non-regular member")
                if info.is_dir():
                    continue
                if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
                    _reject(f"{label} contains an oversized member")
                total += info.file_size
                if total > MAX_EXPANDED_BYTES:
                    _reject(f"{label} payloads exceed the accepted bound")
                payload = archive.read(info)
                if len(payload) != info.file_size:
                    _reject(f"{label} member size does not match its payload")
                payloads[path_name] = payload
    except (zipfile.BadZipFile, OSError, RuntimeError) as error:
        raise DistributionVerificationError(
            f"{label} is not one valid ZIP archive"
        ) from error
    return payloads


def _canonical_sdist(payloads: Mapping[str, bytes]) -> bytes:
    """Return the deliberately normalized release sdist bytes."""

    directories: set[str] = set()
    for path in payloads:
        parts = path.split("/")
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    destination = io.BytesIO()
    with (
        gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=destination,
            mtime=SOURCE_DATE_EPOCH,
        ) as compressed,
        tarfile.open(
            fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
        ) as archive,
    ):
        for directory in sorted(directories):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = SOURCE_DATE_EPOCH
            archive.addfile(info)
        for path, payload in sorted(payloads.items()):
            info = tarfile.TarInfo(path)
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = SOURCE_DATE_EPOCH
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return destination.getvalue()


def _extract_sdist(payloads: Mapping[str, bytes], destination: Path) -> Path:
    roots = {path.split("/", 1)[0] for path in payloads}
    if roots != {SDIST_ROOT} or (
        SDIST_ROOT not in payloads
        and not any(path.startswith(f"{SDIST_ROOT}/") for path in payloads)
    ):
        _reject("sdist does not have the exact expected root")
    source_root = destination / SDIST_ROOT
    source_root.mkdir(mode=0o700, parents=True)
    for path, payload in payloads.items():
        if path == SDIST_ROOT:
            continue
        prefix = f"{SDIST_ROOT}/"
        if not path.startswith(prefix):
            _reject("sdist payload escapes its expected root")
        target = destination / path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.chmod(0o600)
    return source_root


def _parse_metadata(payload: bytes, *, label: str) -> Message:
    try:
        message = BytesParser().parsebytes(payload)
    except (UnicodeError, ValueError) as error:
        raise DistributionVerificationError(f"{label} metadata is malformed") from error
    if message.is_multipart() or message.defects:
        _reject(f"{label} metadata is malformed or unexpectedly multipart")
    return message


def _one_header(message: Message, name: str, *, label: str) -> str:
    values = message.get_all(name, [])
    if len(values) != 1:
        _reject(f"{label} must contain exactly one {name} header")
    return values[0]


def _metadata_description_bytes(payload: bytes) -> bytes:
    header, separator, description = payload.partition(b"\n\n")
    if not separator or not header or b"\r" in header:
        _reject("core metadata does not have the exact LF header boundary")
    return description


def _validate_core_metadata(payload: bytes, *, label: str) -> Message:
    metadata = _parse_metadata(payload, label=label)
    expected = {
        "Metadata-Version": "2.4",
        "Name": NAME,
        "Version": VERSION,
        "Summary": (
            "Dimensional proof certificates for scientific and ML computation graphs"
        ),
        "Author-email": (
            "Omar Ibrahim <31526072+omar07ibrahim@users.noreply.github.com>"
        ),
        "Requires-Python": "<3.15,>=3.11",
        "Description-Content-Type": "text/markdown",
    }
    for header, value in expected.items():
        if _one_header(metadata, header, label=label) != value:
            _reject(f"{label} has unexpected {header} metadata")
    requirement_values = metadata.get_all("Requires-Dist", [])
    requirements = set(requirement_values)
    expected_requirements = {
        "z3-solver==4.16.0.0",
        'build==1.5.0; extra == "dev"',
        'coverage[toml]==7.15.2; extra == "dev"',
        'mypy==2.3.0; extra == "dev"',
        'pip-audit==2.10.1; extra == "dev"',
        'ruff==0.16.0; extra == "dev"',
        'setuptools==83.0.0; extra == "dev"',
    }
    if (
        len(requirement_values) != len(requirements)
        or requirements != expected_requirements
    ):
        _reject(f"{label} dependency metadata is not the exact reviewed set")
    if metadata.get_all("Provides-Extra", []) != ["dev"]:
        _reject(f"{label} optional dependency metadata is not the reviewed set")
    url_values = metadata.get_all("Project-URL", [])
    urls = set(url_values)
    if len(url_values) != len(urls) or urls != {
        "Repository, https://github.com/omar07ibrahim/units",
        "Issues, https://github.com/omar07ibrahim/units/issues",
        "Documentation, https://github.com/omar07ibrahim/units#readme",
    }:
        _reject(f"{label} project URLs are not the exact reviewed set")
    if metadata.get_all("License") or metadata.get_all("License-Expression"):
        _reject(f"{label} invents licensing metadata")
    return metadata


def _validate_pyproject(payload: bytes) -> None:
    try:
        configuration = tomllib.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise DistributionVerificationError("pyproject.toml is malformed") from error
    if configuration.get("build-system") != {
        "requires": ["setuptools==83.0.0"],
        "build-backend": "setuptools.build_meta",
    }:
        _reject("pyproject.toml build backend is not the exact reviewed contract")
    project = configuration.get("project")
    if not isinstance(project, dict):
        _reject("pyproject.toml project table is missing")
    expected = {
        "name": NAME,
        "version": VERSION,
        "requires-python": ">=3.11,<3.15",
        "dependencies": ["z3-solver==4.16.0.0"],
        "scripts": {"unitsentinel": "unitsentinel.cli:main"},
    }
    if any(project.get(key) != value for key, value in expected.items()):
        _reject("pyproject.toml project contract differs from reviewed values")
    tool = configuration.get("tool")
    if not isinstance(tool, dict):
        _reject("pyproject.toml tool table is missing")
    setuptools_configuration = tool.get("setuptools")
    if not isinstance(setuptools_configuration, dict):
        _reject("pyproject.toml setuptools table is missing")
    if setuptools_configuration.get("license-files") != []:
        _reject("pyproject.toml must preserve the explicit no-license decision")


def _validate_record(payloads: Mapping[str, bytes], record_path: str) -> None:
    record_payload = payloads.get(record_path)
    if record_payload is None:
        _reject("wheel RECORD is missing")
    try:
        rows = list(
            csv.reader(io.StringIO(record_payload.decode("utf-8")), strict=True)
        )
    except (UnicodeError, csv.Error) as error:
        raise DistributionVerificationError("wheel RECORD is malformed") from error
    if any(len(row) != 3 for row in rows):
        _reject("wheel RECORD contains a malformed row")
    by_path: dict[str, tuple[str, str]] = {}
    for path, digest, size in rows:
        safe = _safe_path(path)
        if safe in by_path:
            _reject("wheel RECORD contains a duplicate row")
        by_path[safe] = (digest, size)
    if set(by_path) != set(payloads):
        _reject("wheel RECORD does not close the archive surface")
    for path, payload in payloads.items():
        digest, size = by_path[path]
        if path == record_path:
            if digest or size:
                _reject("wheel RECORD self-row must omit digest and size")
            continue
        if digest != f"sha256={_record_digest(payload)}" or size != str(len(payload)):
            _reject("wheel RECORD digest or size does not match its payload")


def _tracked_payloads() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="unitsentinel-git-inventory-") as temporary:
        temporary_root = Path(temporary)
        completed = _bounded_process(
            ("git", "ls-files", "-z", "--cached"),
            cwd=ROOT,
            environment=_closed_environment(
                temporary_root / "home", temporary_root / "tmp"
            ),
            timeout=30.0,
            label="tracked-source inventory",
        )
    if completed.stderr:
        _reject("tracked-source inventory wrote unexpected standard error")
    paths = completed.stdout.split(b"\0")
    if not paths or paths[-1] != b"":
        _reject("tracked-source inventory is not NUL-terminated")
    if len(paths) - 1 > MAX_MEMBERS:
        _reject("tracked release surface exceeds the file-count bound")
    payloads: dict[str, bytes] = {}
    total = 0
    for raw in paths[:-1]:
        try:
            path = _safe_path(raw.decode("utf-8", errors="strict"))
        except UnicodeError as error:
            raise DistributionVerificationError("tracked path is not UTF-8") from error
        source = ROOT / path
        try:
            status = source.stat(follow_symlinks=False)
        except OSError as error:
            raise DistributionVerificationError(
                "tracked release payload is unavailable"
            ) from error
        if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_MEMBER_BYTES:
            _reject("tracked release surface contains a non-regular file")
        total += status.st_size
        if total > MAX_EXPANDED_BYTES:
            _reject("tracked release surface exceeds the total-byte bound")
        payload = source.read_bytes()
        if len(payload) != status.st_size:
            _reject("tracked release payload changed while it was read")
        payloads[path] = payload
    if not payloads:
        _reject("tracked release surface is empty")
    return payloads


def _validate_sdist(
    payloads: Mapping[str, bytes],
    tracked: Mapping[str, bytes],
) -> bytes:
    prefix = f"{SDIST_ROOT}/"
    relative: dict[str, bytes] = {}
    for path, payload in payloads.items():
        if not path.startswith(prefix):
            _reject("sdist contains a payload outside its exact root")
        relative[path.removeprefix(prefix)] = payload
    actual = set(relative)
    expected = set(tracked)
    if expected - actual:
        _reject("sdist omits part of the tracked release surface")
    if actual - expected != set(GENERATED_SDIST_FILES):
        _reject("sdist contains an undeclared or missing generated payload")
    for path, payload in tracked.items():
        if relative[path] != payload:
            _reject("sdist tracked payload differs from the checkout")
    metadata_payload = relative["PKG-INFO"]
    _validate_core_metadata(metadata_payload, label="sdist")
    if relative["src/unitsentinel.egg-info/PKG-INFO"] != metadata_payload:
        _reject("sdist root and egg-info metadata differ")
    if _metadata_description_bytes(metadata_payload) != tracked["README.md"]:
        _reject("sdist long description differs from README.md")
    try:
        sources = (
            relative["src/unitsentinel.egg-info/SOURCES.txt"]
            .decode("utf-8", errors="strict")
            .splitlines()
        )
    except UnicodeError as error:
        raise DistributionVerificationError("sdist SOURCES.txt is not UTF-8") from error
    if len(sources) != len(set(sources)) or set(sources) != actual - {
        "PKG-INFO",
        "setup.cfg",
    }:
        _reject("sdist SOURCES.txt does not close its declared source surface")
    return metadata_payload


def _validate_own_wheel(
    path: Path,
    *,
    source_payloads: Mapping[str, bytes],
    sdist_metadata: bytes,
) -> dict[str, bytes]:
    if path.name != WHEEL_NAME:
        _reject("UnitSentinel wheel has an unexpected filename")
    payloads = _read_wheel(path, label="UnitSentinel wheel")
    package = {
        source_path.removeprefix("src/"): payload
        for source_path, payload in source_payloads.items()
        if source_path.startswith("src/unitsentinel/")
    }
    metadata_paths = {f"{DIST_INFO}/{name}" for name in WHEEL_METADATA_FILES}
    if set(payloads) != set(package) | metadata_paths:
        _reject("UnitSentinel wheel surface is not the exact package plus dist-info")
    for package_path, payload in package.items():
        if payloads[package_path] != payload:
            _reject("UnitSentinel wheel package byte differs from the sdist")
    for wheel_path, payload in payloads.items():
        if _is_native_payload(wheel_path, payload):
            _reject("UnitSentinel wheel unexpectedly contains native code")
    metadata_path = f"{DIST_INFO}/METADATA"
    if payloads[metadata_path] != sdist_metadata:
        _reject("wheel and sdist core metadata differ")
    _validate_core_metadata(payloads[metadata_path], label="wheel")
    wheel_headers = _parse_metadata(payloads[f"{DIST_INFO}/WHEEL"], label="WHEEL")
    if _one_header(wheel_headers, "Root-Is-Purelib", label="WHEEL") != "true":
        _reject("UnitSentinel wheel is not marked purelib")
    if wheel_headers.get_all("Tag", []) != ["py3-none-any"]:
        _reject("UnitSentinel wheel does not have the exact universal tag")
    if payloads[f"{DIST_INFO}/entry_points.txt"] != (
        b"[console_scripts]\nunitsentinel = unitsentinel.cli:main\n"
    ):
        _reject("UnitSentinel wheel has an unexpected console entry point")
    if payloads[f"{DIST_INFO}/top_level.txt"] != b"unitsentinel\n":
        _reject("UnitSentinel wheel has an unexpected top-level package")
    _validate_record(payloads, f"{DIST_INFO}/RECORD")
    return payloads


def _validate_z3_wheel(path: Path) -> None:
    if path.name != Z3_WHEEL_NAME or path.stat().st_size != Z3_SIZE:
        _reject("Z3 wheel filename or outer size differs from the lock")
    raw = path.read_bytes()
    if _sha256(raw) != Z3_SHA256:
        _reject("Z3 wheel outer SHA-256 differs from the lock")
    payloads = _read_wheel(path, label="Z3 wheel")
    metadata_path = f"{Z3_DIST_INFO}/METADATA"
    wheel_path = f"{Z3_DIST_INFO}/WHEEL"
    record_path = f"{Z3_DIST_INFO}/RECORD"
    for required in (metadata_path, wheel_path, record_path):
        if required not in payloads:
            _reject("Z3 wheel omits required dist-info metadata")
    metadata = _parse_metadata(payloads[metadata_path], label="Z3")
    if (
        _one_header(metadata, "Name", label="Z3").replace("_", "-").casefold()
        != "z3-solver"
    ):
        _reject("Z3 wheel has unexpected project identity")
    if _one_header(metadata, "Version", label="Z3") != "4.16.0.0":
        _reject("Z3 wheel has unexpected version metadata")
    wheel = _parse_metadata(payloads[wheel_path], label="Z3 WHEEL")
    if wheel.get_all("Tag", []) != ["py3-none-manylinux_2_27_x86_64"]:
        _reject("Z3 wheel has an unexpected platform tag")
    native_paths = (
        "z3_solver-4.16.0.0.data/data/bin/z3",
        "z3/lib/libz3.so",
        "z3/lib/libz3.so.4.16",
    )
    for native_path in native_paths:
        payload = payloads.get(native_path)
        if payload is None or not _is_x86_64_elf(payload):
            _reject("Z3 wheel omits an expected x86-64 ELF payload")
    _validate_record(payloads, record_path)


def _read_pipe(stream: BinaryIO, sink: bytearray, exceeded: threading.Event) -> None:
    try:
        while chunk := stream.read(64 << 10):
            if len(sink) + len(chunk) > MAX_OUTPUT_BYTES:
                exceeded.set()
            elif not exceeded.is_set():
                sink.extend(chunk)
    finally:
        stream.close()


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=PROCESS_DRAIN_SECONDS)
    if process.poll() is None:
        process.kill()
        process.wait()


def _bounded_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
    label: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        process = subprocess.Popen(
            tuple(command),
            cwd=cwd,
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise DistributionVerificationError(f"{label} could not start") from error
    if process.stdout is None or process.stderr is None:
        _kill_process_group(process)
        _reject(f"{label} did not expose bounded pipes")
    stdout = bytearray()
    stderr = bytearray()
    exceeded = threading.Event()
    readers = (
        threading.Thread(
            target=_read_pipe, args=(process.stdout, stdout, exceeded), daemon=True
        ),
        threading.Thread(
            target=_read_pipe, args=(process.stderr, stderr, exceeded), daemon=True
        ),
    )
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    while process.poll() is None and not exceeded.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        exceeded.wait(min(remaining, 0.05))
    timed_out = process.poll() is None and time.monotonic() >= deadline
    _kill_process_group(process)
    for reader in readers:
        reader.join(timeout=PROCESS_DRAIN_SECONDS)
    if any(reader.is_alive() for reader in readers):
        _reject(f"{label} left an output pipe open")
    if exceeded.is_set():
        _reject(f"{label} exceeded the output bound")
    if timed_out:
        _reject(f"{label} exceeded its timeout")
    returncode = process.wait()
    if returncode != 0:
        _reject(f"{label} failed with exit code {returncode}")
    return subprocess.CompletedProcess(
        tuple(command), returncode, bytes(stdout), bytes(stderr)
    )


def _closed_environment(home: Path, temporary: Path) -> dict[str, str]:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH),
        "TMPDIR": str(temporary),
        "TZ": "UTC",
    }


def _copy_tracked_source(payloads: Mapping[str, bytes], destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=True)
    for path, payload in payloads.items():
        target = destination / path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.chmod(0o600)
    for current in sorted(destination.rglob("*"), reverse=True):
        os.utime(current, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH), follow_symlinks=False)


def _backend_build(function: str, *, source: Path, output: Path, work: Path) -> Path:
    output.mkdir(mode=0o700, parents=True)
    program = (
        "import setuptools.build_meta,sys\n"
        f"print(setuptools.build_meta.{function}(sys.argv[1]))\n"
    )
    completed = _bounded_process(
        (sys.executable, "-I", "-B", "-c", program, str(output)),
        cwd=source,
        environment=_closed_environment(
            work / f"{output.name}-home", work / f"{output.name}-tmp"
        ),
        timeout=180.0,
        label=f"setuptools {function}",
    )
    try:
        lines = completed.stdout.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as error:
        raise DistributionVerificationError("setuptools output is not UTF-8") from error
    if not lines:
        _reject("setuptools did not report an artifact")
    artifact_name = _safe_path(lines[-1])
    if "/" in artifact_name:
        _reject("setuptools reported a non-local artifact")
    artifact = output / artifact_name
    _require_regular_file(artifact, label="setuptools artifact")
    if {entry.name for entry in output.iterdir()} != {artifact_name}:
        _reject("setuptools output directory contains an extra artifact")
    return artifact


def _build_release(tracked: Mapping[str, bytes], work: Path) -> BuiltArtifacts:
    source_a = work / "source-a"
    source_b = work / "source-b"
    _copy_tracked_source(tracked, source_a)
    _copy_tracked_source(tracked, source_b)
    raw_a = _backend_build(
        "build_sdist", source=source_a, output=work / "raw-a", work=work
    )
    raw_b = _backend_build(
        "build_sdist", source=source_b, output=work / "raw-b", work=work
    )
    payloads_a = _read_sdist(raw_a)
    payloads_b = _read_sdist(raw_b)
    canonical_a = _canonical_sdist(payloads_a)
    canonical_b = _canonical_sdist(payloads_b)
    if canonical_a != canonical_b:
        _reject("canonical sdist builds are not byte-for-byte reproducible")
    canonical_path = work / f"{SDIST_ROOT}.tar.gz"
    canonical_path.write_bytes(canonical_a)
    canonical_payloads = _read_sdist(canonical_path)
    _validate_sdist(canonical_payloads, tracked)
    extracted_a = _extract_sdist(canonical_payloads, work / "extracted-a")
    extracted_b = _extract_sdist(canonical_payloads, work / "extracted-b")
    wheel_a = _backend_build(
        "build_wheel", source=extracted_a, output=work / "wheel-a", work=work
    )
    wheel_b = _backend_build(
        "build_wheel", source=extracted_b, output=work / "wheel-b", work=work
    )
    if wheel_a.name != wheel_b.name or wheel_a.read_bytes() != wheel_b.read_bytes():
        _reject(
            "wheels built from the canonical sdist are not byte-for-byte reproducible"
        )
    return BuiltArtifacts(canonical_path, wheel_a)


def _isolated_install(
    wheel: Path,
    z3_wheel: Path,
    *,
    work: Path,
) -> None:
    environment_root = work / "installed"
    _bounded_process(
        (sys.executable, "-I", "-B", "-m", "venv", str(environment_root)),
        cwd=work,
        environment=_closed_environment(work / "venv-home", work / "venv-tmp"),
        timeout=60.0,
        label="isolated virtual environment creation",
    )
    scripts = "Scripts" if os.name == "nt" else "bin"
    python = (
        environment_root / scripts / ("python.exe" if os.name == "nt" else "python")
    )
    console = (
        environment_root
        / scripts
        / ("unitsentinel.exe" if os.name == "nt" else "unitsentinel")
    )
    wheel_digest = _sha256(wheel.read_bytes())
    requirements = work / "install-requirements.txt"
    requirements.write_text(
        f"{NAME} @ {wheel.resolve().as_uri()} --hash=sha256:{wheel_digest}\n"
        f"z3-solver==4.16.0.0 --hash=sha256:{Z3_SHA256}\n",
        encoding="utf-8",
    )
    empty_cwd = work / "empty-cwd"
    empty_cwd.mkdir(mode=0o700)
    environment = _closed_environment(work / "install-home", work / "install-tmp")
    environment["PIP_NO_INDEX"] = "1"
    _bounded_process(
        (
            str(python),
            "-I",
            "-B",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(z3_wheel.parent),
            "--require-hashes",
            "--only-binary=:all:",
            "--no-cache-dir",
            "--no-compile",
            "--requirement",
            str(requirements),
        ),
        cwd=empty_cwd,
        environment=environment,
        timeout=120.0,
        label="isolated offline install",
    )
    smoke_program = "\n".join(
        (
            "import importlib.metadata as m",
            "from pathlib import Path",
            "import unitsentinel, z3",
            f"assert unitsentinel.__version__ == {VERSION!r}",
            f"assert m.version({NAME!r}) == {VERSION!r}",
            "assert m.version('z3-solver') == '4.16.0.0'",
            "assert z3.get_version_string() == '4.16.0'",
            "library = Path(z3.__file__).parent / 'lib' / 'libz3.so'",
            "assert library.read_bytes()[:4] == b'\\x7fELF'",
            "print('installed unitsentinel 0.1.0 with z3 4.16.0')",
        )
    )
    smoke = _bounded_process(
        (str(python), "-I", "-B", "-c", smoke_program),
        cwd=empty_cwd,
        environment=environment,
        timeout=30.0,
        label="installed import smoke",
    )
    if smoke.stdout != b"installed unitsentinel 0.1.0 with z3 4.16.0\n" or smoke.stderr:
        _reject("installed import smoke emitted unexpected output")
    version = _bounded_process(
        (str(console), "--version"),
        cwd=empty_cwd,
        environment=environment,
        timeout=30.0,
        label="installed console smoke",
    )
    if version.stdout != b"unitsentinel 0.1.0\n" or version.stderr:
        _reject("installed console smoke emitted unexpected output")
    if tuple(empty_cwd.iterdir()):
        _reject("installed smoke wrote into its empty working directory")


def verify_distribution(wheelhouse: Path) -> str:
    """Execute the complete, bounded release verification path."""

    if sys.implementation.name != "cpython" or sys.version_info[:3] != EXPECTED_PYTHON:
        _reject("distribution verification requires exact CPython 3.12.3")
    if platform.system() != "Linux" or platform.machine().casefold() not in {
        "x86_64",
        "amd64",
    }:
        _reject("distribution verification requires Linux x86_64")
    try:
        backend_version = importlib_metadata.version("setuptools")
    except importlib_metadata.PackageNotFoundError as error:
        raise DistributionVerificationError(
            "the pinned setuptools backend is missing"
        ) from error
    if f"setuptools=={backend_version}" != EXPECTED_BACKEND:
        _reject("installed setuptools version differs from the pinned build backend")
    if (ROOT / "requirements-distribution.txt").read_text(
        encoding="utf-8"
    ) != LOCK_TEXT:
        _reject("distribution dependency lock differs from the reviewed bytes")
    try:
        wheelhouse_status = wheelhouse.stat(follow_symlinks=False)
    except OSError as error:
        raise DistributionVerificationError("wheelhouse is unavailable") from error
    if not stat.S_ISDIR(wheelhouse_status.st_mode):
        _reject("wheelhouse is not one real directory")
    entries = tuple(wheelhouse.iterdir())
    if len(entries) != 1 or entries[0].name != Z3_WHEEL_NAME:
        _reject("wheelhouse must contain exactly the locked Z3 wheel")
    z3_wheel = entries[0]
    _validate_z3_wheel(z3_wheel)
    tracked = _tracked_payloads()
    _validate_pyproject(tracked["pyproject.toml"])
    with tempfile.TemporaryDirectory(
        prefix="unitsentinel-distribution-"
    ) as temporary_name:
        work = Path(temporary_name).resolve(strict=True)
        if work == ROOT or work.is_relative_to(ROOT):
            _reject("distribution work directory must be outside the checkout")
        artifacts = _build_release(tracked, work)
        canonical_payloads = _read_sdist(artifacts.sdist)
        metadata = _validate_sdist(canonical_payloads, tracked)
        _validate_own_wheel(
            artifacts.wheel,
            source_payloads=tracked,
            sdist_metadata=metadata,
        )
        _isolated_install(artifacts.wheel, z3_wheel, work=work)
    return (
        "verified unitsentinel 0.1.0: canonical sdist -> "
        "py3-none-any wheel -> offline Z3 install"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify UnitSentinel's source-to-offline-install release contract."
    )
    parser.add_argument(
        "--wheelhouse",
        required=True,
        type=Path,
        help="directory containing exactly the locked Z3 wheel",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        result = verify_distribution(options.wheelhouse)
    except (DistributionVerificationError, OSError) as error:
        print(f"distribution verification failed: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
