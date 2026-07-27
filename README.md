# UnitSentinel

Dimensional proof certificates for scientific and machine-learning computation
graphs.

UnitSentinel catches a class of production bugs that tensor shapes and dtypes
cannot express: a graph can be structurally valid while metres, seconds,
temperature kinds, scales, or offsets are physically incompatible. It compiles
a bounded canonical graph into tracked exact constraints, fails closed unless
every public contract is unique, and can issue an unsigned, content-addressed
certificate for a positive result.

> **Status:** the v0.1 verification core, canonical graph codec, 33-unit
> registry, deterministic CLI, detached certificate codec, independent strict
> replay, bounded verification-backed annotation repair, canonical
> training/serving comparison engine, normalization-lineage comparison, and
> strict comparison-result codec are implemented. The fail-closed comparison
> CLI and its reproducible compatible/drift/indeterminate evidence are also
> implemented. Closed-subset ONNX lowering remains future work.

![Implemented UnitSentinel verification pipeline and fail-closed outcomes](docs/assets/verification-pipeline.png)

*Current implementation. Canonical bytes cross a bounded decoder; tracked
constraints pass through Z3, exact extraction, and independent semantic replay
before a positive claim can be issued. The [accessible SVG
source](docs/assets/verification-pipeline.svg) contains the same content.*

## A real end-to-end run

![Recorded UnitSentinel conflict verification and strict replay demo](docs/assets/unitsentinel-demo.gif)

*A 7.6-second loop rendered from the exact committed CLI transcripts:
conflict, corrected graph with certificate issuance, then strict replay.
Equivalent text paths are available for
[conflict](docs/evidence/captures/conflict.txt),
[verification](docs/evidence/captures/verify.txt), and
[replay](docs/evidence/captures/replay.txt).*

The demo is not a mock terminal. Its graphs, stdout, exit codes, JSON records,
certificate, frames, and output hashes are all committed in the
[evidence ledger](docs/evidence/README.md).

## The failure mode

Production ML contracts usually preserve tensor dtypes and shapes while the
physical meaning of a feature remains in preprocessing code, prose, or loose
metadata. Several dangerous changes therefore remain structurally valid:

- serving sends `km/h` while training consumed `m/s`;
- an absolute Celsius temperature is treated as a temperature difference;
- a normalization constant comes from a sensor with a different scale;
- two tensors share a shape and dtype but carry incompatible dimensions;
- an exported graph preserves numerical operations but loses the unit contract
  around its inputs and outputs.

