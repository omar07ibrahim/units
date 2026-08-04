# Reproducible evidence pipeline

This directory turns implemented UnitSentinel behavior into reviewable
portfolio evidence. Production CLI result records come from executed commands;
the stable distribution transcript is independently reconstructed by hosted
CI rather than accepted on the recorder's assertion:

- the general Python recorder executes the production CLI for verified,
  conflict, and strict-replay paths;
- the closed repair-only recorder executes one pinned production CLI search
  and can publish only its capture, provenance, and lineage SVG;
- the closed comparison-only recorder executes three caller-pinned production
  CLI comparisons and can publish only their graphs, plans, captures, strict
  raw results, byte-size data, and provenance;
- the closed comparison-visual recorder derives six SVG sources and three GIF
  frames only from those checked records;
- the closed distribution-visual recorder validates one canonical release
  contract and one hosted transcript, then derives two public SVG sources;
- SVG builders consume those CLI records, inferred contracts, tracked conflict
  witnesses, certificate bindings, and a measured benchmark snapshot;
- the pinned Node renderer converts committed SVG sources to PNG and builds
  both GIFs from their declared transcript frames.

## Install the renderer

From the repository root:

```bash
npm --prefix tools/evidence ci --ignore-scripts
npm --prefix tools/evidence run audit
```

The lockfile pins `@resvg/resvg-js`, `gifenc`, and `pngjs`, including package
integrity hashes. Installation is the only network-dependent step. Recording,
rendering, and verification are offline.

## Refresh legacy verify/conflict/replay evidence

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

### Refresh only comparison records and visuals

```bash
.venv/bin/python -m tools.evidence.comparison_evidence --record
.venv/bin/python -m tools.evidence.comparison_visuals --record
npm --prefix tools/evidence run render:comparison
.venv/bin/python -m tools.evidence.comparison_evidence --check
.venv/bin/python -m tools.evidence.comparison_visuals --check
npm --prefix tools/evidence run check:comparison
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

The visual recorder first checks the canonical comparison records, then derives
workflow, normalization-lineage, exact-artifact-size, and complete terminal
SVGs. Its allowlist contains six public SVGs, three byte-identical frame
copies, and one exact frame manifest. The comparison renderer has a separate
compiled-in six-source/three-frame mode and publishes only the six PNGs and
one GIF.

### Refresh only distribution visuals

```bash
.venv/bin/python -m tools.evidence.distribution_visuals --record
npm --prefix tools/evidence run render
.venv/bin/python -m tools.evidence.distribution_visuals --check
npm --prefix tools/evidence run check
.venv/bin/python -m tools.evidence.generate --write-manifest
```

The distribution recorder can write only
`docs/evidence/data/distribution-contract.json`,
`docs/evidence/captures/distribution.txt`, and the two matching SVG sources.
It performs no download or release build. The exact CPython 3.12.3 GitHub
Actions job reconstructs the transcript from the real verifier invocation and
compares it byte for byte; the local check validates that committed record and
its deterministic renderings.

## Verify freshness

All checks are required:

```bash
.venv/bin/python -m tools.evidence.generate --check
.venv/bin/python -m tools.evidence.distribution_visuals --check
npm --prefix tools/evidence run check
.venv/bin/python -m tools.evidence.repair_evidence --check
npm --prefix tools/evidence run check:repair
.venv/bin/python -m tools.evidence.comparison_evidence --check
.venv/bin/python -m tools.evidence.comparison_visuals --check
npm --prefix tools/evidence run check:comparison
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
| `docs/evidence/data/comparison-artifacts.json` | Exact committed comparison artifact byte lengths; no latency claim |
| `docs/evidence/data/scaling.json` | Recorded bounded timing runs and environment |
| `docs/evidence/data/distribution-contract.json` | Exact reviewed release, solver, install, and nonclaim boundaries |
| `docs/evidence/captures/distribution.txt` | Real hosted release-verifier host facts, stdout, and exit status |
| `docs/assets/*.svg` | Live records consumed by dependency-free SVG builders |
| `docs/assets/*.png` | Pinned Resvg rendering of the SVG sources |
| `docs/assets/unitsentinel-demo.gif` | Declared transcript frames and delays |
| `docs/assets/comparison-demo.gif` | Fixed compatible/drift/indeterminate terminal frames and delays |
| `docs/evidence/comparison-demo/*` | Byte-identical comparison terminal frames and closed manifest |
| `docs/evidence/manifest.json` | Closed file set, byte counts, and SHA-256 digests |

## Rendering boundary

The recorder and renderer open evidence inputs nonblocking and accept only
bounded regular files under the declared evidence directories, so a FIFO
cannot stall validation before the regular-file check. The renderer rejects
symlinks, external SVG resources, scripts, images, DOCTYPE/entity
declarations, oversized documents, unexpected manifest fields, mixed frame
dimensions, and unsafe output targets. Individual outputs are bounded, and
retained output bytes have a 256 MiB aggregate ceiling. Writes are atomic per
file; the complete output set is not a multi-file transaction. The fixed
repair mode writes exactly one PNG.

Rendering uses DejaVu Sans with system-font loading disabled. For byte-identical
output on another host, provide the same regular font file through an absolute
`UNITSENTINEL_FONT_PATH`, or use one of the documented DejaVu system paths.
The current manifest records the exact committed output bytes.

The renderer assumes a POSIX little-endian host and excludes a hostile same-UID
process that can replace repository parent directories during execution.
