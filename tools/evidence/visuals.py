"""Dependency-free SVG builders for recorded UnitSentinel evidence."""

from __future__ import annotations

import math
from html import escape
from typing import Any, Final

SVG_WIDTH: Final = 1_440
TERMINAL_HEIGHT: Final = 900
BACKGROUND: Final = "#07111f"
PANEL: Final = "#0d1b2d"
BORDER: Final = "#253a55"
TEXT: Final = "#d8e4f2"
MUTED: Final = "#8fa5bd"
CYAN: Final = "#38d9f5"
GREEN: Final = "#5ee6a8"
RED: Final = "#ff6b7a"
AMBER: Final = "#ffc857"
VIOLET: Final = "#b69cff"


def _document(
    *,
    width: int,
    height: int,
    title: str,
    description: str,
    body: str,
) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        'role="img" aria-labelledby="title description">\n'
        f'  <title id="title">{escape(title)}</title>\n'
        f'  <desc id="description">{escape(description)}</desc>\n'
        "  <defs>\n"
        '    <linearGradient id="background" x1="0" y1="0" x2="1" y2="1">\n'
        f'      <stop offset="0" stop-color="{BACKGROUND}"/>\n'
        '      <stop offset="1" stop-color="#0a1730"/>\n'
        "    </linearGradient>\n"
        '    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">\n'
        '      <feDropShadow dx="0" dy="10" stdDeviation="12" '
        'flood-color="#000814" flood-opacity=".45"/>\n'
        "    </filter>\n"
        '    <marker id="arrow" markerWidth="10" markerHeight="10" '
        'refX="9" refY="3" orient="auto" markerUnits="strokeWidth">\n'
        f'      <path d="M0,0 L0,6 L9,3 z" fill="{MUTED}"/>\n'
        "    </marker>\n"
        "  </defs>\n"
        f'  <rect width="{width}" height="{height}" fill="url(#background)"/>\n'
        f"{body}\n"
        "</svg>\n"
    )


def _text(
    x: int | float,
    y: int | float,
    value: str,
    *,
    size: int = 18,
    fill: str = TEXT,
    weight: int = 400,
    anchor: str = "start",
    family: str = "DejaVu Sans, sans-serif",
) -> str:
    return (
        f'<text x="{x}" y="{y}" fill="{fill}" font-family="{family}" '
        f'font-size="{size}" font-weight="{weight}" '
        f'text-anchor="{anchor}">{escape(value)}</text>'
    )


def _multiline(
    x: int,
    y: int,
    lines: tuple[str, ...],
    *,
    size: int = 17,
    fill: str = TEXT,
    line_height: int = 25,
    weight: int = 400,
    anchor: str = "start",
) -> str:
    spans = "\n".join(
        f'      <tspan x="{x}" dy="{0 if index == 0 else line_height}">'
        f"{escape(line)}</tspan>"
        for index, line in enumerate(lines)
    )
    return (
        f'<text x="{x}" y="{y}" fill="{fill}" '
        'font-family="DejaVu Sans, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" text-anchor="{anchor}">\n'
        f"{spans}\n"
        "    </text>"
    )


def _box(
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    title: str,
    lines: tuple[str, ...],
    accent: str,
) -> str:
    body = [
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="18" '
        f'fill="{PANEL}" stroke="{BORDER}" stroke-width="2" filter="url(#shadow)"/>',
        f'<rect x="{x}" y="{y}" width="7" height="{height}" rx="3.5" fill="{accent}"/>',
        _text(x + 28, y + 38, title, size=20, fill=accent, weight=700),
    ]
    if lines:
        body.append(_multiline(x + 28, y + 70, lines, size=15, fill=TEXT))
    return "\n".join(body)


