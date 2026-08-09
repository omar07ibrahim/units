from __future__ import annotations

import csv
import io
import os
import stat
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from tools import verify_distribution as distribution


def _metadata_payload(readme: bytes = b"# UnitSentinel test\n") -> bytes:
    headers = (
        b"Metadata-Version: 2.4\n"
        b"Name: unitsentinel\n"
        b"Version: 0.1.0\n"
        b"Summary: Dimensional proof certificates for scientific and ML "
        b"computation graphs\n"
        b"Author-email: Omar Ibrahim "
        b"<31526072+omar07ibrahim@users.noreply.github.com>\n"
        b"Requires-Python: <3.15,>=3.11\n"
        b"Description-Content-Type: text/markdown\n"
        b"Requires-Dist: z3-solver==4.16.0.0\n"
        b'Requires-Dist: build==1.5.0; extra == "dev"\n'
        b'Requires-Dist: coverage[toml]==7.15.2; extra == "dev"\n'
        b'Requires-Dist: mypy==2.3.0; extra == "dev"\n'
        b'Requires-Dist: onnx==1.22.0; extra == "dev"\n'
        b'Requires-Dist: onnx==1.22.0; extra == "onnx"\n'
        b'Requires-Dist: pip-audit==2.10.1; extra == "dev"\n'
        b'Requires-Dist: ruff==0.16.0; extra == "dev"\n'
        b'Requires-Dist: setuptools==83.0.0; extra == "dev"\n'
        b"Provides-Extra: dev\n"
        b"Provides-Extra: onnx\n"
        b"Project-URL: Repository, https://github.com/omar07ibrahim/units\n"
        b"Project-URL: Issues, https://github.com/omar07ibrahim/units/issues\n"
        b"Project-URL: Documentation, https://github.com/omar07ibrahim/units#readme\n"
        b"\n"
    )
    return headers + readme


