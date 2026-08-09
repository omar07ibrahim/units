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
export UNITSENTINEL_NODE="$(command -v node)"

.venv/bin/python -m tools.evidence.onnx_evidence --check
.venv/bin/python -m tools.evidence.generate --check
.venv/bin/python -m tools.evidence.distribution_visuals --check
npm --prefix tools/evidence run check
.venv/bin/python -m tools.evidence.repair_evidence --check
npm --prefix tools/evidence run check:repair
.venv/bin/python -m tools.evidence.comparison_evidence --check
.venv/bin/python -m tools.evidence.comparison_visuals --check
npm --prefix tools/evidence run check:comparison
```

The ONNX command rebuilds the synthetic ModelProto, production import/verify
captures, three actual rejection captures, receipt-derived SVGs, and
provenance. The general Python command re-executes the verified, conflict, and
strict-replay CLI paths; the repair-only command separately re-executes the
pinned non-mutating repair search; and the comparison-only command re-executes
the compatible, drift, and indeterminate comparisons. None remeasures the
timing benchmark. Node renders expected bytes in memory and compares them with
the committed files. No check publishes replacements.

The distribution-only check validates the canonical release contract and the
real hosted transcript, then derives both public SVG sources without running a
network download. The exact release job separately recreates the transcript
while executing the full hash-pinned source-to-installed-wheel verifier.

To intentionally refresh only the closed ONNX slice:

```bash
.venv/bin/python -m tools.evidence.onnx_evidence --record
npm --prefix tools/evidence run render
.venv/bin/python -m tools.evidence.onnx_evidence --check
npm --prefix tools/evidence run check
.venv/bin/python -m tools.evidence.generate --write-manifest
```

The recorder has a fixed 17-file source-record allowlist: one synthetic
ModelProto, one lowered graph, six CLI capture files, one provenance document,
four SVG sources, three byte-identical GIF frames, and one frame manifest. The
general renderer adds exactly four ONNX PNGs and one three-frame GIF. The model
comes from official `onnx.helper` APIs; it is deterministic portfolio evidence,
not a production export.

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

To intentionally refresh only the canonical comparison records and visuals:

```bash
.venv/bin/python -m tools.evidence.comparison_evidence --record
.venv/bin/python -m tools.evidence.comparison_visuals --record
npm --prefix tools/evidence run render:comparison
.venv/bin/python -m tools.evidence.comparison_evidence --check
.venv/bin/python -m tools.evidence.comparison_visuals --check
npm --prefix tools/evidence run check:comparison
.venv/bin/python -m tools.evidence.generate --write-manifest
```

This recorder can publish only the four fixed ratio graphs, three exact plans,
three raw result claims, six CLI captures, byte-size data, and comparison
provenance listed below. The visual recorder can publish only the six fixed
SVG sources, three byte-identical terminal frames, and their closed frame
manifest. The renderer can publish only the corresponding six PNGs and one
GIF. The evidence recorder runs the production CLI twice per case and requires
the text and JSON runs to emit the same strict result bytes.

To intentionally refresh only the closed distribution contract and its two
SVG sources after a reviewed verifier change:

```bash
.venv/bin/python -m tools.evidence.distribution_visuals --record
npm --prefix tools/evidence run render
.venv/bin/python -m tools.evidence.distribution_visuals --check
npm --prefix tools/evidence run check
.venv/bin/python -m tools.evidence.generate --write-manifest
```

The Python recorder has a four-file output allowlist: one canonical contract,
one hosted transcript, and two source-derived SVGs. The general renderer
rebuilds the public PNG set deterministically; unchanged legacy renderings must
remain byte-identical.

To intentionally refresh the legacy verify/conflict/replay evidence while
retaining the recorded benchmark:

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
| In what order does comparison fail closed? | [Comparison workflow SVG](../assets/comparison-workflow.svg) | [Comparison workflow PNG](../assets/comparison-workflow.png) |
| Which normalization identities agree or drift? | [Comparison lineage SVG](../assets/comparison-lineage-drift.svg) | [Comparison lineage PNG](../assets/comparison-lineage-drift.png) |
| How large are the exact comparison artifacts? | [Comparison artifact-size SVG](../assets/comparison-artifact-sizes.svg) | [Comparison artifact-size PNG](../assets/comparison-artifact-sizes.png) |
| What did a compatible comparison return? | [Compatible terminal SVG](../assets/compare-compatible-terminal.svg) | [Compatible terminal PNG](../assets/compare-compatible-terminal.png) |
| What did normalization drift return? | [Drift terminal SVG](../assets/compare-drift-terminal.svg) | [Drift terminal PNG](../assets/compare-drift-terminal.png) |
| What did an indeterminate comparison return? | [Indeterminate terminal SVG](../assets/compare-indeterminate-terminal.svg) | [Indeterminate terminal PNG](../assets/compare-indeterminate-terminal.png) |
| How does source become an offline-installed release? | [Distribution contract SVG](../assets/distribution-contract.svg) | [Distribution contract PNG](../assets/distribution-contract.png) |
| What did the exact hosted release verifier return? | [Distribution terminal SVG](../assets/distribution-terminal.svg) | [Distribution terminal PNG](../assets/distribution-terminal.png) |
| Where does static ONNX import fail closed? | [ONNX adapter architecture SVG](../assets/onnx-adapter-architecture.svg) | [ONNX adapter architecture PNG](../assets/onnx-adapter-architecture.png) |
| What canonical graph did the example lower to? | [ONNX lowered graph SVG](../assets/onnx-lowered-graph.svg) | [ONNX lowered graph PNG](../assets/onnx-lowered-graph.png) |
| What did successful ONNX import return? | [ONNX import terminal SVG](../assets/onnx-import-terminal.svg) | [ONNX import terminal PNG](../assets/onnx-import-terminal.png) |
| Which representative ONNX inputs were rejected? | [ONNX rejection matrix SVG](../assets/onnx-rejection-matrix.svg) | [ONNX rejection matrix PNG](../assets/onnx-rejection-matrix.png) |

The [7.6-second CLI demo GIF](../assets/unitsentinel-demo.gif), separate
[9-second comparison demo GIF](../assets/comparison-demo.gif), and
[8-second ONNX demo GIF](../assets/onnx-demo.gif) are derived presentation, not
primary records. Their declared frames cover verify/conflict/replay;
compatible/drift/indeterminate; and import/lowering/rejections respectively.
The canonical captures, claims, ModelProto, graph, provenance, and SVGs remain
the primary records.

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

### ONNX import evidence

- [Synthetic ONNX speed ModelProto](models/speed-contract.onnx)
- [Canonical lowered speed graph](contracts/onnx-speed.graph.json)
- [Human import transcript](captures/onnx-import.txt) and
  [canonical import receipt envelope](captures/onnx-import.json)
- [Human verification transcript](captures/onnx-verify.txt) and
  [canonical verification envelope](captures/onnx-verify.json)
- [Human rejection transcript](captures/onnx-rejections.txt) and
  [canonical rejection aggregation](captures/onnx-rejections.json)
- [Cross-bound ONNX provenance v2](onnx-provenance.json)

The 593-byte model is generated deterministically by `onnx.helper` and uses one
`Div` node over two static `float32[4,8]` inputs. It is synthetic and exists
only to expose the closed import path. The import receipt binds model, metadata,
checker, operator mapping, and canonical graph digests. The separately executed
verify capture establishes this graph's dimensional result.

The rejection JSON aggregates actual stable exit-4 diagnostics for one symbolic
shape, one unreviewed `Pow` operator, and one embedded initializer. It is not a
successful import receipt or an exhaustive compatibility matrix; every case
records `graph_published: false`. Provenance copies `external_data` and
`model_executed` from the production import receipt. Its
`network_access_required: false` field is a design property of import, not a
measurement of runner traffic.

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

### Hosted distribution contract

- [Canonical source-to-installed-wheel contract](data/distribution-contract.json)
- [Exact hosted verifier transcript](captures/distribution.txt)

The transcript is deliberately free of timestamps, runner paths, run IDs, and
commit SHAs. GitHub Actions reconstructs its host facts, displayed command,
actual verifier stdout, and exit status on exact CPython 3.12.3/Linux x86-64,
then requires byte-for-byte equality. The contract records both the verified
boundaries and the upstream provenance, signature, SLSA, and cross-platform
claims that are explicitly outside this check.

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
| Import static ONNX contract | [onnx-import.txt](captures/onnx-import.txt) | [onnx-import.json](captures/onnx-import.json) |
| Verify lowered ONNX graph | [onnx-verify.txt](captures/onnx-verify.txt) | [onnx-verify.json](captures/onnx-verify.json) |
| Reject three ONNX boundary cases | [onnx-rejections.txt](captures/onnx-rejections.txt) | [onnx-rejections.json](captures/onnx-rejections.json) |

The transcript commands use `.unitsentinel/evidence-run` as an isolated scratch
directory. The recorder creates it with private permissions, refuses to delete
an existing directory at that path, and removes only the directory it created.

### Cross-bindings and benchmark

- [Graph/result/certificate/replay provenance](provenance.json)
- [Repair graph/result/candidate provenance](repair-provenance.json)
- [Comparison graph/plan/result/semantic provenance](comparison-provenance.json)
- [ONNX model/contract/checker/graph/import provenance](onnx-provenance.json)
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
- [Comparison frame manifest and delays](comparison-demo/frames.json)
- [Compatible comparison frame](comparison-demo/frame-compatible.svg)
- [Drift comparison frame](comparison-demo/frame-drift.svg)
- [Indeterminate comparison frame](comparison-demo/frame-indeterminate.svg)
- [ONNX frame manifest and delays](onnx-demo/frames.json)
- [ONNX import frame](onnx-demo/frame-import.svg)
- [ONNX lowered-graph frame](onnx-demo/frame-lowering.svg)
- [ONNX rejection frame](onnx-demo/frame-rejections.svg)

The frames are byte-identical copies of their corresponding public terminal
SVG sources. The renderer rejects undeclared frames, mixed dimensions,
external resources, malformed delays, and any comparison frame order or delay
outside the compiled-in three-case sequence.

## Interpretation limits

- UnitSentinel verifies dimension, quantity kind, exact scale, and offset. It
  does not execute tensors or calculate anomaly scores.
- Shape is preserved as contract metadata; broadcasting and matrix-shape
  correctness are outside this verifier.
- ONNX import accepts only the documented static subset, executes no model, and
  establishes no exporter identity, deployment state, model quality, or
  scientific correctness.
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