def _arrow(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    label: str | None = None,
    color: str = MUTED,
) -> str:
    body = [
        f'<path d="M{x1},{y1} L{x2},{y2}" stroke="{color}" '
        'stroke-width="3" fill="none" marker-end="url(#arrow)"/>'
    ]
    if label is not None:
        body.append(
            _text(
                (x1 + x2) / 2,
                (y1 + y2) / 2 - 10,
                label,
                size=14,
                fill=color,
                anchor="middle",
            )
        )
    return "\n".join(body)


def terminal_svg(
    *,
    title: str,
    transcript: str,
    accent: str,
    description: str,
) -> str:
    lines = transcript.rstrip("\n").splitlines()
    if len(lines) > 31:
        raise ValueError("terminal transcript exceeds the visual line budget")
    body = [
        '<rect x="32" y="30" width="1376" height="840" rx="20" '
        f'fill="{PANEL}" stroke="{BORDER}" stroke-width="2" '
        'filter="url(#shadow)"/>',
        '<rect x="32" y="30" width="1376" height="66" rx="20" fill="#12233a"/>',
        '<rect x="32" y="76" width="1376" height="20" fill="#12233a"/>',
        '<circle cx="70" cy="63" r="8" fill="#ff6b7a"/>',
        '<circle cx="98" cy="63" r="8" fill="#ffc857"/>',
        '<circle cx="126" cy="63" r="8" fill="#5ee6a8"/>',
        _text(720, 70, title, size=19, fill=TEXT, weight=700, anchor="middle"),
        f'<rect x="32" y="94" width="1376" height="4" fill="{accent}"/>',
    ]
    y = 132
    for line in lines:
        fill = TEXT
        weight = 400
        if line.startswith("$") or line.startswith("    "):
            fill = CYAN
        elif "VERIFIED" in line or "REPRODUCED" in line:
            fill = GREEN
            weight = 700
        elif "CONFLICT" in line or "MISMATCH" in line or "not-issued" in line:
            fill = RED
            weight = 700 if "CONFLICT" in line else 400
        elif "sha256:" in line or "replay sha256:" in line:
            fill = VIOLET
        elif line.startswith("[exit "):
            fill = accent
            weight = 700
        body.append(
            _text(
                62,
                y,
                line,
                size=17,
                fill=fill,
                weight=weight,
                family="DejaVu Sans Mono, monospace",
            )
        )
        y += 24
    return _document(
        width=SVG_WIDTH,
        height=TERMINAL_HEIGHT,
        title=title,
        description=description,
        body="\n".join(body),
    )