def _record(payloads: dict[str, bytes], record_path: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for path, payload in sorted(payloads.items()):
        writer.writerow(
            (path, f"sha256={distribution._record_digest(payload)}", len(payload))
        )
    writer.writerow((record_path, "", ""))
    return output.getvalue().encode()


def _write_zip(
    path: Path,
    entries: Sequence[tuple[zipfile.ZipInfo | str, bytes]],
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)


def _write_wheel(path: Path, payloads: dict[str, bytes], record_path: str) -> None:
    complete = dict(payloads)
    complete[record_path] = _record(payloads, record_path)
    _write_zip(path, list(complete.items()))


def _write_tar(
    path: Path,
    entries: Sequence[tuple[tarfile.TarInfo, bytes | None]],
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for info, payload in entries:
            archive.addfile(info, None if payload is None else io.BytesIO(payload))


def _regular_tar_info(name: str, payload: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    return info


class ArchivePathTests(unittest.TestCase):
    def test_accepts_one_normal_relative_path(self) -> None:
        self.assertEqual(distribution._safe_path("a/b.py"), "a/b.py")

    def test_rejects_nonportable_or_ambiguous_paths(self) -> None:
        rejected = (
            "",
            "/absolute",
            "../escape",
            "a/../escape",
            "a/./b",
            "a//b",
            "a\\b",
            "C:/drive",
            "trailing.",
            "trailing ",
            "a\x00b",
            "a\nb",
            "e\u0301.txt",
            "/".join("x" for _ in range(distribution.MAX_PATH_DEPTH + 1)),
            "x" * (distribution.MAX_PATH_CHARS + 1),
        )
        for path in rejected:
            with (
                self.subTest(path=path),
                self.assertRaises(distribution.DistributionVerificationError),
            ):
                distribution._safe_path(path)

    def test_rejects_exact_and_nfkc_casefold_collisions(self) -> None:
        exact: set[str] = set()
        portable: dict[str, str] = {}
        distribution._register_path("Alpha", exact=exact, portable=portable)
        for collision in ("Alpha", "alpha", "\uff21lpha"):
            with (
                self.subTest(path=collision),
                self.assertRaises(distribution.DistributionVerificationError),
            ):
                distribution._register_path(
                    collision,
                    exact=exact,
                    portable=portable,
                )


class TarBoundaryTests(unittest.TestCase):
    def test_reads_a_bounded_regular_sdist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "sample.tar.gz"
            payload = b"reviewed"
            directory = tarfile.TarInfo("unitsentinel-0.1.0")
            directory.type = tarfile.DIRTYPE
            _write_tar(
                archive,
                [
                    (directory, None),
                    (
                        _regular_tar_info(
                            "unitsentinel-0.1.0/README.md",
                            payload,
                        ),
                        payload,
                    ),
                ],
            )
            self.assertEqual(
                distribution._read_sdist(archive),
                {"unitsentinel-0.1.0/README.md": payload},
            )

    def test_rejects_links_and_special_members(self) -> None:
        for member_type in (
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.FIFOTYPE,
            tarfile.CHRTYPE,
            tarfile.BLKTYPE,
        ):
            with (
                self.subTest(member_type=member_type),
                tempfile.TemporaryDirectory() as tmp,
            ):
                archive = Path(tmp) / "unsafe.tar.gz"
                info = tarfile.TarInfo("unitsentinel-0.1.0/member")
                info.type = member_type
                _write_tar(archive, [(info, None)])
                with self.assertRaises(distribution.DistributionVerificationError):
                    distribution._read_sdist(archive)

    def test_rejects_traversal_and_duplicate_members(self) -> None:
        cases = (
            [(_regular_tar_info("../escape", b"x"), b"x")],
            [
                (_regular_tar_info("root/file", b"x"), b"x"),
                (_regular_tar_info("root/file", b"y"), b"y"),
            ],
        )
        for entries in cases:
            with (
                self.subTest(entries=len(entries)),
                tempfile.TemporaryDirectory() as tmp,
            ):
                archive = Path(tmp) / "unsafe.tar.gz"
                _write_tar(archive, entries)
                with self.assertRaises(distribution.DistributionVerificationError):
                    distribution._read_sdist(archive)

    def test_rejects_expansion_beyond_the_configured_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "bomb.tar.gz"
            info = _regular_tar_info("root/payload", b"12345")
            _write_tar(archive, [(info, b"12345")])
            with (
                mock.patch.object(distribution, "MAX_EXPANDED_BYTES", 4),
                self.assertRaises(distribution.DistributionVerificationError),
            ):
                distribution._read_sdist(archive)

    def test_canonical_sdist_is_order_independent_and_has_fixed_time(self) -> None:
        first = distribution._canonical_sdist({"root/b": b"two", "root/a": b"one"})
        second = distribution._canonical_sdist({"root/a": b"one", "root/b": b"two"})
        self.assertEqual(first, second)
        self.assertEqual(
            struct.unpack("<I", first[4:8])[0],
            distribution.SOURCE_DATE_EPOCH,
        )

    def test_extracts_only_an_already_validated_exact_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            source = distribution._extract_sdist(
                {"unitsentinel-0.1.0/README.md": b"reviewed"},
                destination,
            )
            self.assertEqual((source / "README.md").read_bytes(), b"reviewed")
            with self.assertRaises(distribution.DistributionVerificationError):
                distribution._extract_sdist(
                    {"other-0.1.0/README.md": b"wrong"},
                    destination / "other",
                )


class ZipBoundaryTests(unittest.TestCase):
    def test_reads_regular_zip_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "sample.whl"
            _write_zip(archive, [("package/__init__.py", b"reviewed")])
            self.assertEqual(
                distribution._read_wheel(archive, label="sample"),
                {"package/__init__.py": b"reviewed"},
            )

    def test_rejects_duplicate_and_portable_collision_paths(self) -> None:
        cases = (
            [("package/file", b"a"), ("package/file", b"b")],
            [("package/File", b"a"), ("package/file", b"b")],
        )
        for entries in cases:
            with (
                self.subTest(entries=entries),
                tempfile.TemporaryDirectory() as temporary,
            ):
                archive = Path(temporary) / "unsafe.whl"
                with mock.patch("warnings.warn"):
                    _write_zip(archive, entries)
                with self.assertRaises(distribution.DistributionVerificationError):
                    distribution._read_wheel(archive, label="sample")

    def test_rejects_zip_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "symlink.whl"
            info = zipfile.ZipInfo("package/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            _write_zip(archive, [(info, b"target")])
            with self.assertRaises(distribution.DistributionVerificationError):
                distribution._read_wheel(archive, label="sample")

    def test_rejects_encrypted_flag_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "encrypted.whl"
            _write_zip(archive, [("package/file", b"payload")])
            raw = bytearray(archive.read_bytes())
            local = raw.index(b"PK\x03\x04")
            central = raw.index(b"PK\x01\x02")
            raw[local + 6 : local + 8] = (1).to_bytes(2, "little")
            raw[central + 8 : central + 10] = (1).to_bytes(2, "little")
            archive.write_bytes(raw)
            with self.assertRaises(distribution.DistributionVerificationError):
                distribution._read_wheel(archive, label="sample")

    def test_rejects_zip_expansion_beyond_the_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "bomb.whl"
            _write_zip(archive, [("package/file", b"12345")])
            with (
                mock.patch.object(distribution, "MAX_EXPANDED_BYTES", 4),
                self.assertRaises(distribution.DistributionVerificationError),
            ):
                distribution._read_wheel(archive, label="sample")


class RecordTests(unittest.TestCase):
    def _valid_payloads(self) -> tuple[dict[str, bytes], str]:
        record_path = "sample-1.0.dist-info/RECORD"
        payloads = {"sample.py": b"value = 1\n"}
        payloads[record_path] = _record(payloads, record_path)
        return payloads, record_path

    def test_accepts_a_closed_sha256_record(self) -> None:
        payloads, record_path = self._valid_payloads()
        distribution._validate_record(payloads, record_path)

    def test_rejects_missing_extra_and_bad_record_rows(self) -> None:
        payloads, record_path = self._valid_payloads()
        mutations = []
        missing = dict(payloads)
        missing[record_path] = b"sample.py,,\n"
        mutations.append(missing)
        extra = dict(payloads)
        extra[record_path] += b"ghost.py,,\n"
        mutations.append(extra)
        bad_digest = dict(payloads)
        bad_digest[record_path] = bad_digest[record_path].replace(
            b"sha256=", b"sha256=x"
        )
        mutations.append(bad_digest)
        bad_size = dict(payloads)
        bad_size[record_path] = bad_size[record_path].replace(b",10\n", b",9\n")
        mutations.append(bad_size)
        duplicate = dict(payloads)
        duplicate[record_path] += duplicate[record_path].splitlines(keepends=True)[0]
        mutations.append(duplicate)
        self_hashed = dict(payloads)
        self_hashed[record_path] = self_hashed[record_path].replace(
            f"{record_path},,\n".encode(),
            f"{record_path},sha256=bad,1\n".encode(),
        )
        mutations.append(self_hashed)
        for mutation in mutations:
            with (
                self.subTest(record=mutation[record_path]),
                self.assertRaises(distribution.DistributionVerificationError),
            ):
                distribution._validate_record(mutation, record_path)


class MetadataAndSurfaceTests(unittest.TestCase):
    def test_metadata_description_preserves_utf8_bytes(self) -> None:
        readme = "# Exact Δ dimensional contract\n".encode()
        payload = _metadata_payload(readme)
        self.assertEqual(distribution._metadata_description_bytes(payload), readme)

    def test_pyproject_declares_the_exact_backend_and_runtime_contract(self) -> None:
        payload = (distribution.ROOT / "pyproject.toml").read_bytes()
        distribution._validate_pyproject(payload)
        for drifted in (
            payload.replace(b"setuptools==83.0.0", b"setuptools>=83", 1),
            payload.replace(b"z3-solver==4.16.0.0", b"z3-solver>=4", 1),
            payload.replace(b"license-files = []", b'license-files = ["LICENSE"]', 1),
        ):
            with self.assertRaises(distribution.DistributionVerificationError):
                distribution._validate_pyproject(drifted)

    def test_accepts_exact_reviewed_metadata_without_a_license_claim(self) -> None:
        metadata = distribution._validate_core_metadata(
            _metadata_payload(),
            label="test",
        )
        self.assertEqual(metadata["Name"], "unitsentinel")

    def test_rejects_licenses_and_dependency_drift(self) -> None:
        licensed = _metadata_payload().replace(
            b"Version: 0.1.0\n",
            b"Version: 0.1.0\nLicense-Expression: MIT\n",
        )
        drifted = _metadata_payload().replace(
            b"Requires-Dist: z3-solver==4.16.0.0\n",
            b"Requires-Dist: z3-solver>=4\n",
        )
        duplicate_requirement = _metadata_payload().replace(
            b"Requires-Dist: z3-solver==4.16.0.0\n",
            b"Requires-Dist: z3-solver==4.16.0.0\n" * 2,
        )
        duplicate_url = _metadata_payload().replace(
            b"Project-URL: Repository, https://github.com/omar07ibrahim/units\n",
            b"Project-URL: Repository, https://github.com/omar07ibrahim/units\n" * 2,
        )
        wrong_extra = _metadata_payload().replace(
            b"Provides-Extra: dev\n",
            b"Provides-Extra: release\n",
        )
        for payload in (
            licensed,
            drifted,
            duplicate_requirement,
            duplicate_url,
            wrong_extra,
        ):
            with (
                self.subTest(payload=payload[:100]),
                self.assertRaises(distribution.DistributionVerificationError),
            ):
                distribution._validate_core_metadata(payload, label="test")

    def _sdist_fixture(self) -> tuple[dict[str, bytes], dict[str, bytes]]:
        readme = "# UnitSentinel Δ test\n".encode()
        metadata = _metadata_payload(readme)
        tracked = {"README.md": readme}
        relative = {path: b"" for path in distribution.GENERATED_SDIST_FILES}
        relative["README.md"] = readme
        relative["PKG-INFO"] = metadata
        relative["src/unitsentinel.egg-info/PKG-INFO"] = metadata
        sources = sorted(set(relative) - {"PKG-INFO", "setup.cfg"})
        relative["src/unitsentinel.egg-info/SOURCES.txt"] = (
            "\n".join(sources) + "\n"
        ).encode()
        payloads = {
            f"{distribution.SDIST_ROOT}/{path}": payload
            for path, payload in relative.items()
        }
        return payloads, tracked

    def test_sdist_closes_surfaces_and_preserves_utf8_description(self) -> None:
        payloads, tracked = self._sdist_fixture()
        metadata = distribution._validate_sdist(payloads, tracked)
        self.assertEqual(metadata, _metadata_payload(tracked["README.md"]))

    def test_sdist_rejects_missing_extra_and_changed_source_bytes(self) -> None:
        payloads, tracked = self._sdist_fixture()
        mutations = []
        missing = dict(payloads)
        del missing[f"{distribution.SDIST_ROOT}/README.md"]
        mutations.append(missing)
        extra = dict(payloads)
        extra[f"{distribution.SDIST_ROOT}/surprise.txt"] = b"surprise"
        mutations.append(extra)
        changed = dict(payloads)
        changed[f"{distribution.SDIST_ROOT}/README.md"] = b"changed"
        mutations.append(changed)
        for mutation in mutations:
            with self.assertRaises(distribution.DistributionVerificationError):
                distribution._validate_sdist(mutation, tracked)

    def _own_wheel_fixture(
        self,
        directory: Path,
        *,
        source: dict[str, bytes] | None = None,
        tag: str = "py3-none-any",
    ) -> tuple[Path, dict[str, bytes], bytes]:
        source_payloads = source or {
            "src/unitsentinel/__init__.py": b"__version__ = '0.1.0'\n",
            "src/unitsentinel/py.typed": b"",
        }
        metadata = _metadata_payload()
        payloads = {
            path.removeprefix("src/"): payload
            for path, payload in source_payloads.items()
        }
        payloads.update(
            {
                f"{distribution.DIST_INFO}/METADATA": metadata,
                f"{distribution.DIST_INFO}/WHEEL": (
                    "Wheel-Version: 1.0\n"
                    "Generator: setuptools (83.0.0)\n"
                    "Root-Is-Purelib: true\n"
                    f"Tag: {tag}\n"
                ).encode(),
                f"{distribution.DIST_INFO}/entry_points.txt": (
                    b"[console_scripts]\nunitsentinel = unitsentinel.cli:main\n"
                ),
                f"{distribution.DIST_INFO}/top_level.txt": b"unitsentinel\n",
            }
        )
        wheel = directory / distribution.WHEEL_NAME
        _write_wheel(wheel, payloads, f"{distribution.DIST_INFO}/RECORD")
        return wheel, source_payloads, metadata

    def test_own_wheel_matches_the_sdist_and_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wheel, source, metadata = self._own_wheel_fixture(Path(temporary))
            payloads = distribution._validate_own_wheel(
                wheel,
                source_payloads=source,
                sdist_metadata=metadata,
            )
            self.assertIn("unitsentinel/__init__.py", payloads)

    def test_own_wheel_rejects_native_payload_and_wrong_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native_source = {"src/unitsentinel/native.so": b"\x7fELF" + b"x" * 20}
            wheel, source, metadata = self._own_wheel_fixture(
                root,
                source=native_source,
            )
            with self.assertRaises(distribution.DistributionVerificationError):
                distribution._validate_own_wheel(
                    wheel,
                    source_payloads=source,
                    sdist_metadata=metadata,
                )
        with tempfile.TemporaryDirectory() as temporary:
            wheel, source, metadata = self._own_wheel_fixture(
                Path(temporary),
                tag="cp312-cp312-linux_x86_64",
            )
            with self.assertRaises(distribution.DistributionVerificationError):
                distribution._validate_own_wheel(
                    wheel,
                    source_payloads=source,
                    sdist_metadata=metadata,
                )

    def test_native_signatures_and_x86_64_elf_header_are_explicit(self) -> None:
        header = bytearray(20)
        header[:4] = b"\x7fELF"
        header[4] = 2
        header[5] = 1
        header[18:20] = (62).to_bytes(2, "little")
        self.assertTrue(distribution._is_x86_64_elf(bytes(header)))
        self.assertFalse(distribution._is_x86_64_elf(bytes(header[:10])))
        self.assertTrue(distribution._is_native_payload("module.pyd", b"plain"))
        self.assertTrue(distribution._is_native_payload("module", b"MZpayload"))
        self.assertFalse(distribution._is_native_payload("module.py", b"print()"))


class ProcessBoundaryTests(unittest.TestCase):
    def _environment(self, root: Path) -> dict[str, str]:
        return distribution._closed_environment(root / "home", root / "tmp")

    def test_bounded_process_returns_exact_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed = distribution._bounded_process(
                (
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import sys; print('out'); print('err', file=sys.stderr)",
                ),
                cwd=root,
                environment=self._environment(root),
                timeout=5.0,
                label="fixture",
            )
            self.assertEqual(completed.stdout, b"out\n")
            self.assertEqual(completed.stderr, b"err\n")

    def test_bounded_process_rejects_failure_timeout_and_output_overflow(self) -> None:
        commands = (
            ((sys.executable, "-I", "-c", "raise SystemExit(7)"), 5.0, None),
            ((sys.executable, "-I", "-c", "import time; time.sleep(2)"), 0.05, None),
            ((sys.executable, "-I", "-c", "print('x' * 100)"), 5.0, 16),
        )
        for command, timeout, output_bound in commands:
            with (
                self.subTest(command=command),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                patcher = (
                    mock.patch.object(distribution, "MAX_OUTPUT_BYTES", output_bound)
                    if output_bound is not None
                    else mock.patch.object(
                        distribution,
                        "MAX_OUTPUT_BYTES",
                        distribution.MAX_OUTPUT_BYTES,
                    )
                )
                with (
                    patcher,
                    self.assertRaises(distribution.DistributionVerificationError),
                ):
                    distribution._bounded_process(
                        command,
                        cwd=root,
                        environment=self._environment(root),
                        timeout=timeout,
                        label="fixture",
                    )


class LockedDependencyTests(unittest.TestCase):
    def test_lock_bytes_and_expected_artifact_are_exact(self) -> None:
        self.assertEqual(
            distribution.LOCK_TEXT,
            "z3-solver==4.16.0.0 \\\n"
            "    --hash=sha256:"
            "afae2551f795670f0522cfce82132d129c408a2694adff71eb01ba0f2ece44f9\n",
        )
        self.assertEqual(distribution.Z3_SIZE, 31_741_807)
        self.assertEqual(
            distribution.Z3_WHEEL_NAME,
            "z3_solver-4.16.0.0-py3-none-manylinux_2_27_x86_64.whl",
        )

    def test_z3_outer_contract_rejects_an_unlocked_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            impostor = Path(temporary) / distribution.Z3_WHEEL_NAME
            impostor.write_bytes(b"not the reviewed wheel")
            with self.assertRaises(distribution.DistributionVerificationError):
                distribution._validate_z3_wheel(impostor)

    def test_relative_wheelhouse_resolves_before_isolated_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wheelhouse = Path(temporary)
            expected = wheelhouse / distribution.Z3_WHEEL_NAME
            expected.write_bytes(b"fixture")
            relative = Path(os.path.relpath(wheelhouse, start=Path.cwd()))

            actual = distribution._locked_z3_wheel(relative)

            self.assertTrue(actual.is_absolute())
            self.assertEqual(actual, expected.resolve(strict=True))


if __name__ == "__main__":
    unittest.main()
