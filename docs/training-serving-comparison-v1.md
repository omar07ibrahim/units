# Training-serving comparison contract v1

Status: the canonical comparison plan, bounded byte codec, fresh-verification
engine, and immutable comparison result are implemented. Normalization lineage,
the CLI surface, and reproducible comparison evidence are added in later
commits on the comparison branch.

## Objective

A training graph and a serving graph can each be dimensionally verified while
their public contracts still disagree. Version 1 defines an explicit,
content-addressed plan for aligning those interfaces without guessing from
names, positions, tensor values, or statistical samples.

The plan is not a compatibility result. It binds the exact graph and registry
bytes that the comparison engine verifies and names every intended public
contract slot.

## Closed plan model

`ComparisonPlan` contains:

| Field | Contract |
| --- | --- |
| `comparison_id` | Canonical lowercase identifier for this comparison |
| `training_graph_digest` | SHA-256 of one canonical training graph |
| `serving_graph_digest` | SHA-256 of one canonical serving graph |
| `registry_digest` | SHA-256 of the one registry snapshot used on both sides |
| `bindings` | One to 256 sorted, unique logical contract bindings |

The serialized schema is
`unitsentinel.training-serving-comparison/v1`. The plan digest is the SHA-256
of its canonical bytes and is deliberately not embedded in those bytes.

Each `ContractBinding` has one canonical `contract_id` and two nullable sides:

```json
{
  "contract_id": "current-wheel-speed",
  "serving": {
    "role": "input",
    "value_id": "request-speed"
  },
  "training": {
    "role": "input",
    "value_id": "wheel-speed-feature"
  }
}
```

An endpoint names an intended public occurrence `(role, value_id)`, where
`role` is `input` or `output`. Constructing or decoding the plan does not prove
that the value exists, that the declared role is public, or that every public
occurrence is covered. The engine establishes all three properties from the
bound graphs and rejects internal values.

## Explicit alignment only

The plan never performs fuzzy, positional, alias-based, or embedding-based
matching. Different graph identifiers may be aligned only through one explicit
logical binding.

A two-sided binding declares different `value_id` spellings to be an
intentional rename only under that caller-trusted plan; the rename alone is not
drift. The plan itself grants no permission. The result is therefore
always scoped as “compatible under this plan”, binds the exact plan SHA-256,
and reports `authentication: not-provided`. Port order remains independent
metadata and is compared separately.

The invariants are:

- bindings are sorted and unique by `contract_id`;
- the same endpoint occurrence cannot appear twice on one side;
- at least one side of every binding is present;
- a two-sided binding may retain different roles so the engine can report an
  explicit role drift rather than silently repairing it;
- a training-only binding declares a serving-side absence;
- a serving-only binding declares a training-side absence.

The comparison engine additionally establishes endpoint membership and
requires every public input and output occurrence from both bound graphs to
appear exactly once. An omitted occurrence is an incomplete plan, not
permission to ignore part of an interface.

This representation distinguishes three cases that a same-name join cannot:

1. an intentional rename with otherwise matching semantics;
2. a public value missing on one side;
3. a value moved from input to output or the reverse.

## Byte boundary

The decoder accepts exact canonical UTF-8 JSON bytes and reuses UnitSentinel's
allocation preflight. Version 1 fixes:

- 131,072 document bytes;
- six nested containers;
- 256 entries in one object or array;
- 4,096 total JSON values;
- 192 Unicode scalar values in one string;
- ten digits in an integer token.

Duplicate fields, floats, non-finite numbers, a BOM, invalid UTF-8, unknown or
missing fields, noncanonical ordering or whitespace, oversized structures, and
unsupported schemas fail closed. Decoding reconstructs the immutable model and
then requires its canonical bytes to equal the input byte for byte.

## Fresh comparison engine

`compare_graphs` accepts the plan plus keyword-only training and serving
graphs, one registry snapshot, one exact `SolverLimits`, and an optional
`ComparisonPolicy` with a caller-trusted expected plan digest. It evaluates
them in this order:

