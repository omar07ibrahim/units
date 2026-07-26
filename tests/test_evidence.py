from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import statistics
import struct
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ElementTree
import zlib
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from examples.build_wheel_anomaly_contract import build_graph
from unitsentinel import (
    BUILTIN_REGISTRY,
    VerificationStatus,
    decode_certificate,
    decode_graph,
    encode_graph,
)
from unitsentinel.canonical import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
EVIDENCE = ROOT / "docs" / "evidence"
MANIFEST = EVIDENCE / "manifest.json"
EVIDENCE_README = EVIDENCE / "README.md"

VERIFIED_GRAPH_DIGEST = (
    "139e3e3d99d64c3d9cde89e9e1f116f09452c3532eaaee2e0513c71a0f2ada3c"
)
CONFLICT_GRAPH_DIGEST = (
    "6ae6457c38e5dbe707187031a521e4c76124ee55ac58869a36ba746978a4f708"
)
VERIFIED_RESULT_DIGEST = (
    "f2dce1e2b1e602719d117d05dfe356521bb204039de084beccc68dd8920406bd"
)
CONFLICT_RESULT_DIGEST = (
    "521b85cbf597e5ca45716c7add5346cc65191063060c24c87ca70797bac67aea"
)
CERTIFICATE_DIGEST = "e93cc87cd72c6ede9cf8d324bfb41b2eb2bdcea6cb0aa6fea7aed4696009ab1a"
REPLAY_DIGEST = "aca0b2794371a552a1f1b3af75bdd86b8cf8fb21e5cb664b835b2adf19acf3aa"
REGISTRY_DIGEST = "fc80cbb596f3341b1d2ff13795e50d2d1e05c792b34f24804afc97c3470913e5"

EXPECTED_CONFLICT_CORE = (
    "declaration/acceleration-si/unit",
    "operation/derive-acceleration/dimension",
    "operation/normalize-sample-period/dimension",
    "operation/normalize-speed-delta/dimension",
)
EXPECTED_VISUAL_DIMENSIONS = {
    "certificate-lineage": (1_440, 900),
    "conflict-core": (1_440, 930),
    "conflict-terminal": (1_440, 900),
    "replay-terminal": (1_440, 900),
    "scaling": (1_440, 890),
    "verification-pipeline": (1_440, 890),
    "verify-terminal": (1_440, 900),
    "wheel-anomaly-contract": (1_440, 900),
}
REQUIRED_README_EMBEDS = {
    "docs/assets/certificate-lineage.png",
    "docs/assets/conflict-core.png",
    "docs/assets/conflict-terminal.png",
    "docs/assets/replay-terminal.png",
    "docs/assets/scaling.png",
    "docs/assets/unitsentinel-demo.gif",
    "docs/assets/verification-pipeline.png",
    "docs/assets/verify-terminal.png",
    "docs/assets/wheel-anomaly-contract.png",
}

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
SAFE_SVG_TAGS = {
    "circle",
    "defs",
    "desc",
    "feDropShadow",
    "filter",
    "line",
    "linearGradient",
    "marker",
    "path",
    "polyline",
    "rect",
    "stop",
    "svg",
    "text",
    "title",
    "tspan",
}
MARKDOWN_LINK = re.compile(
    r"(?P<image>!)?\[(?P<label>[^\]]*)\]\(\s*"
    r"(?P<target><[^>\n]+>|[^)\s]+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)",
)
LOCAL_SVG_REFERENCE = re.compile(
    r"url\(#(?P<identifier>[A-Za-z_][A-Za-z0-9_.:-]*)\)",
    re.IGNORECASE,
)
SHA256 = re.compile(r"[0-9a-f]{64}")
CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SECRET_PATTERNS = (
    (
        "absolute user path",
        re.compile(
            r"(?i)(?:/home/[^/\s]+|/users/[^/\s]+|/root(?:/|\b)|"
            r"[a-z]:[\\/]+users[\\/])"
        ),
    ),
    (
        "email address",
        re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b"),
    ),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "GitHub token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|"
            r"github_pat_[A-Za-z0-9_]{20,255})\b"
        ),
    ),
    (
        "OpenAI key",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "JSON Web Token",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\."
            r"[A-Za-z0-9_-]{8,}\b"
        ),
    ),
    (
        "private key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ),
    (
        "bearer credential",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    ),
    (
        "credential-bearing URL",
        re.compile(
            r"(?i)\b(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)"
            r"://[^\s/:@]+:[^\s/@]+@"
        ),
    ),
    (
        "secret assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|"
            r"secret[_-]?access[_-]?key|client[_-]?secret|password|passwd)"
            r"\b[\"']?\s*[:=]\s*[\"']?(?!not[-_])[^ \t\r\n\"',}]{8,}"
        ),
    ),
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GIF_HEADER = b"GIF89a"


