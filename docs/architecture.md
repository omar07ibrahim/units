# Architecture and verification boundary

This document separates the implemented verifier, certificate/replay,
bounded-repair, canonical comparison-plan, and CLI boundaries from the later
comparison engine and adapter design. The implementation map at the end is the
authoritative status summary.

## Design objective

Given a bounded computation graph, a versioned unit registry, and optional unit
annotations, the current verifier determines one of four outcomes:

1. `verified`: every value has a unique, consistent dimensional interpretation;
2. `underconstrained`: more than one interpretation remains;
3. `conflict`: a tracked subset of declarations and operations is inconsistent;
4. `unknown`: the trusted verification result could not be established.

Only `verified` may produce a positive proof certificate. A timeout, unsupported
operation, malformed graph, or non-unique interpretation is never converted
into success.

Stable `unknown` reasons cover timeout, memory/resource failure, solver unknown,
contract rejection, out-of-domain model extraction, and internal
inconsistency. Raw solver diagnostics are not exposed.

## Quantity model

### Dimension

A dimension is a seven-component vector ordered as:

```text
length, mass, time, electric-current,
thermodynamic-temperature, amount-of-substance, luminous-intensity
```

Each component is an exact rational exponent. Multiplication adds vectors,
division subtracts them, and exponentiation multiplies every component by the
same bounded rational. Equality is exact; floating-point tolerances do not
participate in type checking.

### Unit

A unit contains:

- a canonical identifier;
- one dimension vector;
- an exact rational scale relative to a canonical reference unit;
- an exact rational offset when the conversion is affine;
- a quantity kind that distinguishes linear values, absolute temperatures, and
  temperature differences.

An affine unit is not generally closed under multiplication, division, or
exponentiation. An absolute Celsius magnitude may be normalized onto the
canonical absolute-temperature reference scale, but that conversion does not
reclassify it as a linear quantity. Direct temperature units must declare
whether they represent an absolute value or a difference; a temperature
difference has no absolute offset.

### Tensor value

The core verifier reasons about a value contract, not tensor payload bytes:

- scalar element type;
- optional symbolic shape;
- dimension or dimension variable;
- optional concrete unit;
- semantic quantity kind;
- source declaration identity.

Shape and dimensional inference are separate. A dimensionally valid graph can
still have an invalid tensor shape, and vice versa.

## Canonical graph format

The implemented core decoder accepts strict JSON with:

- a schema version;
- a bounded, unique list of graph inputs;
- a bounded, unique topological list of nodes;
- declared outputs;
- explicit operation names and closed parameter objects;
- optional unit declarations tied to values;
- no executable code, import path, URL, or extension hook.

Parsing rejects duplicate JSON keys, unknown fields, invalid UTF-8, noncanonical
numbers, cycles, forward references, excessive nesting, and values outside
declared limits. Public witnesses use canonical value/node source identifiers
and constraint identifiers, not host filesystem paths.

## Constraint compilation

Each value receives seven exact dimension expressions plus exact scale, offset,
and semantic quantity-kind expressions. Operations emit labelled equations:

| Operation family | Dimensional rule |
| --- | --- |
| identity | output contract equals input contract |
| add, subtract, minimum, maximum | dimensions match; kind/transform follow the explicit arithmetic truth table |
| multiply, matrix multiplication | output dimension is left plus right |
| divide | output dimension is left minus right |
| power by rational constant | output equals input multiplied by exponent |
| exponential, logarithm, sigmoid, softmax | input and output are dimensionless |
| explicit conversion | dimensions equal; unit transform is declared |

Every equation is tracked by a stable constraint identifier derived from its
graph declaration. Solver-generated names never appear in user-facing output.

### Inference

A satisfiable system is not automatically verified. UnitSentinel asks whether
every dimension component, quantity kind, scale, and offset has a unique value.
If two models assign different values to any observable contract, the graph is
`underconstrained`.

All extracted solver rationals pass a 256-bit numerator/denominator boundary.
Dimension exponents additionally require an absolute numerator no greater than
64 and a denominator no greater than 12 before they enter the domain model.

### Conflict cores

Tracked assertions provide an initial unsatisfiable core. Because solver cores
need not be minimal, UnitSentinel deterministically shrinks the sorted core
within a fixed check budget. The resulting core is a diagnostic witness, not a
proof that every omitted declaration is irrelevant to scientific intent.

## Bounded repair

The implemented repair boundary remains downstream of verification and never
mutates its input. Its v1 operator is deliberately narrow: it can propose
replacing one explicit canonical unit annotation that participates in a freshly
verified deletion-minimal conflict core.

For each bounded eligible site, the implementation removes only that annotation
in memory and requires the relaxed graph to be `verified`. It then enumerates
registry units whose dimension, quantity kind, scale, and offset exactly match
the relaxed value contract, restores each candidate annotation in memory, and
freshly verifies the candidate graph. It returns a proposal only when exactly
one candidate verifies. Unknown outcomes or exhausted limits are indeterminate;
multiple verified candidates, a remaining conflict, or no exact canonical
match cause abstention.