1. validate exact plan, graph, registry, limit, and caller policy object types;
2. when policy supplies an expected plan digest, require it to match before
   interpreting any binding;
3. require both graph digests and the registry digest to match the plan;
4. establish endpoint membership and require total, duplicate-free coverage of
   every public occurrence;
5. freshly run the existing verifier for both graphs with the same registry
   snapshot and bounded solver policy;
6. compare no interfaces unless both results are complete `verified` results;
7. compare each aligned endpoint's role, dtype, declared shape, explicit unit
   identifier, dimension, quantity kind, exact scale, and exact offset;
8. revalidate all nested objects and identities before publishing a result;
9. emit `authentication: not-provided` and the exact plan digest in every
   report.

A conflict, underconstrained graph, unknown verification, timeout, resource
limit, stale digest, incomplete contract coverage, unexpected verifier return,
or nested mutation can never become a positive compatibility result.

For ordinary verifier outcomes the engine makes exactly two verifier calls,
even when the training result is already negative or indeterminate. A source
mutation is a rejected comparison input and can stop processing immediately.
Each accepted positive result must cover every graph value and pass the
solver-independent semantic replay before any interface snapshot is built.

The immutable result uses schema
`unitsentinel.training-serving-comparison-result/v1` and has three outcomes:

| Status | Meaning |
| --- | --- |
| `compatible` | Both graphs freshly verify and every mapped interface field agrees |
| `drift` | Both graphs freshly verify and at least one exact mismatch is present |
| `indeterminate` | A graph is not verified or a fresh verifier result is rejected; no partial interface findings are published |

Each decisive binding lists mismatch codes in this fixed order:
`missing-in-serving`, `extra-in-serving`, `role-drift`, `position-drift`,
`dtype-drift`, `shape-drift`, `explicit-unit-drift`, `dimension-drift`,
`kind-drift`, `scale-drift`, and `offset-drift`. Position is role-local:
input and output ordinals are not compared across a role change. Different
`value_id` strings are not themselves drift because the explicit binding
already declares the logical alignment.

The work is structurally bounded by 256 bindings, the graph size limits, and
two verifier calls. `SolverLimits.total_timeout_ms` and `max_memory_mb` apply
to each solver call; the documented worst-case solver time is therefore twice
the per-call total plus bounded validation and replay overhead.

`ComparisonResult` is an unsigned detached claim. Direct construction cannot
prove freshness. Callers that rely on a result must obtain it from
`compare_graphs`, retain the caller-trusted plan policy separately, and treat
its content digest as integrity rather than author authentication.

## Planned lineage boundary

Version 1 normalization provenance is intentionally narrower than arbitrary
graph equivalence. It will trace dimensionless ratio-normalization sites
created by `divide` operations.

The lineage fingerprint will:

- use mapped logical contract IDs for public input roots;
- include closed operation attributes and exact inferred contracts;
- collapse identity nodes;
- sort children only for operations that are actually commutative;
- preserve order for subtract, divide, and matrix multiplication;
- ignore internal node/value renames;
- bind every canonical record by SHA-256.

The output can establish that the normalization lineage changed. It does not
establish that training is correct, serving is wrong, or two different
algebraic forms are mathematically equivalent.

## Security and nonclaims

The plan contains no tensor payloads, model code, import paths, URLs, plugins,
callbacks, credentials, or executable metadata. Comparing it requires no
network and does not run a model.

The plan and report are unsigned content-addressed records. Their digests
detect byte changes; they do not authenticate an author, prove that a
deployment used either graph, validate broadcasting or matrix shapes, measure
statistical drift, or establish scientific correctness.

A party that needs an approved mapping must supply a caller-trusted expected
plan digest or equivalent allow-list policy to the engine and must reject a
mismatch. Recomputing a digest after replacing a plan establishes only the
integrity of the replacement; it does not make the replacement trusted.