def _canonical_document(path: Path, *, transport_newline: bool = True) -> object:
    payload = path.read_bytes()
    document = json.loads(payload)
    suffix = b"\n" if transport_newline else b""
    expected = canonical_json_bytes(document) + suffix
    if payload != expected:
        raise AssertionError(f"{path.relative_to(ROOT)} is not canonical JSON")
    return document


def _repository_files(
    directory: Path,
    *,
    exclude: set[Path] | None = None,
) -> list[Path]:
    excluded = exclude or set()
    files: list[Path] = []
    if not directory.is_dir():
        return files
    for path in directory.rglob("*"):
        if path in excluded:
            continue
        if path.is_symlink():
            raise AssertionError(f"{path.relative_to(ROOT)} is a symbolic link")
        if path.is_file():
            files.append(path)
    return sorted(files)


def _assert_inside_repository(path: Path) -> None:
    try:
        path.resolve().relative_to(ROOT.resolve())
    except ValueError as error:
        raise AssertionError(f"{path} escapes the repository") from error
    current = path
    while current != ROOT:
        if current.is_symlink():
            raise AssertionError(f"{current.relative_to(ROOT)} is a symbolic link")
        current = current.parent


def _parse_png(payload: bytes) -> tuple[int, int]:
    if not payload.startswith(PNG_SIGNATURE):
        raise ValueError("PNG signature is invalid")
    if len(payload) > 67_108_864:
        raise ValueError("PNG exceeds the structural test byte limit")

    offset = len(PNG_SIGNATURE)
    chunk_types: list[bytes] = []
    idat_parts: list[bytes] = []
    header: bytes | None = None
    idat_closed = False

    while offset < len(payload):
        if len(payload) - offset < 12:
            raise ValueError("PNG chunk header is truncated")
        length = struct.unpack_from(">I", payload, offset)[0]
        chunk_end = offset + 12 + length
        if chunk_end > len(payload):
            raise ValueError("PNG chunk payload is truncated")
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_data = payload[offset + 8 : offset + 8 + length]
        recorded_crc = struct.unpack_from(">I", payload, offset + 8 + length)[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFF_FFFF
        if not re.fullmatch(rb"[A-Za-z]{4}", chunk_type):
            raise ValueError("PNG chunk type is malformed")
        if actual_crc != recorded_crc:
            raise ValueError("PNG chunk CRC does not match")

        if chunk_type == b"IHDR":
            if header is not None or chunk_types:
                raise ValueError("PNG IHDR is repeated or out of order")
            if length != 13:
                raise ValueError("PNG IHDR length is invalid")
            header = chunk_data
        elif chunk_type == b"IDAT":
            if idat_closed:
                raise ValueError("PNG IDAT chunks are not consecutive")
            idat_parts.append(chunk_data)
        elif idat_parts:
            idat_closed = True

        chunk_types.append(chunk_type)
        offset = chunk_end
        if chunk_type == b"IEND":
            if length != 0 or offset != len(payload):
                raise ValueError("PNG IEND is invalid or has trailing bytes")
            break

    if header is None:
        raise ValueError("PNG IHDR is missing")
    if not idat_parts:
        raise ValueError("PNG IDAT is missing")
    if chunk_types[-1:] != [b"IEND"]:
        raise ValueError("PNG IEND is missing")
    if chunk_types.count(b"IHDR") != 1 or chunk_types.count(b"IEND") != 1:
        raise ValueError("PNG critical chunks are repeated")

    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB",
        header,
    )
    if (depth, color, compression, filtering, interlace) != (8, 6, 0, 0, 0):
        raise ValueError("PNG format is not non-interlaced 8-bit RGBA")
    if width < 1 or height < 1 or width > 4_096 or height > 4_096:
        raise ValueError("PNG dimensions are out of bounds")
    if width * height > 16_777_216:
        raise ValueError("PNG pixel count is out of bounds")

    expected_bytes = height * (1 + width * 4)
    decompressor = zlib.decompressobj()
    decoded = decompressor.decompress(
        b"".join(idat_parts),
        expected_bytes + 1,
    )
    remaining = expected_bytes + 1 - len(decoded)
    if remaining > 0:
        decoded += decompressor.flush(remaining)
    if (
        len(decoded) != expected_bytes
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise ValueError("PNG decompressed scanlines are malformed")
    stride = 1 + width * 4
    if any(decoded[row * stride] not in range(5) for row in range(height)):
        raise ValueError("PNG scanline filter is invalid")
    return width, height


def _gif_sub_blocks(payload: bytes, offset: int) -> tuple[int, bytes]:
    combined = bytearray()
    while True:
        if offset >= len(payload):
            raise ValueError("GIF sub-block length is truncated")
        length = payload[offset]
        offset += 1
        if length == 0:
            return offset, bytes(combined)
        end = offset + length
        if end > len(payload):
            raise ValueError("GIF sub-block payload is truncated")
        combined.extend(payload[offset:end])
        offset = end


def _parse_gif(
    payload: bytes,
) -> tuple[
    tuple[int, int, int],
    list[tuple[bytes, bytes]],
    list[tuple[int, int, int, int, int, int, int, int]],
]:
    if not payload.startswith(GIF_HEADER) or len(payload) < 14:
        raise ValueError("GIF header is invalid")
    width, height, screen_flags = struct.unpack_from("<HHB", payload, 6)
    if not screen_flags & 0x80:
        raise ValueError("GIF global color table is missing")
    offset = 13 + 3 * (1 << ((screen_flags & 0x07) + 1))
    if offset > len(payload):
        raise ValueError("GIF global color table is truncated")

    applications: list[tuple[bytes, bytes]] = []
    frames: list[tuple[int, int, int, int, int, int, int, int]] = []
    pending_control: tuple[int, int] | None = None
    trailer_seen = False

    while offset < len(payload):
        marker = payload[offset]
        offset += 1
        if marker == 0x3B:
            if offset != len(payload) or pending_control is not None:
                raise ValueError("GIF trailer or pending frame is invalid")
            trailer_seen = True
            break
        if marker == 0x21:
            if offset >= len(payload):
                raise ValueError("GIF extension label is truncated")
            label = payload[offset]
            offset += 1
            if label == 0xF9:
                if pending_control is not None or offset + 6 > len(payload):
                    raise ValueError("GIF graphic control extension is invalid")
                block_length = payload[offset]
                offset += 1
                if block_length != 4:
                    raise ValueError("GIF graphic control block length is invalid")
                control_flags = payload[offset]
                delay = struct.unpack_from("<H", payload, offset + 1)[0]
                offset += 4
                if payload[offset] != 0:
                    raise ValueError("GIF graphic control terminator is invalid")
                offset += 1
                pending_control = (control_flags, delay)
                continue
            if label == 0xFF:
                if offset >= len(payload):
                    raise ValueError("GIF application header is truncated")
                header_length = payload[offset]
                offset += 1
                header_end = offset + header_length
                if header_end > len(payload):
                    raise ValueError("GIF application identifier is truncated")
                identifier = payload[offset:header_end]
                offset, application_data = _gif_sub_blocks(payload, header_end)
                applications.append((identifier, application_data))
                continue
            raise ValueError("GIF contains an unsupported extension")
        if marker != 0x2C:
            raise ValueError("GIF contains an unsupported block")
        if pending_control is None or offset + 9 > len(payload):
            raise ValueError("GIF image descriptor is missing frame control")

        left, top, frame_width, frame_height, image_flags = struct.unpack_from(
            "<HHHHB",
            payload,
            offset,
        )
        offset += 9
        if image_flags & 0x80:
            offset += 3 * (1 << ((image_flags & 0x07) + 1))
        if offset >= len(payload):
            raise ValueError("GIF image data is truncated")
        lzw_minimum = payload[offset]
        offset += 1
        offset, image_data = _gif_sub_blocks(payload, offset)
        if not image_data:
            raise ValueError("GIF image data is empty")
        control_flags, delay = pending_control
        frames.append(
            (
                left,
                top,
                frame_width,
                frame_height,
                image_flags,
                control_flags,
                delay,
                lzw_minimum,
            )
        )
        pending_control = None

    if not trailer_seen:
        raise ValueError("GIF trailer is missing")
    return (width, height, screen_flags), applications, frames


class EvidenceIntegrityTests(unittest.TestCase):
    def test_generator_reproduces_committed_evidence(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.evidence.generate",
                "--check",
            ],
            cwd=ROOT,
            env={
                "HOME": str(ROOT / ".unitsentinel" / "evidence-run"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
                "PYTHONHASHSEED": "0",
                "PYTHONPATH": str(ROOT / "src"),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=60,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

    def test_manifest_is_canonical_closed_and_content_addressed(self) -> None:
        document = _canonical_document(MANIFEST)

        self.assertIs(type(document), dict)
        self.assertEqual(set(document), {"files", "schema"})
        self.assertEqual(
            document["schema"],
            "unitsentinel.evidence-manifest/v1",
        )
        self.assertIs(type(document["files"]), list)
        records = document["files"]
        paths = [record["path"] for record in records]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))

        actual_files = _repository_files(ASSETS) + _repository_files(
            EVIDENCE,
            exclude={MANIFEST},
        )
        actual_paths = {path.relative_to(ROOT).as_posix() for path in actual_files}
        recorded_paths: set[str] = set()
        for record in records:
            with self.subTest(record=record):
                self.assertIs(type(record), dict)
                self.assertEqual(set(record), {"bytes", "path", "sha256"})
                self.assertIs(type(record["bytes"]), int)
                self.assertGreater(record["bytes"], 0)
                self.assertIs(type(record["path"]), str)
                self.assertIs(type(record["sha256"]), str)
                self.assertRegex(record["sha256"], rf"\A{SHA256.pattern}\Z")

                relative = record["path"]
                pure_path = PurePosixPath(relative)
                self.assertEqual(pure_path.as_posix(), relative)
                self.assertFalse(pure_path.is_absolute())
                self.assertNotIn("", pure_path.parts)
                self.assertNotIn(".", pure_path.parts)
                self.assertNotIn("..", pure_path.parts)
                self.assertTrue(relative.startswith(("docs/assets/", "docs/evidence/")))
                path = ROOT / relative
                _assert_inside_repository(path)
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())
                payload = path.read_bytes()
                self.assertEqual(len(payload), record["bytes"])
                self.assertEqual(
                    hashlib.sha256(payload).hexdigest(),
                    record["sha256"],
                )
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o111, 0)
                recorded_paths.add(relative)

        self.assertEqual(recorded_paths, actual_paths)

    def test_semantic_goldens_are_canonical_and_cross_bound(self) -> None:
        graph_cases = {
            "verified": (
                EVIDENCE / "contracts" / "wheel-anomaly-verified.json",
                VERIFIED_GRAPH_DIGEST,
            ),
            "conflict": (
                EVIDENCE / "contracts" / "wheel-anomaly-conflict.json",
                CONFLICT_GRAPH_DIGEST,
            ),
        }
        decoded_graphs = {}
        for variant, (path, digest) in graph_cases.items():
            with self.subTest(graph=variant):
                expected_graph = build_graph(variant)
                payload = path.read_bytes()
                self.assertEqual(payload, encode_graph(expected_graph))
                self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
                decoded = decode_graph(payload)
                self.assertEqual(decoded, expected_graph)
                self.assertEqual(decoded.digest, digest)
                decoded_graphs[variant] = decoded

        certificate_path = EVIDENCE / "claims" / "wheel-anomaly.cert.json"
        certificate_payload = certificate_path.read_bytes()
        certificate = decode_certificate(certificate_payload)
        self.assertEqual(certificate.canonical_bytes(), certificate_payload)
        self.assertEqual(
            hashlib.sha256(certificate_payload).hexdigest(),
            CERTIFICATE_DIGEST,
        )
        self.assertEqual(certificate.digest, CERTIFICATE_DIGEST)
        self.assertEqual(
            certificate.result.status,
            VerificationStatus.VERIFIED,
        )
        self.assertEqual(certificate.result.graph_digest, VERIFIED_GRAPH_DIGEST)
        self.assertEqual(certificate.result.digest, VERIFIED_RESULT_DIGEST)
        self.assertEqual(certificate.result.registry_digest, REGISTRY_DIGEST)
        self.assertEqual(certificate.result.checks_performed, 2)
        self.assertEqual(len(certificate.result.contracts), 10)
        self.assertEqual(len(certificate.constraints), 24)
        self.assertEqual(certificate.registry_version, BUILTIN_REGISTRY.version)

        verify = _canonical_document(
            EVIDENCE / "captures" / "verify.json",
        )
        conflict = _canonical_document(
            EVIDENCE / "captures" / "conflict.json",
        )
        replay = _canonical_document(
            EVIDENCE / "captures" / "replay.json",
        )
        self.assertEqual(verify["schema"], "unitsentinel.cli.verify/v1")
        self.assertEqual(verify["exit_code"], 0)
        self.assertEqual(verify["graph"]["sha256"], VERIFIED_GRAPH_DIGEST)
        self.assertEqual(verify["result"]["sha256"], VERIFIED_RESULT_DIGEST)
        self.assertEqual(verify["result"]["record"]["status"], "verified")
        self.assertEqual(verify["result"]["record"]["checks_performed"], 2)
        self.assertEqual(len(verify["result"]["record"]["contracts"]), 10)
        self.assertEqual(
            verify["certificate"],
            {
                "authentication": "not-provided",
                "schema": "unitsentinel.proof-certificate/v1",
                "sha256": CERTIFICATE_DIGEST,
            },
        )

        conflict_record = conflict["result"]["record"]
        self.assertEqual(conflict["schema"], "unitsentinel.cli.verify/v1")
        self.assertEqual(conflict["exit_code"], 1)
        self.assertEqual(conflict["graph"]["sha256"], CONFLICT_GRAPH_DIGEST)
        self.assertEqual(conflict["result"]["sha256"], CONFLICT_RESULT_DIGEST)
        self.assertEqual(conflict_record["status"], "conflict")
        self.assertIs(conflict_record["core_minimal"], True)
        self.assertEqual(
            tuple(
                witness["constraint_id"] for witness in conflict_record["conflict_core"]
            ),
            EXPECTED_CONFLICT_CORE,
        )
        self.assertIsNone(conflict["certificate"])

        self.assertEqual(replay["schema"], "unitsentinel.cli.replay/v1")
        self.assertEqual(replay["exit_code"], 0)
        self.assertEqual(replay["certificate"]["sha256"], CERTIFICATE_DIGEST)
        self.assertEqual(
            replay["certificate"]["authentication"],
            "not-provided",
        )
        self.assertEqual(replay["graph"]["sha256"], VERIFIED_GRAPH_DIGEST)
        self.assertEqual(replay["report"]["sha256"], REPLAY_DIGEST)
        self.assertEqual(replay["report"]["record"]["status"], "reproduced")
        self.assertIs(replay["report"]["record"]["strict_toolchain"], True)
        self.assertEqual(
            replay["report"]["record"]["fresh_result"]["sha256"],
            VERIFIED_RESULT_DIGEST,
        )

        provenance = _canonical_document(EVIDENCE / "provenance.json")
        self.assertEqual(
            provenance["schema"],
            "unitsentinel.evidence-provenance/v1",
        )
        self.assertEqual(provenance["registry"]["sha256"], REGISTRY_DIGEST)
        self.assertEqual(
            provenance["certificate"],
            {
                "authentication": "not-provided",
                "bytes": len(certificate_payload),
                "sha256": CERTIFICATE_DIGEST,
            },
        )
        self.assertEqual(provenance["verified"]["status"], "verified")
        self.assertEqual(
            provenance["verified"]["graph_sha256"],
            decoded_graphs["verified"].digest,
        )
        self.assertEqual(
            provenance["verified"]["result_sha256"],
            VERIFIED_RESULT_DIGEST,
        )
        self.assertEqual(provenance["verified"]["contracts"], 10)
        self.assertEqual(provenance["conflict"]["status"], "conflict")
        self.assertEqual(
            provenance["conflict"]["graph_sha256"],
            decoded_graphs["conflict"].digest,
        )
        self.assertEqual(
            provenance["conflict"]["result_sha256"],
            CONFLICT_RESULT_DIGEST,
        )
        self.assertEqual(
            tuple(provenance["conflict"]["core"]),
            EXPECTED_CONFLICT_CORE,
        )
        self.assertIs(provenance["conflict"]["core_minimal"], True)
        self.assertEqual(provenance["replay"]["status"], "reproduced")
        self.assertEqual(
            provenance["replay"]["authentication"],
            "not-established",
        )
        self.assertEqual(
            provenance["replay"]["report_sha256"],
            REPLAY_DIGEST,
        )
        self.assertIs(provenance["replay"]["strict_toolchain"], True)

        benchmark = _canonical_document(EVIDENCE / "data" / "scaling.json")
        self.assertEqual(
            benchmark["schema"],
            "unitsentinel.scaling-benchmark/v1",
        )
        self.assertEqual(benchmark["repetitions"], 3)
        self.assertEqual(
            set(benchmark["environment"]),
            {"architecture", "python", "solver", "system", "unitsentinel"},
        )
        recorded_at = datetime.fromisoformat(benchmark["recorded_at_utc"])
        self.assertIsNotNone(recorded_at.tzinfo)
        self.assertEqual(recorded_at.utcoffset(), timedelta(0))
        self.assertEqual(
            [row["nodes"] for row in benchmark["rows"]],
            [1, 8, 32, 128, 256],
        )
        for row in benchmark["rows"]:
            with self.subTest(benchmark_nodes=row["nodes"]):
                self.assertEqual(row["constraints"], 3 * row["nodes"] + 1)
                self.assertGreater(row["graph_bytes"], 0)
                self.assertGreater(row["certificate_bytes"], 0)
                for prefix in ("verify", "replay"):
                    runs = row[f"{prefix}_runs_ms"]
                    self.assertEqual(len(runs), 3)
                    self.assertTrue(all(run > 0 for run in runs))
                    self.assertEqual(
                        row[f"{prefix}_median_ms"],
                        round(statistics.median(runs), 6),
                    )

        transcript_expectations = {
            "verify.txt": (
                "UnitSentinel verification: VERIFIED",
                VERIFIED_GRAPH_DIGEST,
                VERIFIED_RESULT_DIGEST,
                CERTIFICATE_DIGEST,
                "[exit 0]\n",
            ),
            "conflict.txt": (
                "UnitSentinel verification: CONFLICT",
                CONFLICT_GRAPH_DIGEST,
                CONFLICT_RESULT_DIGEST,
                *EXPECTED_CONFLICT_CORE,
                "[exit 1]\n",
            ),
            "replay.txt": (
                "UnitSentinel replay: REPRODUCED",
                VERIFIED_GRAPH_DIGEST,
                CERTIFICATE_DIGEST,
                REPLAY_DIGEST,
                "[exit 0]\n",
            ),
        }
        for name, expected_fragments in transcript_expectations.items():
            transcript = (EVIDENCE / "captures" / name).read_text(encoding="utf-8")
            with self.subTest(transcript=name):
                for fragment in expected_fragments:
                    self.assertIn(fragment, transcript)
                self.assertTrue(transcript.endswith(expected_fragments[-1]))

    def test_svg_sources_are_accessible_and_self_contained(self) -> None:
        public_sources = sorted(ASSETS.glob("*.svg"))
        public_stems = {path.stem for path in public_sources}
        self.assertTrue(
            set(EXPECTED_VISUAL_DIMENSIONS).issubset(public_stems),
        )
        demo_sources = sorted((EVIDENCE / "demo").glob("frame-*.svg"))
        self.assertEqual(
            [path.name for path in demo_sources],
            ["frame-01.svg", "frame-02.svg", "frame-03.svg"],
        )

        for path in [*public_sources, *demo_sources]:
            with self.subTest(svg=path.relative_to(ROOT).as_posix()):
                payload = path.read_bytes()
                payload.decode("utf-8", errors="strict")
                self.assertIsNone(
                    re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", payload, re.I)
                )
                root = ElementTree.fromstring(payload)
                namespace_prefix = f"{{{SVG_NAMESPACE}}}"
                self.assertEqual(root.tag, f"{namespace_prefix}svg")

                identifiers: list[str] = []
                for element in root.iter():
                    self.assertTrue(element.tag.startswith(namespace_prefix))
                    local_tag = element.tag[len(namespace_prefix) :]
                    self.assertIn(local_tag, SAFE_SVG_TAGS)
                    identifier = element.attrib.get("id")
                    if identifier is not None:
                        identifiers.append(identifier)
                    for attribute, value in element.attrib.items():
                        local_attribute = attribute.rsplit("}", 1)[-1]
                        lowered_attribute = local_attribute.lower()
                        self.assertFalse(lowered_attribute.startswith("on"))
                        self.assertNotIn(
                            lowered_attribute,
                            {"href", "src", "style"},
                        )
                        self.assertIsNone(
                            re.search(
                                r"(?i)(?:javascript:|data:|file:|https?://)",
                                value,
                            )
                        )
                        if "url(" in value.lower():
                            reference = LOCAL_SVG_REFERENCE.fullmatch(value)
                            self.assertIsNotNone(reference)

                self.assertEqual(len(identifiers), len(set(identifiers)))
                identifier_set = set(identifiers)
                for element in root.iter():
                    for value in element.attrib.values():
                        if "url(" not in value.lower():
                            continue
                        reference = LOCAL_SVG_REFERENCE.fullmatch(value)
                        self.assertIsNotNone(reference)
                        self.assertIn(
                            reference.group("identifier"),
                            identifier_set,
                        )

                self.assertEqual(root.attrib.get("role"), "img")
                self.assertEqual(
                    root.attrib.get("aria-labelledby"),
                    "title description",
                )
                titles = root.findall(f"{namespace_prefix}title")
                descriptions = root.findall(f"{namespace_prefix}desc")
                self.assertEqual(len(titles), 1)
                self.assertEqual(len(descriptions), 1)
                self.assertEqual(titles[0].attrib.get("id"), "title")
                self.assertEqual(
                    descriptions[0].attrib.get("id"),
                    "description",
                )
                self.assertTrue("".join(titles[0].itertext()).strip())
                self.assertTrue("".join(descriptions[0].itertext()).strip())

                width = int(root.attrib["width"])
                height = int(root.attrib["height"])
                self.assertEqual(
                    root.attrib.get("viewBox"),
                    f"0 0 {width} {height}",
                )
                self.assertGreater(width, 0)
                self.assertGreater(height, 0)
                self.assertLessEqual(width * height, 16_777_216)
                if path.parent == ASSETS:
                    expected_dimensions = EXPECTED_VISUAL_DIMENSIONS.get(path.stem)
                    if expected_dimensions is not None:
                        self.assertEqual(
                            (width, height),
                            expected_dimensions,
                        )

        expected_demo_sources = (
            ASSETS / "conflict-terminal.svg",
            ASSETS / "verify-terminal.svg",
            ASSETS / "replay-terminal.svg",
        )
        for frame, public_source in zip(
            demo_sources,
            expected_demo_sources,
            strict=True,
        ):
            with self.subTest(frame=frame.name):
                self.assertEqual(frame.read_bytes(), public_source.read_bytes())

    def test_png_derivatives_are_structurally_valid(self) -> None:
        svg_sources = {path.stem: path for path in ASSETS.glob("*.svg")}
        png_outputs = {path.stem: path for path in ASSETS.glob("*.png")}
        self.assertTrue(
            set(EXPECTED_VISUAL_DIMENSIONS).issubset(svg_sources),
        )
        self.assertEqual(set(svg_sources), set(png_outputs))

        for stem, png_path in sorted(png_outputs.items()):
            with self.subTest(png=png_path.name):
                dimensions = _parse_png(png_path.read_bytes())
                svg_root = ElementTree.fromstring(svg_sources[stem].read_bytes())
                source_dimensions = (
                    int(svg_root.attrib["width"]),
                    int(svg_root.attrib["height"]),
                )
                self.assertEqual(dimensions, source_dimensions)
                expected = EXPECTED_VISUAL_DIMENSIONS.get(stem)
                if expected is not None:
                    self.assertEqual(dimensions, expected)

    def test_gif_demo_has_declared_frames_delays_and_loop(self) -> None:
        frame_manifest = _canonical_document(EVIDENCE / "demo" / "frames.json")
        expected_manifest = {
            "frames": [
                {"delay_ms": 2_400, "path": "frame-01.svg"},
                {"delay_ms": 2_400, "path": "frame-02.svg"},
                {"delay_ms": 2_800, "path": "frame-03.svg"},
            ],
            "schema": "unitsentinel.demo-frames/v1",
        }
        self.assertEqual(frame_manifest, expected_manifest)

        logical_screen, applications, frames = _parse_gif(
            (ASSETS / "unitsentinel-demo.gif").read_bytes()
        )
        self.assertEqual(logical_screen[:2], (1_440, 900))
        self.assertTrue(logical_screen[2] & 0x80)
        self.assertEqual(
            applications,
            [(b"NETSCAPE2.0", b"\x01\x00\x00")],
        )
        self.assertEqual(len(frames), 3)
        self.assertEqual(
            [frame[6] for frame in frames],
            [240, 240, 280],
        )
        for frame in frames:
            with self.subTest(frame=frame):
                self.assertEqual(frame[:4], (0, 0, 1_440, 900))
                self.assertEqual(frame[4], 0)
                self.assertEqual(frame[5], 0x04)
                self.assertEqual(frame[7], 8)
        delays_ms = [
            frame_record["delay_ms"] for frame_record in frame_manifest["frames"]
        ]
        self.assertTrue(all(delay % 10 == 0 for delay in delays_ms))
        self.assertEqual(
            [delay // 10 for delay in delays_ms],
            [frame[6] for frame in frames],
        )

        expected_sources = (
            ASSETS / "conflict-terminal.svg",
            ASSETS / "verify-terminal.svg",
            ASSETS / "replay-terminal.svg",
        )
        for frame_record, expected_source in zip(
            frame_manifest["frames"],
            expected_sources,
            strict=True,
        ):
            frame_path = EVIDENCE / "demo" / frame_record["path"]
            with self.subTest(frame_source=frame_path.name):
                _assert_inside_repository(frame_path)
                self.assertEqual(
                    frame_path.read_bytes(),
                    expected_source.read_bytes(),
                )

    def test_text_evidence_has_no_local_identity_credentials_or_controls(
        self,
    ) -> None:
        paths = [ROOT / "README.md"]
        if EVIDENCE_README.exists():
            paths.append(EVIDENCE_README)
        paths.extend(
            path
            for path in _repository_files(EVIDENCE)
            if path.suffix in {".json", ".md", ".svg", ".txt"}
        )
        paths.extend(sorted(ASSETS.glob("*.svg")))

        for path in sorted(set(paths)):
            relative = path.relative_to(ROOT).as_posix()
            with self.subTest(path=relative):
                text = path.read_bytes().decode("utf-8", errors="strict")
                self.assertIsNone(
                    CONTROL_CHARACTER.search(text),
                    f"{relative}: control character",
                )
                for label, pattern in SECRET_PATTERNS:
                    self.assertIsNone(
                        pattern.search(text),
                        f"{relative}: detected {label}",
                    )

    def test_readmes_link_every_artifact_and_embed_required_visuals(
        self,
    ) -> None:
        readmes = (ROOT / "README.md", EVIDENCE_README)
        for readme in readmes:
            self.assertTrue(
                readme.is_file(),
                f"missing {readme.relative_to(ROOT)}",
            )

        linked_files: set[Path] = set()
        root_embeds: set[str] = set()
        for readme in readmes:
            markdown = readme.read_text(encoding="utf-8")
            matches = list(MARKDOWN_LINK.finditer(markdown))
            self.assertEqual(
                markdown.count("!["),
                sum(match.group("image") is not None for match in matches),
                f"{readme.relative_to(ROOT)} has an unparsed image link",
            )
            for match in matches:
                target = match.group("target")
                if target.startswith("<") and target.endswith(">"):
                    target = target[1:-1]
                parsed = urlsplit(target)
                is_image = match.group("image") is not None
                if parsed.scheme or parsed.netloc:
                    self.assertIn(
                        parsed.scheme,
                        {"http", "https", "mailto"},
                    )
                    self.assertFalse(
                        is_image,
                        "README images must be committed local artifacts",
                    )
                    continue
                if not parsed.path:
                    continue

                decoded_path = unquote(parsed.path)
                candidate = (readme.parent / decoded_path).resolve()
                _assert_inside_repository(candidate)
                self.assertTrue(
                    candidate.is_file(),
                    (f"{readme.relative_to(ROOT)} links missing file {decoded_path}"),
                )
                self.assertFalse(candidate.is_symlink())
                linked_files.add(candidate)

                if is_image:
                    alt_text = " ".join(match.group("label").split())
                    self.assertGreaterEqual(len(alt_text), 12)
                    self.assertIsNone(
                        re.fullmatch(
                            r"(?i)(?:image|screenshot|diagram|demo|gif)\s*\d*",
                            alt_text,
                        )
                    )
                    if readme == ROOT / "README.md":
                        root_embeds.add(candidate.relative_to(ROOT).as_posix())

        self.assertTrue(
            REQUIRED_README_EMBEDS.issubset(root_embeds),
            (
                "root README is missing required evidence embeds: "
                f"{sorted(REQUIRED_README_EMBEDS - root_embeds)}"
            ),
        )

        asset_files = set(_repository_files(ASSETS))
        evidence_files = set(_repository_files(EVIDENCE, exclude={EVIDENCE_README}))
        self.assertTrue(
            asset_files.issubset(linked_files),
            (
                "evidence READMEs do not link assets: "
                f"{sorted(path.name for path in asset_files - linked_files)}"
            ),
        )
        self.assertTrue(
            evidence_files.issubset(linked_files),
            (
                "evidence READMEs do not link source files: "
                f"{sorted(path.name for path in evidence_files - linked_files)}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
