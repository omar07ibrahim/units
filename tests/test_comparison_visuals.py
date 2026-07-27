from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from tools.evidence import comparison_visuals
from tools.evidence.generate import EvidenceError
from unitsentinel.canonical import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"


def svg_root(path: Path) -> ET.Element:
    return ET.fromstring(path.read_bytes())


class ComparisonVisualTests(unittest.TestCase):
    def test_generator_reproduces_every_committed_visual_source(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.evidence.comparison_visuals",
                "--check",
            ],
            cwd=ROOT,
            env={
                "HOME": str(ROOT / ".unitsentinel" / "visual-test-home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
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
                for path in comparison_visuals.EXPECTED_OUTPUT_PATHS
            },
            {
                "docs/assets/compare-compatible-terminal.svg",
                "docs/assets/compare-drift-terminal.svg",
                "docs/assets/compare-indeterminate-terminal.svg",
                "docs/assets/comparison-artifact-sizes.svg",
                "docs/assets/comparison-lineage-drift.svg",
                "docs/assets/comparison-workflow.svg",
                "docs/evidence/comparison-demo/frame-compatible.svg",
                "docs/evidence/comparison-demo/frame-drift.svg",
                "docs/evidence/comparison-demo/frame-indeterminate.svg",
                "docs/evidence/comparison-demo/frames.json",
            },
        )
        with self.assertRaisesRegex(EvidenceError, "allowlist"):
            comparison_visuals._write_files({})

    def test_terminal_sources_show_every_committed_line(self) -> None:
        for name in comparison_visuals.CASE_NAMES:
            with self.subTest(case=name):
                terminal_path = comparison_visuals.TERMINAL_SVG_PATHS[name]
                root = svg_root(terminal_path)
                self.assertEqual(root.attrib["width"], "1440")
                self.assertEqual(root.attrib["height"], "1100")
                source_lines = (
                    comparison_visuals.TRANSCRIPT_PATHS[name].read_text().splitlines()
                )
                rendered: dict[int, list[str]] = {}
                for element in root.iter(f"{SVG_NAMESPACE}text"):
                    source_line = element.attrib.get("data-source-line")
                    if source_line is None:
                        continue
                    rendered.setdefault(int(source_line), []).append(element.text or "")
                reconstructed = [
                    "".join(rendered[index])
                    for index in range(1, len(source_lines) + 1)
                ]
                self.assertEqual(reconstructed, source_lines)
                self.assertEqual(
                    len(reconstructed),
                    comparison_visuals.EXPECTED_LINE_COUNTS[name],
                )

    def test_demo_frames_are_exact_terminal_bytes(self) -> None:
        payload = comparison_visuals.FRAME_MANIFEST_PATH.read_bytes()
        manifest = json.loads(payload)
        self.assertEqual(payload, canonical_json_bytes(manifest) + b"\n")
        self.assertEqual(
            manifest,
            {
                "frames": [
                    {"delay_ms": 3_000, "path": "frame-compatible.svg"},
                    {"delay_ms": 3_000, "path": "frame-drift.svg"},
                    {"delay_ms": 3_000, "path": "frame-indeterminate.svg"},
                ],
                "schema": "unitsentinel.comparison-demo-frames/v1",
            },
        )
        for name in comparison_visuals.CASE_NAMES:
            with self.subTest(case=name):
                self.assertEqual(
                    comparison_visuals.FRAME_SVG_PATHS[name].read_bytes(),
                    comparison_visuals.TERMINAL_SVG_PATHS[name].read_bytes(),
                )

    def test_every_svg_is_accessible_and_self_contained(self) -> None:
        paths = (
            comparison_visuals.WORKFLOW_SVG_PATH,
            comparison_visuals.LINEAGE_SVG_PATH,
            comparison_visuals.SIZES_SVG_PATH,
            *comparison_visuals.TERMINAL_SVG_PATHS.values(),
            *comparison_visuals.FRAME_SVG_PATHS.values(),
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                payload = path.read_bytes()
                root = ET.fromstring(payload)
                self.assertEqual(root.attrib["role"], "img")
                self.assertEqual(root.attrib["aria-labelledby"], "title description")
                title = root.find(f"{SVG_NAMESPACE}title")
                description = root.find(f"{SVG_NAMESPACE}desc")
                self.assertIsNotNone(title)
                self.assertIsNotNone(description)
                assert title is not None
                assert description is not None
                self.assertTrue(title.text)
                self.assertTrue(description.text)
                lowered = payload.lower()
                for forbidden in (
                    b"<image",
                    b"<script",
                    b"<foreignobject",
                    b" href=",
                    b"url(http",
                    b"url('http",
                    b'url("http',
                ):
                    self.assertNotIn(forbidden, lowered)

    def test_workflow_makes_security_and_durability_order_visible(self) -> None:
        root = svg_root(comparison_visuals.WORKFLOW_SVG_PATH)
        visible = " ".join(root.itertext())
        ordered_labels = (
            "1 · Validate limits",
            "2 · Pin raw plan",
            "3 · Strict plan decode",
            "4 · Registry gate",
            "5a · Training graph",
            "5b · Serving graph",
            "6 · Two fresh verifications",
            "7 · Interfaces + lineage",
            "8 · Strict result",
            "9 · Optional output",
            "10 · Stdout + exit",
        )
        positions = [visible.index(label) for label in ordered_labels]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("checked before graph reads", visible)
        self.assertIn("plan SHA-256 before decode", visible)
        self.assertIn("if --result is requested", visible)
        self.assertIn("only after output commit", visible)

    def test_lineage_figure_uses_real_content_semantic_and_output_digests(
        self,
    ) -> None:
        sources = comparison_visuals._load_sources()
        by_name = {case.name: case for case in sources.cases}
        compatible = comparison_visuals._lineage_values(by_name["compatible"])
        drift = comparison_visuals._lineage_values(by_name["drift"])

        self.assertNotEqual(
            compatible["content_training"],
            compatible["content_serving"],
        )
        self.assertEqual(
            compatible["semantic_training"],
            compatible["semantic_serving"],
        )
        self.assertEqual(
            compatible["output_training"],
            compatible["output_serving"],
        )
        self.assertNotEqual(drift["content_training"], drift["content_serving"])
        self.assertNotEqual(drift["semantic_training"], drift["semantic_serving"])
        self.assertNotEqual(drift["output_training"], drift["output_serving"])

        payload = comparison_visuals.LINEAGE_SVG_PATH.read_text()
        for digest in (*compatible.values(), *drift.values()):
            self.assertIn(digest[:32], payload)
            self.assertIn(digest[32:], payload)
        self.assertIn("Content lineage SHA-256", payload)
        self.assertIn("Semantic lineage SHA-256", payload)
        self.assertIn("Output normalization SHA-256", payload)

    def test_size_figure_uses_only_exact_recorded_bytes(self) -> None:
        sources = comparison_visuals._load_sources()
        payload = comparison_visuals.SIZES_SVG_PATH.read_text()
        for case in sources.cases:
            row = comparison_visuals._size_row(case)
            for field in (
                "capture_json_bytes",
                "capture_text_bytes",
                "claim_bytes",
                "plan_bytes",
                "serving_graph_bytes",
                "training_graph_bytes",
            ):
                self.assertIn(f"{row[field]:,} B", payload)
        self.assertIn(comparison_visuals.MEASUREMENT_SCOPE, payload)
        self.assertIn("not latency, throughput, or memory measurements", payload)
        self.assertNotIn("faster", payload.lower())
        self.assertNotIn("elapsed", payload.lower())

    def test_source_loader_rejects_a_noncanonical_result_claim(self) -> None:
        original_reader = comparison_visuals._read_regular_file
        target = comparison_visuals.CLAIM_PATHS["compatible"]

        def tampered_reader(path: Path, *, purpose: str) -> bytes:
            payload = original_reader(path, purpose=purpose)
            return payload + b"\n" if path == target else payload

        with (
            patch.object(
                comparison_visuals,
                "_read_regular_file",
                side_effect=tampered_reader,
            ),
            self.assertRaisesRegex(EvidenceError, "strictly decodable"),
        ):
            comparison_visuals._load_sources()

    def test_source_loader_rejects_forged_text_with_updated_provenance(
        self,
    ) -> None:
        original_reader = comparison_visuals._read_regular_file
        transcript_path = comparison_visuals.TRANSCRIPT_PATHS["compatible"]
        transcript = transcript_path.read_bytes()
        forged = transcript.replace(
            b"tool: unitsentinel 0.1.0",
            b"tool: FORGED-CLAIM 0.1.0",
        )
        self.assertEqual(len(forged), len(transcript))
        self.assertNotEqual(forged, transcript)

        provenance = json.loads(comparison_visuals.PROVENANCE_PATH.read_bytes())
        provenance["cases"][0]["captures"]["text"]["sha256"] = hashlib.sha256(
            forged
        ).hexdigest()
        forged_provenance = canonical_json_bytes(provenance) + b"\n"

        def tampered_reader(path: Path, *, purpose: str) -> bytes:
            if path == transcript_path:
                return forged
            if path == comparison_visuals.PROVENANCE_PATH:
                return forged_provenance
            return original_reader(path, purpose=purpose)

        with (
            patch.object(
                comparison_visuals,
                "_read_regular_file",
                side_effect=tampered_reader,
            ),
            self.assertRaisesRegex(EvidenceError, "exact bound report"),
        ):
            comparison_visuals._load_sources()

    def test_source_loader_rejects_forged_json_with_updated_provenance(
        self,
    ) -> None:
        original_reader = comparison_visuals._read_regular_file
        capture_path = comparison_visuals.JSON_CAPTURE_PATHS["compatible"]
        capture = json.loads(capture_path.read_bytes())
        capture["tool"]["name"] = "FORGED-CLAIM"
        forged = canonical_json_bytes(capture) + b"\n"
        self.assertEqual(len(forged), capture_path.stat().st_size)

        provenance = json.loads(comparison_visuals.PROVENANCE_PATH.read_bytes())
        provenance["cases"][0]["captures"]["json"]["sha256"] = hashlib.sha256(
            forged
        ).hexdigest()
        forged_provenance = canonical_json_bytes(provenance) + b"\n"

        def tampered_reader(path: Path, *, purpose: str) -> bytes:
            if path == capture_path:
                return forged
            if path == comparison_visuals.PROVENANCE_PATH:
                return forged_provenance
            return original_reader(path, purpose=purpose)

        with (
            patch.object(
                comparison_visuals,
                "_read_regular_file",
                side_effect=tampered_reader,
            ),
            self.assertRaisesRegex(EvidenceError, "tool identity"),
        ):
            comparison_visuals._load_sources()

    def test_source_loader_rejects_unbound_registry_provenance(self) -> None:
        original_reader = comparison_visuals._read_regular_file
        provenance = json.loads(comparison_visuals.PROVENANCE_PATH.read_bytes())
        provenance["registry"]["sha256"] = "0" * 64
        forged_provenance = canonical_json_bytes(provenance) + b"\n"

        def tampered_reader(path: Path, *, purpose: str) -> bytes:
            if path == comparison_visuals.PROVENANCE_PATH:
                return forged_provenance
            return original_reader(path, purpose=purpose)

        with (
            patch.object(
                comparison_visuals,
                "_read_regular_file",
                side_effect=tampered_reader,
            ),
            self.assertRaisesRegex(EvidenceError, "trust bindings"),
        ):
            comparison_visuals._load_sources()


if __name__ == "__main__":
    unittest.main()