def workflow_svg(*, graph_digest: str, result_digest: str) -> str:
    body = [
        _text(70, 70, "UnitSentinel verification pipeline", size=32, weight=700),
        _text(
            70,
            104,
            "Implemented boundaries; positive, conflict, and replay paths "
            "are recorded.",
            size=17,
            fill=MUTED,
        ),
        _box(
            70,
            155,
            260,
            150,
            title="Canonical graph",
            lines=(
                "closed JSON schema",
                "1 MiB / 512-node caps",
                f"sha256 {graph_digest[:12]}…",
            ),
            accent=CYAN,
        ),
        _box(
            405,
            155,
            260,
            150,
            title="Strict decoder",
            lines=(
                "duplicate / float reject",
                "topology + shape metadata",
                "byte-for-byte closure",
            ),
            accent=CYAN,
        ),
        _box(
            740,
            155,
            290,
            150,
            title="Constraint compiler",
            lines=(
                "dimension + kind",
                "exact scale + offset",
                "source-labelled witnesses",
            ),
            accent=VIOLET,
        ),
        _box(
            1105,
            155,
            265,
            150,
            title="Tracked Z3",
            lines=(
                "bounded checks",
                "alternate-model test",
                "independent Python replay",
            ),
            accent=VIOLET,
        ),
        _arrow(330, 230, 405, 230),
        _arrow(665, 230, 740, 230),
        _arrow(1030, 230, 1105, 230),
        _box(
            110,
            420,
            260,
            155,
            title="CONFLICT",
            lines=(
                "tracked witness core",
                "bounded deterministic shrink",
                "never issues a certificate",
            ),
            accent=RED,
        ),
        _box(
            435,
            420,
            260,
            155,
            title="UNDERCONSTRAINED",
            lines=(
                "alternate model exists",
                "observable values listed",
                "no positive claim",
            ),
            accent=AMBER,
        ),
        _box(
            760,
            420,
            260,
            155,
            title="UNKNOWN",
            lines=(
                "timeout / memory / solver",
                "redacted stable reason",
                "fails closed",
            ),
            accent=AMBER,
        ),
        _box(
            1085,
            420,
            260,
            155,
            title="VERIFIED",
            lines=(
                "unique exact contracts",
                f"result {result_digest[:12]}…",
                "positive issuance allowed",
            ),
            accent=GREEN,
        ),
        _arrow(1238, 305, 240, 420, label="unsat", color=RED),
        _arrow(1238, 305, 565, 420, label="non-unique", color=AMBER),
        _arrow(1238, 305, 890, 420, label="resource error", color=AMBER),
        _arrow(1238, 305, 1215, 420, label="unique model", color=GREEN),
        _box(
            530,
            690,
            380,
            130,
            title="Detached certificate",
            lines=(
                "graph + registry + limits + toolchain",
                "content-addressed, unsigned",
                "only a positive verification claim",
            ),
            accent=GREEN,
        ),
        _box(
            1010,
            690,
            330,
            130,
            title="Independent replay",
            lines=(
                "pure contract check",
                "fresh bounded solver run",
                "REPRODUCED ≠ authenticated",
            ),
            accent=CYAN,
        ),
        _arrow(1215, 575, 720, 690, label="issue", color=GREEN),
        _arrow(910, 755, 1010, 755, label="replay", color=CYAN),
    ]
    return _document(
        width=1_440,
        height=890,
        title="UnitSentinel verification pipeline",
        description=(
            "The implemented canonical decoding, constraint solving, fail-closed "
            "outcomes, positive certificate issuance, and independent replay flow."
        ),
        body="\n".join(body),
    )


def contract_flow_svg(contracts: list[dict[str, Any]]) -> str:
    by_id = {str(item["value_id"]): item for item in contracts}

    def contract(value_id: str) -> str:
        item = by_id[value_id]
        dimension = item["dimension"]
        if not dimension:
            dimension_text = "dimensionless"
        else:
            dimension_text = " ".join(
                f"{term['base']}^{term['exponent']}" for term in dimension
            )
        return f"{dimension_text}\nscale {item['scale']} · {item['kind']}"

    body = [
        _text(60, 64, "Physics-informed wheel anomaly feature", size=31, weight=700),
        _text(
            60,
            98,
            "Dimensions, scales, and kinds come from the live verified record.",
            size=17,
            fill=MUTED,
        ),
        _box(
            55,
            160,
            275,
            155,
            title="wheel-speed-kph",
            lines=tuple(contract("wheel-speed-kph").splitlines()),
            accent=CYAN,
        ),
        _box(
            55,
            365,
            275,
            155,
            title="previous speed",
            lines=tuple(contract("previous-wheel-speed-kph").splitlines()),
            accent=CYAN,
        ),
        _box(
            410,
            260,
            255,
            160,
            title="SUBTRACT",
            lines=(
                "speed-delta-kph",
                *contract("speed-delta-kph").splitlines(),
            ),
            accent=VIOLET,
        ),
        _box(
            745,
            260,
            255,
            160,
            title="CONVERT → m/s",
            lines=(
                "speed-delta-si",
                *contract("speed-delta-si").splitlines(),
            ),
            accent=VIOLET,
        ),
        _box(
            55,
            640,
            275,
            155,
            title="sample-period-ms",
            lines=tuple(contract("sample-period-ms").splitlines()),
            accent=CYAN,
        ),
        _box(
            410,
            640,
            255,
            155,
            title="CONVERT → s",
            lines=(
                "sample-period-si",
                *contract("sample-period-si").splitlines(),
            ),
            accent=VIOLET,
        ),
        _box(
            1080,
            380,
            285,
            175,
            title="DIVIDE Δv / Δt",
            lines=(
                "acceleration-si",
                *contract("acceleration-si").splitlines(),
            ),
            accent=GREEN,
        ),
        _box(
            745,
            680,
            255,
            155,
            title="reference accel.",
            lines=tuple(contract("acceleration-reference").splitlines()),
            accent=CYAN,
        ),
        _box(
            1080,
            625,
            285,
            220,
            title="DIVIDE + SIGMOID",
            lines=(
                "normalized-acceleration",
                *contract("normalized-acceleration").splitlines(),
                "anomaly-score",
                *contract("anomaly-score").splitlines(),
            ),
            accent=GREEN,
        ),
        _arrow(330, 237, 410, 320),
        _arrow(330, 442, 410, 365),
        _arrow(665, 340, 745, 340, label="5/18 → 1"),
        _arrow(1000, 340, 1080, 445),
        _arrow(330, 717, 410, 717),
        _arrow(665, 717, 1080, 500, label="time^1"),
        _arrow(1220, 555, 1220, 625),
        _arrow(1000, 757, 1080, 757),
    ]
    return _document(
        width=1_440,
        height=900,
        title="Verified wheel anomaly contract flow",
        description=(
            "A real six-operation ML feature pipeline with exact dimensions, "
            "unit scales, acceleration derivation, normalization, and sigmoid."
        ),
        body="\n".join(body),
    )


