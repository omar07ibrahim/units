# UnitSentinel evidence ledger

This directory is the audit trail behind the images in the project README.
Every terminal capture comes from the production CLI, every graph and claim is
canonical JSON, and every public PNG/GIF is derived from a committed SVG or
frame manifest. The closed [evidence manifest](manifest.json) records the exact
file set, byte counts, and SHA-256 digests.

## Reproduce the current snapshot

From a checkout with the Python development environment installed:

```bash
npm --prefix tools/evidence ci --ignore-scripts

.venv/bin/python -m tools.evidence.generate --check
npm --prefix tools/evidence run check
.venv/bin/python -m tools.evidence.repair_evidence --check
npm --prefix tools/evidence run check:repair
.venv/bin/python -m tools.evidence.comparison_evidence --check
```

The Python command re-executes the verified, conflict, and strict-replay CLI
paths; the repair-only Python command separately re-executes the pinned
non-mutating repair search; and the comparison-only command re-executes pinned
compatible, drift, and indeterminate training/serving comparisons. None
remeasures the timing benchmark. The Node commands render expected bytes in
memory and compare them with the committed files. No check publishes
replacements.

To intentionally refresh only the repair slice:

```bash
.venv/bin/python -m tools.evidence.repair_evidence --record
npm --prefix tools/evidence run render:repair
.venv/bin/python -m tools.evidence.repair_evidence --check
npm --prefix tools/evidence run check:repair
.venv/bin/python -m tools.evidence.generate --write-manifest
```

Those write modes are closed to the repair transcript, canonical record,
repair provenance, lineage SVG, lineage PNG, and final manifest. They do not
publish the existing demo GIF, scaling snapshot, or legacy PNGs.

To intentionally refresh only the canonical comparison records:

```bash
.venv/bin/python -m tools.evidence.comparison_evidence --record
.venv/bin/python -m tools.evidence.comparison_evidence --check
.venv/bin/python -m tools.evidence.generate --write-manifest
```

This recorder can publish only the four fixed ratio graphs, three exact plans,
three raw result claims, six CLI captures, byte-size data, and comparison
provenance listed below. It runs the production CLI twice per case and requires
the text and JSON runs to emit the same strict result bytes.

To intentionally refresh deterministic evidence while retaining the recorded
benchmark:

```bash
.venv/bin/python -m tools.evidence.generate --record
npm --prefix tools/evidence run render
.venv/bin/python -m tools.evidence.generate --write-manifest
```

Use `--record-benchmark` instead of `--record` only when a new measured host
snapshot is intended. Benchmark timings are observations, not canonical
goldens.

## Public visual index

| Question | Accessible SVG source | PNG rendering |
| --- | --- | --- |
| Where are the verification boundaries? | [Verification pipeline SVG](../assets/verification-pipeline.svg) | [Verification pipeline PNG](../assets/verification-pipeline.png) |
| What physical feature graph was checked? | [Wheel anomaly contract SVG](../assets/wheel-anomaly-contract.svg) | [Wheel anomaly contract PNG](../assets/wheel-anomaly-contract.png) |
| What did a successful CLI run return? | [Verified terminal SVG](../assets/verify-terminal.svg) | [Verified terminal PNG](../assets/verify-terminal.png) |
| How did the serving-contract bug fail? | [Conflict terminal SVG](../assets/conflict-terminal.svg) | [Conflict terminal PNG](../assets/conflict-terminal.png) |
| Which tracked constraints form the conflict? | [Conflict core SVG](../assets/conflict-core.svg) | [Conflict core PNG](../assets/conflict-core.png) |
| How was one bounded repair proposal verified? | [Unit repair lineage SVG](../assets/unit-repair-lineage.svg) | [Unit repair lineage PNG](../assets/unit-repair-lineage.png) |
| How is a claim bound and replayed? | [Certificate lineage SVG](../assets/certificate-lineage.svg) | [Certificate lineage PNG](../assets/certificate-lineage.png) |
| What did strict replay return? | [Replay terminal SVG](../assets/replay-terminal.svg) | [Replay terminal PNG](../assets/replay-terminal.png) |
| How did bounded graph size affect this host? | [Scaling plot SVG](../assets/scaling.svg) | [Scaling plot PNG](../assets/scaling.png) |

The [7.6-second CLI demo GIF](../assets/unitsentinel-demo.gif) is presentation,
not the primary record. Its three frames loop in the order declared below.

## Canonical inputs and outputs

### Graph contracts

- [Verified wheel-anomaly graph](contracts/wheel-anomaly-verified.json)
- [Conflicting wheel-anomaly graph](contracts/wheel-anomaly-conflict.json)
- [Ratio training graph](contracts/ratio-training.json)
- [Compatible renamed serving graph](contracts/ratio-serving-renamed.json)
- [Reversed-divide serving graph](contracts/ratio-serving-reversed.json)
- [Underconstrained serving graph](contracts/ratio-serving-underconstrained.json)