The result binds the source, relaxed, and repaired graph and verification
digests. The CLI exposes the same bounded search as a read-only canonical JSON
report with `application: not-performed`; it has no apply or output-file
surface. A verified dimensional proposal does not establish scientific intent
or permission to change a model. The complete acceptance protocol and aggregate
limits are documented in [unit-repair-v1.md](unit-repair-v1.md).

## Proof certificate

A canonical positive certificate currently binds:

- graph schema version and SHA-256;
- unit registry version and SHA-256;
- verifier version, semantic contract, and solver version;
- the exact configured solver/resource limits and checks performed;
- ordered inferred contracts;
- the complete ordered source-labelled constraint catalog;
- the verified result SHA-256;
- certificate schema version.

The complete canonical certificate bytes are content-addressed by SHA-256; the
digest is computed outside the closed certificate document.

Only an exact runtime `VerificationResult` with status `verified`, complete
contract coverage, matching graph/registry bindings, and freshly revalidated
sources can be issued. Negative and indeterminate outcomes have no certificate
shape.

Detached replay decodes the canonical claim, recomputes its digest, compares
graph and registry identities, compares the current source-labelled catalog,
optionally pins the toolchain, replays every claimed contract against the graph
and registry through solver-independent Python semantics, then performs a fresh
bounded uniqueness verification. It returns `reproduced`, `mismatch`, or
`indeterminate`.

A certificate is an unsigned claim about one bounded verifier execution.
Successful replay establishes current semantic agreement, not that issuance
happened, author identity, or scientific validity. The exact byte contract is
documented in [certificate-format.md](certificate-format.md).

## Training and serving comparison

The canonical alignment plan and bounded byte codec are implemented. They bind
the exact training graph, serving graph, registry snapshot, and explicit
logical interface mapping without making a compatibility claim. The
fresh-verification comparison engine is planned, not yet implemented.

Mappings operate on public `(role, value_id)` occurrences. Every occurrence
must be covered exactly once before comparison; one-sided bindings express an
explicit absence, cross-role bindings preserve a role drift, and different
identifiers are treated as an intentional rename only under the caller-trusted
plan that pairs them. Constructing or decoding a plan does not establish
endpoint membership, total coverage, or permission; the later engine must
validate membership and coverage against both bound graphs. It must also bind
the exact plan digest, expose `authentication: not-provided`, and allow a
caller-trusted expected digest or allow-list policy to fail closed. No fuzzy,
positional, or alias-based matching is permitted. The complete plan contract
is documented in
[training-serving-comparison-v1.md](training-serving-comparison-v1.md).

Two individually verified graphs may still disagree. Contract comparison will
align declared public inputs and outputs, then report:

- missing or extra values;
- dimension changes;
- compatible dimension but different scale or offset;
- quantity-kind changes;
- normalization provenance changes;
- underconstrained values on either side.

Statistical drift tools remain complementary: UnitSentinel finds semantic
contract skew even when no representative payload samples are available.

## ONNX adapter boundary

ONNX describes tensor element types and static or symbolic shapes. Its metadata
can carry strings, but UnitSentinel will not treat arbitrary metadata as trusted
unit semantics. The adapter will:

1. validate the model with the official ONNX checker;
2. import only a closed operator subset;
3. read a versioned UnitSentinel contract namespace;
4. lower into the same canonical core graph;
5. reject unsupported control flow or operator semantics rather than guessing.

The adapter is not implemented.

## Resource and security limits

- No network access is required by verification or replay.
- No shell, `eval`, dynamic import, or arbitrary model execution.
- Fixed limits for document bytes/tree shape, inputs, values, nodes, outputs,
  tensor rank, exponent size, core-shrink checks, uniqueness checks, solver
  memory, and solver time.
- Deterministic errors omit host paths, environment values, and raw solver
  diagnostics.
- Unknown unit identifiers and unsupported operations fail closed.
- Canonical JSON readers reject duplicate fields and non-finite numbers.
- The same registry snapshot is used for compilation, certificate issuance,
  and replay.

The threat model excludes a same-UID process that can rewrite the verifier,
solver binary, or repository parent directories during execution. The
reproducible evidence tooling states its separate rendering and filesystem
boundary explicitly.

## Current implementation map

| Area | Current | Next |
| --- | --- | --- |
| Package boundary | Typed exact values, graph, registry, verification/repair results, certificates, replay reports, and content-addressed comparison plans | Verified comparison result |
| Dimension semantics | Exact bounded rational algebra and graph inference | Contract comparison |
| Unit registry | Immutable 33-unit snapshot with pinned SHA-256 | External snapshot decoder |
| Graph IR | Content-addressed bounded IR and strict decoder | ONNX lowering |
| Solver | Tracked dimension/kind/scale/offset constraints, uniqueness, replay, bounded cores, and repair re-verification | Training/serving contract comparison |
| Repairs | One bounded, non-mutating, exact-registry annotation replacement with independent verification and abstention | Additional operators require separate design and review |
| Certificates | Positive-only canonical codec, content digest, and detached replay with optional strict-toolchain policy | Contract-comparison evidence |
| CLI | Bounded regular-file reads, stable exits, atomic no-overwrite certificate publication, and read-only repair reports | Contract-comparison command |
| Visual evidence | Real CLI captures, repair lineage, diagrams, plot, GIF, accessible SVG, and closed digest manifest | Grouped synthetic fault corpus |
