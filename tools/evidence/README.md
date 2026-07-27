# Reproducible evidence pipeline

This directory turns implemented UnitSentinel behavior into reviewable
portfolio evidence. It does not synthesize terminal output or edit result
records by hand:

- the general Python recorder executes the production CLI for verified,
  conflict, and strict-replay paths;
- the closed repair-only recorder executes one pinned production CLI search
  and can publish only its capture, provenance, and lineage SVG;
- the closed comparison-only recorder executes three caller-pinned production
  CLI comparisons and can publish only their graphs, plans, captures, strict
  raw results, byte-size data, and provenance;
- SVG builders consume those CLI records, inferred contracts, tracked conflict
  witnesses, certificate bindings, and a measured benchmark snapshot;
- the pinned Node renderer converts the committed SVG sources to PNG and builds
  the GIF from the three declared transcript frames.

## Install the renderer

From the repository root:

```bash
npm --prefix tools/evidence ci --ignore-scripts
npm --prefix tools/evidence run audit
```

The lockfile pins `@resvg/resvg-js`, `gifenc`, and `pngjs`, including package
integrity hashes. Installation is the only network-dependent step. Recording,
rendering, and verification are offline.

## Refresh evidence

Use the repository virtual environment so the displayed command and the
executed interpreter describe the same setup:

```bash
.venv/bin/python -m tools.evidence.generate --record
npm --prefix tools/evidence run render
.venv/bin/python -m tools.evidence.generate --write-manifest
```

`--record` reuses the committed timing snapshot. Use
`--record-benchmark` only when intentionally measuring a new snapshot:

```bash
.venv/bin/python -m tools.evidence.generate --record-benchmark
npm --prefix tools/evidence run render
.venv/bin/python -m tools.evidence.generate --write-manifest
```

The recorder refuses to delete an existing
`.unitsentinel/evidence-run` directory. Its own scratch directory is removed
after a successful or failed run.

### Refresh only repair evidence

The repair slice has fixed input, bounds, output paths, and raster target:

```bash
.venv/bin/python -m tools.evidence.repair_evidence --record
npm --prefix tools/evidence run render:repair
.venv/bin/python -m tools.evidence.repair_evidence --check
npm --prefix tools/evidence run check:repair
.venv/bin/python -m tools.evidence.generate --write-manifest
```

The recorder validates the canonical CLI envelope, all graph/result/candidate
digests, and the exact one-annotation lineage before writing anything. Its
allowlist contains only `repair.json`, `repair.txt`,
`repair-provenance.json`, and `unit-repair-lineage.svg`. The renderer mode has
one compiled-in source and output basename; it cannot select arbitrary files.
The manifest command performs a full in-memory raster freshness check and then
rewrites only the global closed manifest.

### Refresh only comparison records

```bash
.venv/bin/python -m tools.evidence.comparison_evidence --record
.venv/bin/python -m tools.evidence.comparison_evidence --check
.venv/bin/python -m tools.evidence.generate --write-manifest
```

The recorder constructs four canonical ratio graphs and three plans from typed
production models, executes both text and JSON CLI modes for compatible,
normalization-drift, and indeterminate outcomes, and requires both runs to
write identical strict result bytes. Its fixed output allowlist contains 18
files under `docs/evidence`; no timing or performance claim is recorded.
Writes are atomic per file, not across the complete slice. An interrupted
refresh therefore leaves the manifest stale and the required `--check` fails
until the entire fixed slice is recorded again.

## Verify freshness

Both checks are required:

```bash
.venv/bin/python -m tools.evidence.generate --check
npm --prefix tools/evidence run check
.venv/bin/python -m tools.evidence.repair_evidence --check
npm --prefix tools/evidence run check:repair
.venv/bin/python -m tools.evidence.comparison_evidence --check
```

The Python check replays the deterministic source evidence without re-running
the timing benchmark. The renderer check builds every expected PNG and GIF in
memory and compares exact bytes without publishing output. The manifest command
also refuses to bless raster assets until the renderer check passes.

Production CLI captures are drained incrementally with a 30-second deadline,
a 40 MiB stdout ceiling, and a 64 KiB stderr ceiling. Exceeding any bound kills
the child and fails without recording its output. Comparison text is accepted
only when every line exactly reconstructs from the decoded result, graph,
plan, registry, and solver limits; extra or missing stdout is rejected.

## Output map

| Output | Source of truth |
| --- | --- |
| `docs/evidence/contracts/*.json` | Production graph builder and canonical codec |
| `docs/evidence/captures/*` | Actual production CLI stdout and exit status |
| `docs/evidence/claims/wheel-anomaly.cert.json` | Actual positive certificate |
| `docs/evidence/claims/ratio-*.result.json` | Actual unsigned comparison results, including non-compatible outcomes |
| `docs/evidence/provenance.json` | Cross-bound graph, result, registry, certificate, and replay identities |
| `docs/evidence/repair-provenance.json` | Cross-bound source, relaxed, repaired, candidate, and search identities |
| `docs/evidence/comparison-provenance.json` | Cross-bound graph, plan, verification, result, and normalization identities |
| `docs/evidence/data/comparison-artifacts.json` | Exact canonical comparison artifact sizes; no latency claim |
| `docs/evidence/data/scaling.json` | Recorded bounded timing runs and environment |
| `docs/assets/*.svg` | Live records consumed by dependency-free SVG builders |
| `docs/assets/*.png` | Pinned Resvg rendering of the SVG sources |
| `docs/assets/unitsentinel-demo.gif` | Declared transcript frames and delays |
| `docs/evidence/manifest.json` | Closed file set, byte counts, and SHA-256 digests |

## Rendering boundary

The renderer accepts only bounded regular files under the declared evidence
directories. It rejects symlinks, external SVG resources, scripts, images,
DOCTYPE/entity declarations, oversized documents, unexpected manifest fields,
mixed frame dimensions, and unsafe output targets. Writes are atomic per file;
the complete output set is not a multi-file transaction. The fixed repair mode
writes exactly one PNG.

Rendering uses DejaVu Sans with system-font loading disabled. For byte-identical
output on another host, provide the same regular font file through an absolute
`UNITSENTINEL_FONT_PATH`, or use one of the documented DejaVu system paths.
The current manifest records the exact committed output bytes.

The renderer assumes a POSIX little-endian host and excludes a hostile same-UID
process that can replace repository parent directories during execution.