The wheel-anomaly pair differs in one serving annotation:
`acceleration-si` is `meter-per-second-squared` in the verified graph and
`meter-per-second` in the conflict graph. Shape and dtype metadata stay the
same.

### Positive claim

- [Detached wheel-anomaly certificate](claims/wheel-anomaly.cert.json)

The certificate is content-addressed and unsigned. The CLI transcript and JSON
wrapper deliberately label its authentication as `not-provided`; authentication
is not a field in the closed certificate document. A reproduced replay
establishes current semantic reproduction, not issuer identity or issuance
provenance.

### Comparison plans and detached results

| Outcome | Caller-pinned plan | Raw unsigned result |
| --- | --- | --- |
| Compatible rename | [ratio-compatible.plan.json](plans/ratio-compatible.plan.json) | [ratio-compatible.result.json](claims/ratio-compatible.result.json) |
| Normalization drift | [ratio-drift.plan.json](plans/ratio-drift.plan.json) | [ratio-drift.result.json](claims/ratio-drift.result.json) |
| Serving indeterminate | [ratio-indeterminate.plan.json](plans/ratio-indeterminate.plan.json) | [ratio-indeterminate.result.json](claims/ratio-indeterminate.result.json) |

The compatible case deliberately renames every public value, so the two whole
lineage records have different content digests. Compatibility comes from equal
mapped output-normalization semantic digests. Reversing the divide operands
keeps both graphs dimensionally verified but changes that mapped semantic
digest; the comparison's sole mismatch code is
`normalization-lineage-drift`. Removing serving input units yields an
underconstrained verification and therefore no partial bindings or lineage
claim.

Plans and results are content-addressed but unsigned:
`authentication: not-provided`. The required plan pin proves that these CLI
runs consumed the expected bytes; it does not identify who approved the
mapping or prove that a deployment used either graph.

### Actual CLI captures

| Path | Human transcript | Canonical JSON record |
| --- | --- | --- |
| Verify and issue | [verify.txt](captures/verify.txt) | [verify.json](captures/verify.json) |
| Fail closed on conflict | [conflict.txt](captures/conflict.txt) | [conflict.json](captures/conflict.json) |
| Propose one non-applied repair | [repair.txt](captures/repair.txt) | [repair.json](captures/repair.json) |
| Strict detached replay | [replay.txt](captures/replay.txt) | [replay.json](captures/replay.json) |
| Compare compatible rename | [compare-compatible.txt](captures/compare-compatible.txt) | [compare-compatible.json](captures/compare-compatible.json) |
| Detect normalization drift | [compare-drift.txt](captures/compare-drift.txt) | [compare-drift.json](captures/compare-drift.json) |
| Abstain on underconstrained serving | [compare-indeterminate.txt](captures/compare-indeterminate.txt) | [compare-indeterminate.json](captures/compare-indeterminate.json) |

The transcript commands use `.unitsentinel/evidence-run` as an isolated scratch
directory. The recorder creates it with private permissions, refuses to delete
an existing directory at that path, and removes only the directory it created.

### Cross-bindings and benchmark

- [Graph/result/certificate/replay provenance](provenance.json)
- [Repair graph/result/candidate provenance](repair-provenance.json)
- [Comparison graph/plan/result/semantic provenance](comparison-provenance.json)
- [Exact comparison artifact byte lengths](data/comparison-artifacts.json)
- [Measured scaling runs](data/scaling.json)

The recorded benchmark uses identity chains with 1, 8, 32, 128, and 256
operations. Each plotted point is the median of three raw runs for
verification-plus-certificate issuance or strict replay. It is a single-host
engineering snapshot, not a real-world accuracy result, cross-machine ranking,
or performance guarantee.

### Demo sources

- [Frame manifest and delays](demo/frames.json)
- [Conflict frame](demo/frame-01.svg)
- [Verified frame](demo/frame-02.svg)
- [Replay frame](demo/frame-03.svg)

The frames are byte-identical copies of the corresponding public terminal SVG
sources. The renderer rejects undeclared frames, mixed dimensions, external
resources, and malformed delays.

## Interpretation limits

- UnitSentinel verifies dimension, quantity kind, exact scale, and offset. It
  does not execute tensors or calculate anomaly scores.
- Shape is preserved as contract metadata; broadcasting and matrix-shape
  correctness are outside this verifier.
- `core_minimal: true` means deletion-minimal under the bounded shrink
  procedure, not minimum-cardinality.
- A verified dimensional contract does not establish scientific correctness.
- A `proposed` repair is one exact, freshly verified annotation replacement
  under the recorded registry and search limits. It is not automatically
  applied and does not establish scientific intent.
- A compatible comparison is scoped to one exact plan and two sequential file
  snapshots. It does not prove runtime deployment, coexistence, statistical
  drift, model quality, or scientific correctness.
- Timings include the recorded Python, UnitSentinel, and Z3 versions shown in
  [the benchmark data](data/scaling.json).

The generation implementation, dependency pins, host assumptions, and renderer
limits are documented in the
[evidence tooling guide](../../tools/evidence/README.md).