def conflict_core_svg(
    *,
    core_ids: list[str],
    graph_digest: str,
    result_digest: str,
) -> str:
    body = [
        _text(
            70,
            68,
            "A shape-valid serving contract that cannot be physical",
            size=30,
            weight=700,
        ),
        _text(
            70,
            104,
            "The only changed annotation is acceleration-si: m/s² → m/s.",
            size=18,
            fill=MUTED,
        ),
        _box(
            90,
            190,
            300,
            165,
            title="speed-delta-si",
            lines=("length^1 time^-1", "scale 1", "derived through CONVERT"),
            accent=CYAN,
        ),
        _box(
            90,
            520,
            300,
            165,
            title="sample-period-si",
            lines=("time^1", "scale 1", "derived through CONVERT"),
            accent=CYAN,
        ),
        _box(
            545,
            355,
            310,
            175,
            title="DIVIDE Δv / Δt",
            lines=(
                "derived acceleration",
                "length^1 time^-2",
                "meter-per-second-squared",
            ),
            accent=GREEN,
        ),
        _box(
            1010,
            355,
            330,
            175,
            title="declared acceleration-si",
            lines=("length^1 time^-1", "meter-per-second", "CONTRADICTION"),
            accent=RED,
        ),
        _arrow(390, 272, 545, 410),
        _arrow(390, 602, 545, 480),
        _arrow(855, 442, 1010, 442, label="≠", color=RED),
        _text(70, 760, "Deletion-minimal tracked core", size=23, fill=RED, weight=700),
        _text(
            1370,
            760,
            f"graph {graph_digest[:12]}… · result {result_digest[:12]}…",
            size=14,
            fill=MUTED,
            anchor="end",
        ),
    ]
    for index, witness in enumerate(core_ids, start=1):
        body.append(
            _text(
                90,
                798 + 28 * (index - 1),
                f"{index}. {witness}",
                size=16,
                fill=TEXT,
                family="DejaVu Sans Mono, monospace",
            )
        )
    return _document(
        width=1_440,
        height=930,
        title="UnitSentinel conflict-core explanation",
        description=(
            "The actual four-witness deletion-minimal conflict produced by the "
            "wheel anomaly serving-contract error."
        ),
        body="\n".join(body),
    )


