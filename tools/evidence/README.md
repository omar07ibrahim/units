# Reproducible evidence pipeline

This directory turns implemented UnitSentinel behavior into reviewable
portfolio evidence. It does not synthesize terminal output or edit result
records by hand:

- the Python recorder executes the production CLI for verified, conflict, and
  strict-replay paths;
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

## Verify freshness

Both checks are required:

```bash
.venv/bin/python -m tools.evidence.generate --check
npm --prefix tools/evidence run check
```

The Python check replays the deterministic source evidence without re-running
the timing benchmark. The renderer check builds every expected PNG and GIF in
memory and compares exact bytes without publishing output. The manifest command
also refuses to bless raster assets until the renderer check passes.

## Output map

| Output | Source of truth |
| --- | --- |
| `docs/evidence/contracts/*.json` | Production graph builder and canonical codec |
| `docs/evidence/captures/*` | Actual production CLI stdout and exit status |
| `docs/evidence/claims/*.json` | Actual positive certificate |
| `docs/evidence/provenance.json` | Cross-bound graph, result, registry, certificate, and replay identities |
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
the complete output set is not a multi-file transaction.

Rendering uses DejaVu Sans with system-font loading disabled. For byte-identical
output on another host, provide the same regular font file through an absolute
`UNITSENTINEL_FONT_PATH`, or use one of the documented DejaVu system paths.
The current manifest records the exact committed output bytes.

The renderer assumes a POSIX little-endian host and excludes a hostile same-UID
process that can replace repository parent directories during execution.
