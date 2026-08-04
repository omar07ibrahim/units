from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from tools import verify_distribution as distribution
from tools.evidence import distribution_visuals
from tools.evidence.generate import EvidenceError
from tools.evidence.visuals import (
    distribution_contract_svg,
    distribution_terminal_svg,
)
from unitsentinel.canonical import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"


def svg_root(path: Path) -> ET.Element:
    return ET.fromstring(path.read_bytes())


def nested_key_paths(
    value: object, prefix: tuple[str, ...] = ()
) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise AssertionError("canonical evidence object key is not a string")
            path = (*prefix, key)
            paths.add(path)
            paths.update(nested_key_paths(child, path))
    elif type(value) is list:
        for child in value:
            paths.update(nested_key_paths(child, prefix))
    return paths


def build_contract_visual(
    *,
    z3_sha256: str = distribution.Z3_SHA256,
    z3_elf_paths: tuple[str, ...] = distribution.Z3_ELF_PATHS,
    pip_flags: tuple[str, ...] = distribution_visuals.OFFLINE_PIP_FLAGS,
) -> str:
    return distribution_contract_svg(
        backend=distribution.EXPECTED_BACKEND,
        sdist_filename=f"{distribution.SDIST_ROOT}.tar.gz",
        wheel_filename=distribution.WHEEL_NAME,
        wheel_tag=distribution.WHEEL_TAG,
        z3_filename=distribution.Z3_WHEEL_NAME,
        z3_sha256=z3_sha256,
        z3_size_bytes=distribution.Z3_SIZE,
        z3_tag=distribution.Z3_WHEEL_TAG,
        z3_elf_paths=z3_elf_paths,
        pip_flags=pip_flags,
        import_smoke=distribution.IMPORT_SMOKE_STDOUT.rstrip("\n"),
        console_smoke=distribution.CONSOLE_SMOKE_STDOUT.rstrip("\n"),
    )