def lineage_svg(
    *,
    certificate_bytes: int,
    graph_digest: str,
    graph_bytes: int,
    registry_digest: str,
    registry_units: int,
    registry_version: str,
    result_digest: str,
    contract_count: int,
    constraint_count: int,
    checks_performed: int,
    certificate_digest: str,
    replay_digest: str,
    verifier_version: str,
    solver_version: str,
) -> str:
    body = [
        _text(70, 68, "Content-addressed proof lineage", size=31, weight=700),
        _text(
            70,
            104,
            "Integrity and semantic reproduction are explicit; "
            "issuer authentication is not provided.",
            size=17,
            fill=MUTED,
        ),
        _box(
            65,
            165,
            300,
            145,
            title="Canonical graph",
            lines=(
                f"sha256 {graph_digest[:20]}…",
                f"{graph_bytes:,} exact bytes",
            ),
            accent=CYAN,
        ),
        _box(
            430,
            165,
            300,
            145,
            title="Registry snapshot",
            lines=(
                f"sha256 {registry_digest[:20]}…",
                f"version {registry_version} · {registry_units} units",
            ),
            accent=CYAN,
        ),
        _box(
            795,
            165,
            300,
            145,
            title="Pinned toolchain",
            lines=(
                f"unitsentinel {verifier_version}",
                f"z3 {solver_version}",
                "caller-owned solver limits",
            ),
            accent=VIOLET,
        ),
        _box(
            345,
            405,
            390,
            170,
            title="Verified result",
            lines=(
                f"{contract_count} unique contracts · {constraint_count} witnesses",
                f"{checks_performed} bounded solver checks",
                f"sha256 {result_digest[:20]}…",
            ),
            accent=GREEN,
        ),
        _box(
            345,
            665,
            390,
            150,
            title="Detached certificate",
            lines=(
                f"positive claim · unsigned · {certificate_bytes:,} bytes",
                "authentication: not-provided",
                f"sha256 {certificate_digest[:20]}…",
            ),
            accent=GREEN,
        ),
        _box(
            970,
            455,
            395,
            250,
            title="Strict replay",
            lines=(
                "recompute certificate digest",
                "compare current graph + registry",
                "match current toolchain (strict)",
                "catalog + pure witness replay",
                "fresh uniqueness check",
                "status REPRODUCED",
                f"sha256 {replay_digest[:20]}…",
            ),
            accent=CYAN,
        ),
        _text(
            540,
            365,
            "graph + registry + toolchain establish the claim",
            size=15,
            fill=MUTED,
            anchor="middle",
        ),
        _arrow(215, 310, 430, 405),
        _arrow(580, 310, 540, 405),
        _arrow(945, 310, 650, 405),
        _arrow(540, 575, 540, 665, label="issue", color=GREEN),
        _arrow(
            735,
            740,
            970,
            590,
            label="claim + current inputs",
            color=CYAN,
        ),
    ]
    return _document(
        width=1_440,
        height=900,
        title="UnitSentinel certificate and replay lineage",
        description=(
            "Exact graph, registry, toolchain, verification result, certificate, "
            "and strict semantic replay digests."
        ),
        body="\n".join(body),
    )