[ONNX tensor types](https://onnx.ai/onnx/intro/concepts.html#supported-types)
describe element types and shapes.
[TensorFlow Data Validation](https://www.tensorflow.org/tfx/guide/tfdv) checks
schema properties and statistical skew. UnitSentinel targets another layer:
exact physical dimension, quantity kind, scale, and offset across the
computation itself.

## The example contract

The committed example is a small physics-informed feature pipeline rather than
a toy `metres + seconds` expression:

1. subtract current and previous wheel speed in `km/h`;
2. convert the speed delta to `m/s`;
3. convert the sample period from `ms` to `s`;
4. derive acceleration as `Δv / Δt`;
5. normalize by a reference acceleration;
6. apply a sigmoid only after the value is dimensionless.

![Verified physics-informed wheel anomaly dimensional contract](docs/assets/wheel-anomaly-contract.png)

*Dimensions, exact scales, and quantity kinds come from the live verified
contract record. UnitSentinel checks the feature contract; it does not execute
the tensors or calculate an anomaly score. [Inspect the accessible
SVG](docs/assets/wheel-anomaly-contract.svg).*

The verified graph is 2,072 canonical bytes and has SHA-256:

```text
139e3e3d99d64c3d9cde89e9e1f116f09452c3532eaaee2e0513c71a0f2ada3c
```

## Install and verify

UnitSentinel supports Python 3.11 and newer.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'

mkdir -p .unitsentinel/demo
.venv/bin/python -I examples/build_wheel_anomaly_contract.py \
    --variant verified \
    > .unitsentinel/demo/wheel-anomaly.json

.venv/bin/python -m unitsentinel verify \
    .unitsentinel/demo/wheel-anomaly.json \
    --certificate .unitsentinel/demo/wheel-anomaly.cert.json
```

Certificate output is deliberately no-overwrite: choose a fresh path for a
second run. Successful human output includes ten exact contracts, both graph
and result digests, the registry fingerprint, solver checks, and an explicit
authentication disclaimer.

![Genuine successful UnitSentinel CLI certificate issuance](docs/assets/verify-terminal.png)

*[Plain text](docs/evidence/captures/verify.txt) and
[canonical JSON](docs/evidence/captures/verify.json) are the sources behind
this screenshot. The PNG is derived from the
[terminal SVG](docs/assets/verify-terminal.svg).*

Machine consumers can request the same closed result record:

```bash
.venv/bin/python -m unitsentinel verify \
    .unitsentinel/demo/wheel-anomaly.json \
    --json
```

Verification, replay, repair, and comparison reports use structured stdout.
Usage, input/output, expected-digest preflight, interruption, and redacted
internal failures use stderr.

| Exit | Meaning |
| ---: | --- |
| `0` | Graph verified, certificate reproduced, or comparison compatible |
| `1` | Dimensional conflict |
| `2` | Underconstrained public contract |
| `3` | Verification or replay/repair/comparison is indeterminate |
| `4` | Input, output, or canonicality failure |
| `5` | Replay mismatch, comparison drift, or expected-digest mismatch |
| `6` | Repair search abstained without one unique proposal |
| `64` | Command-line usage error |
| `70` | Redacted internal failure |
| `130` | Interrupted execution |

## Fail closed on a shape-valid bug

The conflicting variant changes exactly one annotation:
`acceleration-si` claims `meter-per-second` even though `Δv / Δt` derives
`meter-per-second-squared`. Shapes and dtypes are unchanged.

```bash
.venv/bin/python -I examples/build_wheel_anomaly_contract.py \
    --variant conflict \
    > .unitsentinel/demo/wheel-anomaly-conflict.json

.venv/bin/python -m unitsentinel verify \
    .unitsentinel/demo/wheel-anomaly-conflict.json \
    --certificate .unitsentinel/demo/should-not-exist.cert.json
```

![Genuine UnitSentinel CLI dimensional conflict output](docs/assets/conflict-terminal.png)

*The command exits `1`; no positive certificate is written. See the
[plain transcript](docs/evidence/captures/conflict.txt),
[canonical record](docs/evidence/captures/conflict.json), or
[accessible terminal SVG](docs/assets/conflict-terminal.svg).*

UnitSentinel returns the actual four-item deletion-minimal tracked witness
rather than a generic “unit mismatch” message:

![Tracked UnitSentinel deletion-minimal conflict core explanation](docs/assets/conflict-core.png)

*The witness connects the wrong serving declaration to the divide operation and
both explicit conversions. “Deletion-minimal” does not mean
minimum-cardinality. [Inspect the accessible
SVG](docs/assets/conflict-core.svg).*

## Bounded repair without automatic application

The repair command can mechanically investigate one explicit unit annotation
from a fresh minimal conflict core. It removes that annotation in memory,
requires the relaxed graph to verify, finds exact canonical registry matches,
and freshly verifies each bounded candidate. It never edits the input graph or
claims to infer scientific intent.

```bash
.venv/bin/python -m unitsentinel repair \
    docs/evidence/contracts/wheel-anomaly-conflict.json \
    --max-sites 1 \
    --max-candidates 1 \
    --max-verifier-calls 3 \
    --max-work-items 64 \
    --total-timeout-ms 30000
```

![Source-derived UnitSentinel conflict to verified repair proposal lineage](docs/assets/unit-repair-lineage.png)

*The production CLI found exactly one verified candidate under the pinned
registry and completed bounds: `acceleration-si` changes from
`meter-per-second` to no declaration, then to
`meter-per-second-squared`. The source remains unchanged and the output states
`application: not-performed`. Inspect the [exact transcript](docs/evidence/captures/repair.txt),
[canonical JSON](docs/evidence/captures/repair.json),
[cross-bound provenance](docs/evidence/repair-provenance.json), or
[accessible SVG](docs/assets/unit-repair-lineage.svg).*

The repaired candidate retains the source graph identifier
`wheel-anomaly-conflict`; it is not byte-identical to the separately committed
verified fixture. `proposed` establishes a unique mechanical replacement under
the recorded registry and limits—not scientific correctness or permission to
apply it. The complete fail-closed contract is documented in
[Verified unit-annotation repair v1](docs/unit-repair-v1.md).

## Compare training and serving without guessing

Two graphs can each be dimensionally verified and still disagree about their
public interface or the normalization computation behind an output.
`unitsentinel compare` uses an explicit canonical plan instead of matching
names, positions, embeddings, or samples. The caller must pin the exact plan
SHA-256.

![Fail-closed UnitSentinel training-serving comparison workflow](docs/assets/comparison-workflow.png)

*The implemented order is part of the trust boundary: validate limits; hash
and pin raw plan bytes before decoding; check the registry before opening
graphs; hash each graph before decoding; freshly verify both sides; rederive
normalization lineage; strictly encode the detached result; publish a new
private result file if `--result` was requested; only then write stdout.
[Inspect the accessible SVG](docs/assets/comparison-workflow.svg).*

Run the recorded lineage-drift case with a fresh result path:

```bash
mkdir -p .unitsentinel/demo

.venv/bin/python -m unitsentinel compare \
    docs/evidence/plans/ratio-drift.plan.json \
    --training-graph docs/evidence/contracts/ratio-training.json \
    --serving-graph docs/evidence/contracts/ratio-serving-reversed.json \
    --expect-plan-sha256 \
    9e0163fba563e9bf73114fd756c5edfe0c3fd2bf2e9a0dc6d555e38b75765009 \
    --result .unitsentinel/demo/ratio-drift.result.json
```

The command exits `5` with a valid drift report. Both graphs freshly verify,
all three public bindings agree dimensionally, and the only mismatch is
`normalization-lineage-drift` for the mapped output. Reversing the ordered
divide operands changes the output-normalization semantic digest even though
shape, dtype, explicit units, dimension, kind, scale, and offset still match.

![Source-derived UnitSentinel normalization lineage comparison](docs/assets/comparison-lineage-drift.png)

*The content digests preserve graph-local diagnostics, while whole-lineage
semantic digests provide rename-insensitive reviewer evidence. Compatibility
is decided from mapped interface metadata and per-output normalization
digests—not from the whole-lineage semantic digest. The compatible rename has
equal mapped output digests; the reversed divide has unequal mapped output
digests and exactly one reported mismatch. Every digest comes from the
committed strict claims. [Inspect the accessible
SVG](docs/assets/comparison-lineage-drift.svg).*

The same fixed fixture family records all three closed outcomes:

| Case | Exit | What was established | Raw result |
| --- | ---: | --- | --- |
| Compatible rename | `0` | Both sides verified; three explicit bindings and mapped output normalization agree under plan `2038cbb9…` | [13,276-byte claim](docs/evidence/claims/ratio-compatible.result.json) |
| Ordered lineage drift | `5` | Both sides verified; only the mapped output normalization digest differs under plan `9e0163fb…` | [13,327-byte claim](docs/evidence/claims/ratio-drift.result.json) |
| Serving underconstrained | `3` | Serving verification is not unique; no bindings or lineages are partially published under plan `12e6f514…` | [2,378-byte claim](docs/evidence/claims/ratio-indeterminate.result.json) |

![Exact UnitSentinel comparison artifact byte sizes](docs/assets/comparison-artifact-sizes.png)

*These are exact committed artifact byte lengths, not latency, throughput,
accuracy, or scalability measurements. The source table is
[comparison-artifacts.json](docs/evidence/data/comparison-artifacts.json);
the accessible plot source is
[comparison-artifact-sizes.svg](docs/assets/comparison-artifact-sizes.svg).*

![Recorded UnitSentinel compatible drift and indeterminate CLI demo](docs/assets/comparison-demo.gif)

*The loop is rendered from the same committed full terminal SVGs and declared
frame delays. It is presentation, while the
[comparison provenance](docs/evidence/comparison-provenance.json), strict raw
claims, and exact text/JSON captures are the primary records.*

<details>
<summary>Open the three complete terminal captures</summary>

### Compatible under the pinned plan

![Genuine UnitSentinel compatible comparison terminal output](docs/assets/compare-compatible-terminal.png)

[Text](docs/evidence/captures/compare-compatible.txt) ·
[canonical JSON](docs/evidence/captures/compare-compatible.json) ·
[accessible SVG](docs/assets/compare-compatible-terminal.svg)

### Normalization lineage drift

![Genuine UnitSentinel normalization drift terminal output](docs/assets/compare-drift-terminal.png)

[Text](docs/evidence/captures/compare-drift.txt) ·
[canonical JSON](docs/evidence/captures/compare-drift.json) ·
[accessible SVG](docs/assets/compare-drift-terminal.svg)

### Indeterminate serving contract

![Genuine UnitSentinel indeterminate comparison terminal output](docs/assets/compare-indeterminate-terminal.png)

[Text](docs/evidence/captures/compare-indeterminate.txt) ·
[canonical JSON](docs/evidence/captures/compare-indeterminate.json) ·
[accessible SVG](docs/assets/compare-indeterminate-terminal.svg)

</details>

The result file is canonical, strict-decoder round-trippable, unsigned, and
written with mode `0600` through atomic no-overwrite publication before
stdout. `compatible` means only “compatible under these exact plan, graph,
registry, and solver-limit bindings.” It does not authenticate plan approval,
prove deployment or simultaneous file state, execute a model, validate
broadcasting/matmul, measure statistical drift, or establish scientific
correctness. The complete byte and execution contract is documented in
[training-serving comparison v1](docs/training-serving-comparison-v1.md).

## Certificates and independent replay

A positive certificate binds:

- graph schema and SHA-256;
- registry version and SHA-256;
- verifier and solver versions;
- exact solver limits;
- ordered inferred contracts;
- the source-labelled constraint catalog;
- the complete verified result digest.

It is canonical and content-addressed, but not signed. UnitSentinel therefore
prints `authentication: not-provided` instead of implying provenance it cannot
establish.

![Content-addressed UnitSentinel certificate and replay lineage](docs/assets/certificate-lineage.png)

*Graph, registry, toolchain, result, certificate, and replay identities are
taken from the committed claim. `REPRODUCED` means semantic reproduction, not
issuer authentication. [Inspect the accessible
SVG](docs/assets/certificate-lineage.svg).*

Replay can pin the expected certificate bytes and require the current toolchain
to match the claim:

```bash
.venv/bin/python -m unitsentinel replay \
    docs/evidence/claims/wheel-anomaly.cert.json \
    --graph docs/evidence/contracts/wheel-anomaly-verified.json \
    --expect-sha256 \
    e93cc87cd72c6ede9cf8d324bfb41b2eb2bdcea6cb0aa6fea7aed4696009ab1a \
    --strict-toolchain
```

![Genuine UnitSentinel strict certificate replay output](docs/assets/replay-terminal.png)

*Strict replay recomputes the certificate digest, checks graph/registry and
toolchain bindings, replays pure semantic witnesses, then performs a fresh
bounded uniqueness verification. Sources:
[text](docs/evidence/captures/replay.txt),
[JSON](docs/evidence/captures/replay.json), and
[SVG](docs/assets/replay-terminal.svg).*

## Why a solver belongs here

Local arithmetic is enough when every intermediate unit is already known. Real
graphs are only partially annotated. UnitSentinel creates exact expressions
for seven SI dimension exponents, quantity kind, scale, and offset, then asks
two distinct questions:

1. is the tracked system satisfiable?
2. can any observable contract take a different model value?

Only a satisfiable system with no alternate observable model is `verified`.
Every extracted rational value is checked against domain bounds and replayed by
independent Python semantics before publication. A timeout, memory boundary,
non-rational model value, unsupported operation, or out-of-domain exponent
fails closed.

Tracked assertions retain source identities such as:

```text
declaration/acceleration-si/unit
operation/derive-acceleration/dimension
operation/normalize-sample-period/dimension
operation/normalize-speed-delta/dimension
```

Solver-generated names and raw diagnostics never enter public output.

## Measured bounded scaling

The repository includes one measured snapshot over identity chains of 1, 8,
32, 128, and 256 operations. Each point is the median of three recorded runs.

![Measured UnitSentinel verification and strict replay scaling](docs/assets/scaling.png)

*This plot reports wall-clock verification-plus-issuance and strict replay on
the recorded Python/Z3/Linux environment. It is not an accuracy benchmark,
cross-machine ranking, or performance guarantee. Raw runs and environment:
[scaling.json](docs/evidence/data/scaling.json); accessible source:
[scaling.svg](docs/assets/scaling.svg).*

## What runs today

The implementation includes:

- immutable vectors over the seven SI base dimensions with bounded rational
  exponents;
- exact unit scales and affine offsets without float coercion;
- distinct linear, absolute-temperature, and temperature-difference kinds;
- an immutable 33-unit registry with canonical serialization and a pinned
  SHA-256 fingerprint;
- a closed topological graph IR with one producer per non-input value;
- scalar types, bounded concrete/symbolic shapes, and explicit unit
  annotations;
- a byte-level decoder that rejects duplicate keys, floats, noncanonical JSON,
  unknown fields, invalid topology, and oversized inputs;
- a bounded canonical, unsigned training/serving plan with explicit,
  duplicate-free interface mappings and graph/registry digest bindings;
- a fresh-verification comparison engine with caller-trusted plan pinning,
  exact public-occurrence coverage, semantic replay, and deterministic drift
  codes;
- bounded, content-addressed normalization lineage with mapped logical roots,
  exact inferred metadata, output routing, and internal-rename invariance;
- fail-closed cross-graph normalization-lineage comparison over two freshly
  verified, graph-rederived lineages, including repeated-site multiplicity;
- a strict 32 MiB canonical JSON codec for bounded, unsigned comparison-result
  claims with exact nested digest and model round-trip checks;
- structural preflight limits on bytes, nesting, tokens, nodes, and items;
- exact constraints for all 14 supported graph operations;
- alternate-model uniqueness checks;
- deterministic tracked-core shrinking within a fixed check budget;
- monotonic per-check and whole-run deadlines plus solver memory bounds;
- independent semantic replay of extracted models;
- canonical verification results, proof certificates, and replay reports;
- bounded, non-mutating, verification-backed unit-annotation proposals;
- a deterministic CLI with caller-pinned comparison plans, bounded
  regular-file reads, stable domain exits, and atomic private no-overwrite
  certificate/result writes.

The [canonical graph contract](docs/graph-format.md),
[registry snapshot](docs/registry.md), and
[architecture boundary](docs/architecture.md) specify the core. The
[certificate and replay contract](docs/certificate-format.md) documents the
detached claim byte boundary and replay ordering. The
[training-serving comparison contract](docs/training-serving-comparison-v1.md)
defines the explicit alignment plan, fresh engine, and strict unsigned result
byte boundary without claiming that decoding proves freshness or that a plan
digest authenticates who approved its mapping.

## Trust boundary

Verification, replay, repair, and comparison require no network and never
execute model code. The CLI does not consume stdin; it accepts path-backed
regular files and rejects FIFOs, oversized documents, duplicate JSON fields,
executable extension hooks, unsafe output targets, and symlinks at the input
leaf or final parent component. Earlier intermediate path components are not
walked individually. Every open descriptor must be a regular file, and bounded
nonblocking reads still cap a file that grows after its initial `fstat`.
Certificate and comparison-result writes use private no-overwrite temporary
files, file and directory durability checks, and atomic publication before
stdout.

Comparison reads the plan, training graph, and serving graph in that order as
sequential descriptor snapshots; it does not claim an atomic simultaneous
snapshot of all three paths. Human/JSON reports intentionally repeat
user-supplied semantic graph and contract identifiers, so stdout is not a
private channel.

The threat model excludes a hostile same-UID process that can rewrite the
installed verifier, solver, or repository parent directories during execution.
A caller-trusted expected digest can detect certificate-byte substitution;
successful replay establishes current semantic agreement. Neither establishes
author identity or proves that the claimed issuance happened.

## Deliberate non-goals

- Claiming dimensional consistency proves scientific correctness.
- Executing tensor payloads or validating broadcasting/matmul shapes.
- Full UCUM compatibility.
- Currency, calendar arithmetic, logarithmic units, or contextual chemistry
  conversion.
- Guessing scientific intent from variable names.
- Executing user Python, model code, plugins, URLs, or import paths.
- Applying an LLM-generated repair without formal re-verification.

## Reproduce the visual evidence

The recorder and renderer are part of the repository:

```bash
npm --prefix tools/evidence ci --ignore-scripts
npm --prefix tools/evidence run audit

.venv/bin/python -m tools.evidence.generate --check
npm --prefix tools/evidence run check
.venv/bin/python -m tools.evidence.repair_evidence --check
npm --prefix tools/evidence run check:repair
.venv/bin/python -m tools.evidence.comparison_evidence --check
.venv/bin/python -m tools.evidence.comparison_visuals --check
npm --prefix tools/evidence run check:comparison
```

To refresh only the deterministic repair records and repair rendering:

```bash
.venv/bin/python -m tools.evidence.repair_evidence --record
npm --prefix tools/evidence run render:repair
.venv/bin/python -m tools.evidence.generate --write-manifest
```

To refresh only the deterministic comparison records and visuals:

```bash
.venv/bin/python -m tools.evidence.comparison_evidence --record
.venv/bin/python -m tools.evidence.comparison_visuals --record
npm --prefix tools/evidence run render:comparison
.venv/bin/python -m tools.evidence.comparison_evidence --check
.venv/bin/python -m tools.evidence.comparison_visuals --check
npm --prefix tools/evidence run check:comparison
.venv/bin/python -m tools.evidence.generate --write-manifest
```

The general recorder and renderer remain available when intentionally
refreshing the complete legacy evidence set:

```bash
.venv/bin/python -m tools.evidence.generate --record
npm --prefix tools/evidence run render
.venv/bin/python -m tools.evidence.generate --write-manifest
```

The timing snapshot changes only through the explicit
`--record-benchmark` mode. The [evidence ledger](docs/evidence/README.md),
[generation guide](tools/evidence/README.md), and
[closed manifest](docs/evidence/manifest.json) document every input and output.

## Local quality gates

The current suite contains 418 unit, integration, adversarial, and evidence
tests with 97% statement coverage, 94% branch coverage, and 96% combined
statement/branch coverage.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/coverage run -m unittest discover -s tests
.venv/bin/coverage report

.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src tools/evidence tools/measure_comparison_result_boundary.py
.venv/bin/python -m build
.venv/bin/pip-audit

.venv/bin/python -m tools.evidence.generate --check
npm --prefix tools/evidence run check
.venv/bin/python -m tools.evidence.repair_evidence --check
npm --prefix tools/evidence run check:repair
.venv/bin/python -m tools.evidence.comparison_evidence --check
.venv/bin/python -m tools.evidence.comparison_visuals --check
npm --prefix tools/evidence run check:comparison
```

The evidence tests independently validate canonical graph/certificate
bindings, the closed manifest, SVG accessibility and self-containment, PNG
chunk CRCs and decompressed dimensions, GIF frame timing/loop structure,
README coverage, and secret/PII exclusions.

## Roadmap

| Slice | Status |
| --- | --- |
| Exact values, units, quantities, affine semantics | Complete |
| Immutable content-addressed unit registry | Complete |
| Bounded canonical graph IR and strict decoder | Complete |
| Tracked exact verification and fail-closed outcomes | Complete |
| Detached positive certificates and independent replay | Complete |
| Production CLI and reproducible visual evidence | Complete |
| Bounded formally reverified repair candidates | Complete |
| Canonical training/serving alignment plan | Complete |
| Fresh-verified training/serving comparison engine | Complete |
| Bounded normalization-lineage extraction | Complete |
| Fresh cross-graph normalization-lineage comparison | Complete |
| Strict bounded comparison-result codec | Complete |
| Comparison CLI and reproducible visual evidence | Complete |
| Closed-subset ONNX metadata adapter | Planned |
| Grouped synthetic fault benchmark with abstention metrics | Planned |

The repository intentionally has no license yet. Licensing is a decision for
Omar before third-party reuse is invited.