class DistributionVisualTests(unittest.TestCase):
    def test_generator_reproduces_the_committed_slice(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.evidence.distribution_visuals",
                "--check",
            ],
            cwd=ROOT,
            env={
                "HOME": str(ROOT / ".unitsentinel" / "distribution-visual-home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONPATH": str(ROOT / "src"),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

    def test_generator_has_one_closed_output_allowlist(self) -> None:
        self.assertEqual(
            {
                path.relative_to(ROOT).as_posix()
                for path in distribution_visuals.EXPECTED_OUTPUT_PATHS
            },
            {
                "docs/assets/distribution-contract.svg",
                "docs/assets/distribution-terminal.svg",
                "docs/evidence/captures/distribution.txt",
                "docs/evidence/data/distribution-contract.json",
            },
        )
        with self.assertRaisesRegex(EvidenceError, "allowlist"):
            distribution_visuals._write_files({})

    def test_contract_is_canonical_and_bound_to_verifier_constants(self) -> None:
        payload = distribution_visuals.CONTRACT_PATH.read_bytes()
        contract = json.loads(payload)
        self.assertEqual(payload, canonical_json_bytes(contract) + b"\n")
        self.assertEqual(contract, distribution_visuals._expected_contract())

        self.assertEqual(
            contract["backend"]["build_backend"], distribution.BUILD_BACKEND
        )
        self.assertEqual(
            contract["backend"]["requirement"], distribution.EXPECTED_BACKEND
        )
        self.assertEqual(
            contract["reproducibility"]["wheel"]["tag"], distribution.WHEEL_TAG
        )
        self.assertEqual(contract["z3_solver"]["filename"], distribution.Z3_WHEEL_NAME)
        self.assertEqual(contract["z3_solver"]["sha256"], distribution.Z3_SHA256)
        self.assertEqual(contract["z3_solver"]["size_bytes"], distribution.Z3_SIZE)
        self.assertEqual(contract["z3_solver"]["tag"], distribution.Z3_WHEEL_TAG)
        self.assertEqual(
            tuple(contract["z3_solver"]["elf_paths"]),
            distribution.Z3_ELF_PATHS,
        )

    def test_contract_has_only_the_dependency_digest_and_no_file_counts(self) -> None:
        contract = json.loads(distribution_visuals.CONTRACT_PATH.read_bytes())
        paths = nested_key_paths(contract)
        digest_paths = {path for path in paths if path[-1] in {"digest", "sha256"}}
        self.assertEqual(digest_paths, {("z3_solver", "sha256")})
        forbidden = {
            "capture_sha256",
            "contract_sha256",
            "file_count",
            "member_count",
            "self_digest",
            "source_file_count",
        }
        self.assertFalse({path[-1] for path in paths} & forbidden)

    def test_transcript_is_the_complete_stable_hosted_report(self) -> None:
        transcript = distribution_visuals.TRANSCRIPT_PATH.read_text(encoding="utf-8")
        self.assertEqual(transcript, distribution_visuals._expected_transcript())
        self.assertEqual(len(transcript.splitlines()), 9)
        self.assertIn(distribution.DISTRIBUTION_SUCCESS_TEXT, transcript)
        self.assertTrue(transcript.endswith("[exit 0]\n"))
        self.assertNotRegex(transcript, re.compile(r"[0-9a-f]{64}"))
        for forbidden in (
            "/home/runner/",
            "actions/jobs/",
            "run id",
            "2026-",
        ):
            self.assertNotIn(forbidden, transcript.casefold())

    def test_build_is_byte_deterministic(self) -> None:
        first = distribution_visuals._build_files(use_committed_sources=True)
        second = distribution_visuals._build_files(use_committed_sources=True)
        self.assertEqual(first, second)
        for path, expected in first.items():
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual(path.read_bytes(), expected)

    def test_terminal_svg_reconstructs_every_capture_line(self) -> None:
        root = svg_root(distribution_visuals.TERMINAL_SVG_PATH)
        self.assertEqual(root.attrib["width"], "1440")
        self.assertEqual(root.attrib["height"], "520")
        rendered: dict[int, str] = {}
        for element in root.iter(f"{SVG_NAMESPACE}text"):
            source_line = element.attrib.get("data-source-line")
            if source_line is not None:
                rendered[int(source_line)] = element.text or ""
        source_lines = distribution_visuals.TRANSCRIPT_PATH.read_text().splitlines()
        self.assertEqual(
            [rendered[index] for index in range(1, 10)],
            source_lines,
        )

    def test_contract_svg_exposes_exact_release_boundaries(self) -> None:
        root = svg_root(distribution_visuals.CONTRACT_SVG_PATH)
        self.assertEqual(root.attrib["width"], "1440")
        self.assertEqual(root.attrib["height"], "1000")
        payload = distribution_visuals.CONTRACT_SVG_PATH.read_text()
        visible = " ".join(root.itertext())
        for value in (
            distribution.Z3_WHEEL_NAME,
            distribution.Z3_WHEEL_TAG,
            *distribution.Z3_ELF_PATHS,
        ):
            self.assertIn(value, visible)
        self.assertIn(distribution.Z3_SHA256[:32], visible)
        self.assertIn(distribution.Z3_SHA256[32:], visible)
        self.assertIn(f"{distribution.Z3_SIZE:,} bytes", visible)
        self.assertIn("canonical bytes A = B", visible)
        self.assertIn("wheel bytes A = B", visible)
        self.assertIn("NETWORK BOUNDARY", visible)
        self.assertIn("VERIFIED", visible)
        self.assertIn("NOT CLAIMED", visible)
        self.assertNotIn("networking disabled", payload.casefold())

    def test_svgs_are_accessible_and_self_contained(self) -> None:
        for path in (
            distribution_visuals.CONTRACT_SVG_PATH,
            distribution_visuals.TERMINAL_SVG_PATH,
        ):
            with self.subTest(path=path.relative_to(ROOT)):
                payload = path.read_bytes()
                root = ET.fromstring(payload)
                self.assertEqual(root.attrib["role"], "img")
                self.assertEqual(root.attrib["aria-labelledby"], "title description")
                self.assertTrue(root.findtext(f"{SVG_NAMESPACE}title"))
                self.assertTrue(root.findtext(f"{SVG_NAMESPACE}desc"))
                lowered = payload.lower()
                for forbidden in (
                    b"<image",
                    b"<script",
                    b"<foreignobject",
                    b" href=",
                    b"url(http",
                    b"<!doctype",
                    b"<!entity",
                ):
                    self.assertNotIn(forbidden, lowered)

    def test_loader_rejects_canonical_contract_drift(self) -> None:
        original_reader = distribution_visuals._read_regular_file
        contract = json.loads(distribution_visuals.CONTRACT_PATH.read_bytes())
        contract["z3_solver"]["sha256"] = "0" * 64
        forged = canonical_json_bytes(contract) + b"\n"

        def tampered_reader(path: Path, *, purpose: str) -> bytes:
            if path == distribution_visuals.CONTRACT_PATH:
                return forged
            return original_reader(path, purpose=purpose)

        with (
            patch.object(
                distribution_visuals,
                "_read_regular_file",
                side_effect=tampered_reader,
            ),
            self.assertRaisesRegex(EvidenceError, "drifted from the verifier"),
        ):
            distribution_visuals._load_sources()

    def test_loader_rejects_transcript_drift(self) -> None:
        original_reader = distribution_visuals._read_regular_file
        forged = distribution_visuals.TRANSCRIPT_PATH.read_bytes().replace(
            distribution.DISTRIBUTION_SUCCESS_TEXT.encode(),
            b"verified forged distribution",
        )

        def tampered_reader(path: Path, *, purpose: str) -> bytes:
            if path == distribution_visuals.TRANSCRIPT_PATH:
                return forged
            return original_reader(path, purpose=purpose)

        with (
            patch.object(
                distribution_visuals,
                "_read_regular_file",
                side_effect=tampered_reader,
            ),
            self.assertRaisesRegex(EvidenceError, "exact hosted report"),
        ):
            distribution_visuals._load_sources()

    def test_rejects_noncanonical_and_out_of_budget_sources(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "canonical JSON"):
            distribution_visuals._validate_contract_payload(
                distribution_visuals.CONTRACT_PATH.read_bytes() + b"\n"
            )
        with self.assertRaisesRegex(ValueError, "exactly nine lines"):
            distribution_terminal_svg(
                transcript=distribution_visuals._expected_transcript() + "extra\n"
            )

    def test_contract_builder_rejects_incomplete_native_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "three Z3 ELF paths"):
            build_contract_visual(z3_elf_paths=distribution.Z3_ELF_PATHS[:2])
        with self.assertRaisesRegex(ValueError, "exact Z3 wheel identity"):
            build_contract_visual(z3_sha256="not-a-digest")
        with self.assertRaisesRegex(ValueError, "closed offline flag set"):
            build_contract_visual(pip_flags=distribution_visuals.OFFLINE_PIP_FLAGS[:5])


if __name__ == "__main__":
    unittest.main()