def scaling_svg(benchmark: dict[str, Any]) -> str:
    rows = list(benchmark["rows"])
    if len(rows) < 2:
        raise ValueError("benchmark plot requires at least two rows")
    left, right, top, bottom = 120, 1_355, 170, 660
    all_values = [
        float(row[key])
        for row in rows
        for key in ("verify_median_ms", "replay_median_ms")
    ]
    minimum = max(min(all_values) * 0.75, 0.01)
    maximum = max(all_values) * 1.25
    log_min = math.log10(minimum)
    log_max = math.log10(maximum)

    def x_position(index: int) -> float:
        return round(left + index * (right - left) / (len(rows) - 1), 1)

    def y_position(value: float) -> float:
        ratio = (math.log10(value) - log_min) / (log_max - log_min)
        return round(bottom - ratio * (bottom - top), 1)

    verify_points = " ".join(
        f"{x_position(index):.1f},{y_position(float(row['verify_median_ms'])):.1f}"
        for index, row in enumerate(rows)
    )
    replay_points = " ".join(
        f"{x_position(index):.1f},{y_position(float(row['replay_median_ms'])):.1f}"
        for index, row in enumerate(rows)
    )
    body = [
        _text(65, 62, "Bounded verification and replay scaling", size=31, weight=700),
        _text(
            65,
            98,
            "Measured snapshot · median of 3 runs · "
            "log-scale milliseconds · lower is better",
            size=17,
            fill=MUTED,
        ),
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" '
        f'stroke="{BORDER}" stroke-width="2"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" '
        f'stroke="{BORDER}" stroke-width="2"/>',
    ]
    tick_count = 5
    for tick in range(tick_count):
        ratio = tick / (tick_count - 1)
        y = bottom - ratio * (bottom - top)
        value = 10 ** (log_min + ratio * (log_max - log_min))
        body.extend(
            (
                f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" '
                f'stroke="{BORDER}" stroke-width="1" stroke-dasharray="5 8"/>',
                _text(
                    left - 18, y + 6, f"{value:.1f}", size=14, fill=MUTED, anchor="end"
                ),
            )
        )
    body.extend(
        (
            f'<polyline points="{verify_points}" fill="none" stroke="{GREEN}" '
            'stroke-width="4"/>',
            f'<polyline points="{replay_points}" fill="none" stroke="{CYAN}" '
            'stroke-width="4"/>',
            f'<line x1="955" y1="132" x2="1005" y2="132" stroke="{GREEN}" '
            'stroke-width="4"/>',
            _text(1018, 138, "verify + issue", size=15, fill=GREEN, weight=700),
            f'<line x1="1170" y1="132" x2="1220" y2="132" stroke="{CYAN}" '
            'stroke-width="4"/>',
            _text(1233, 138, "strict replay", size=15, fill=CYAN, weight=700),
        )
    )
    for index, row in enumerate(rows):
        x = x_position(index)
        verify_y = y_position(float(row["verify_median_ms"]))
        replay_y = y_position(float(row["replay_median_ms"]))
        body.extend(
            (
                f'<circle cx="{x:.1f}" cy="{verify_y:.1f}" r="7" fill="{GREEN}"/>',
                f'<circle cx="{x:.1f}" cy="{replay_y:.1f}" r="7" fill="{CYAN}"/>',
                _text(
                    x,
                    bottom + 32,
                    str(row["nodes"]),
                    size=15,
                    fill=TEXT,
                    anchor="middle",
                ),
                _text(
                    x,
                    verify_y + 25,
                    f"{float(row['verify_median_ms']):.1f}",
                    size=13,
                    fill=GREEN,
                    weight=700,
                    anchor="middle",
                ),
                _text(
                    x,
                    replay_y - 14,
                    f"{float(row['replay_median_ms']):.1f}",
                    size=13,
                    fill=CYAN,
                    weight=700,
                    anchor="middle",
                ),
            )
        )
    body.extend(
        (
            _text(
                (left + right) / 2,
                735,
                "identity-chain nodes",
                size=17,
                fill=MUTED,
                anchor="middle",
            ),
            _text(65, 805, "Snapshot context", size=20, fill=VIOLET, weight=700),
            _text(
                65,
                840,
                (
                    f"Python {benchmark['environment']['python']} · "
                    f"Z3 {benchmark['environment']['solver']} · "
                    f"UnitSentinel {benchmark['environment']['unitsentinel']} · "
                    f"{benchmark['recorded_at_utc']}"
                ),
                size=15,
                fill=MUTED,
            ),
        )
    )
    return _document(
        width=1_440,
        height=890,
        title="UnitSentinel scaling benchmark",
        description=(
            "Measured median verification-plus-issuance and strict replay time "
            "across bounded identity-chain graph sizes."
        ),
        body="\n".join(body),
    )
